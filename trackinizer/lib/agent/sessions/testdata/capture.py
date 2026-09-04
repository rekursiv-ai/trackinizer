#!/bin/sh
# ruff: noqa: EXE003, D300, T201 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Capture hermetic agent-CLI sessions as round-trip test fixtures.

Each capture points the CLI at a throwaway config root -- ``CLAUDE_CONFIG_DIR``
for claude, ``CODEX_HOME`` for codex -- so a run never reads or writes the
operator's real session history. Credentials are copied in, ONE scripted
session drives the CLI through every record kind a log can contain, and the
session files it wrote are redacted and copied to the fixture directory.

One session per CLI, not one per feature: a fixture should look like a real
log, and a real log is a single session that did many things. The script is
typed into the running TUI over a pseudo-terminal, because that is the only
interface that reaches everything -- a slash command (``/model``,
``/compact``) is handled inside the CLI and never becomes a command-line
argument, so ``-p`` / ``codex exec`` cannot produce those records at all.

Claude splits one session into two files regardless: a spawned subagent's
transcript lands under ``<session-id>/subagents/``. That is the CLI's
layout, not a second capture.

The fixtures are live CLI output, so they capture record kinds no
hand-written sample anticipates: 2026-08 captures surfaced claude's
``atis-latch`` and codex's ``event_msg/item_completed``, neither of which any
adapter maps today.

Costs real API tokens. Run it when a CLI version changes, not per test run;
the emitted JSONL is what the test suite reads. It lives beside the fixtures
it writes, so the command that regenerates them is found by looking at them.

Examples:
  sh trackinizer/lib/agent/sessions/testdata/capture.py --cli claude
  sh trackinizer/lib/agent/sessions/testdata/capture.py --out /tmp/fixtures

'''
# fmt: on

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import argparse
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib

from trackinizer.lib.custom_json import DictCodec
from trackinizer.lib.posix.relay import ThreadedRelay


# One scripted session per CLI, submitted line by line into the running TUI.
#
# A single session rather than one run per feature, because the goal is a
# fixture that stresses everything a real log contains -- and a real log is
# one session that did many things, not several that each did one. Driving
# the TUI is what makes that possible: a slash command is handled inside the
# CLI and reaches the log only when a human submits it on a terminal, so
# ``-p`` / ``codex exec`` cannot reach ``/model`` or ``/compact`` at all.
#
# The turns, and the record kinds each is here to produce:
#   1. a succeeding tool call, a failing one (``is_error``), a file write and
#      an edit, and an image read (an attachment)
#   2. a web search and a fetch -- distinct kinds, and absent from any
#      offline capture
#   3. a subagent spawn, whose transcript claude writes to its own sidechain
#      file
#   4. ``/model``, which records the model change. Submitted bare, so the CLI
#      offers its own picker and the follow-up Enter accepts whatever it
#      lists: naming a model here would date the fixture to an alias that
#      moves, and the record under test is the change, not the destination.
#   5. ``/compact``, which records the compaction
_SESSION_SCRIPT: Final = (
    (
        'Do these in order, briefly: (1) run the shell command "echo alpha". '
        '(2) run the shell command "exit 7", which fails. (3) create note.txt '
        'containing "beta", then change beta to gamma. (4) read tiny.png and '
        "describe it in three words."
    ),
    (
        "Now search the web for the current stable Python version, then fetch "
        "https://example.com and quote its heading."
    ),
    (
        "Spawn one general-purpose subagent whose entire job is to reply with "
        'the word "sub". Report what it said.'
    ),
    "/model",
    "Say exactly: after-model",
    "/compact",
    "Say exactly: after-compact",
)


def main(argv: Sequence[str] | None = None) -> int:
    """Capture sessions and return the process exit code."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    # House-authorized deviation from "no generated data in the checkout":
    # the scratch tree lives beside the fixtures, under a .gitignore, rather
    # than in a temp dir. A capture that fails leaves the CLI's own config
    # root and workspace to read, and a self-deleting temp dir destroys
    # exactly the evidence needed to tell a swallowed turn from a refused
    # one. Nothing here is committed; ``--out`` still redirects the fixtures.
    root = args.out / "scratch"
    shutil.rmtree(root, ignore_errors=True)
    # ``_drive`` chdirs into each capture's workspace, so the original cwd
    # is restored between CLIs; otherwise the second capture inherits the
    # first's directory.
    origin = Path.cwd()
    if args.cli in ("claude", "both"):
        written += _capture_claude(
            root / "claude",
            args.out,
            turn_sec=args.turn_sec,
            timeout_sec=args.timeout_sec,
        )
        os.chdir(origin)
    if args.cli in ("codex", "both"):
        written += _capture_codex(
            root / "codex",
            args.out,
            turn_sec=args.turn_sec,
            timeout_sec=args.timeout_sec,
        )
        os.chdir(origin)
    for path in written:
        print(f"{path} ({path.stat().st_size} bytes)")
    if not written:
        print("no session files captured", file=sys.stderr)
    return 0 if written else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--cli",
        choices=("claude", "codex", "both"),
        default="both",
        help="Which CLI to drive (default: both).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory to write fixtures into.",
    )
    # No ``--model``: each CLI's own default is what a real session uses, and
    # pinning one dates the fixture to a model alias that will move.
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=300,
        help="Seconds to wait for the CLI to exit after the script (default: 300).",
    )
    parser.add_argument(
        "--turn-sec",
        type=float,
        default=180.0,
        help=(
            "Cap on how long one scripted turn may run. Turns normally finish "
            "when the session log goes quiet; this only bounds a wedged one "
            "(default: 180)."
        ),
    )


def _capture_claude(
    root: Path, out: Path, *, turn_sec: float, timeout_sec: int
) -> list[Path]:
    """Drive one claude session and return the fixture paths written.

    A subagent's transcript lives at ``<session>/subagents/*.jsonl`` and
    carries the ``isSidechain`` / ``agentId`` envelope no top-level session
    has, so the one session yields two files. That split is claude's, not a
    choice here -- every other record kind lands in the single main file.

    ``--permission-mode bypassPermissions``, not
    ``--dangerously-skip-permissions``: both skip per-tool prompts, but the
    latter shows a one-time acceptance dialog on first use in a TUI, and the
    first scripted line answers THAT instead of being submitted as a turn.
    Either way an unanswered prompt blocks on a keypress the script never
    sends. The capture runs in a throwaway workspace on a fixed script, so
    there is nothing for a tool to damage.
    """
    home, work = _prepare(root, real_home=Path.home() / ".claude")
    _drive(
        ["claude", "--permission-mode", "bypassPermissions"],
        cwd=work,
        env={"CLAUDE_CONFIG_DIR": str(home)},
        turn_sec=turn_sec,
        timeout_sec=timeout_sec,
    )
    return [
        _emit(
            path,
            out
            / (
                "claude_sidechain.jsonl"
                if path.parent.name == "subagents"
                else "claude_main.jsonl"
            ),
            home=home,
            work=work,
        )
        for path in sorted((home / "projects").rglob("*.jsonl"))
    ]


def _capture_codex(
    root: Path, out: Path, *, turn_sec: float, timeout_sec: int
) -> list[Path]:
    """Drive one codex session and return the fixture paths written.

    ``model_reasoning_summary=detailed`` is what populates a ``reasoning``
    item's ``summary[].text``; without it the thinking record is
    encrypted-only and the fixture cannot exercise readable thinking.
    """
    home, work = _prepare(root, real_home=Path.home() / ".codex")
    _drive(
        [
            "codex",
            "--skip-git-repo-check",
            "-c",
            "model_reasoning_summary=detailed",
            "--dangerously-bypass-approvals-and-sandbox",
        ],
        cwd=work,
        env={"CODEX_HOME": str(home)},
        turn_sec=turn_sec,
        timeout_sec=timeout_sec,
    )
    return [
        _emit(path, out / "codex_main.jsonl", home=home, work=work)
        for path in sorted((home / "sessions").rglob("rollout-*.jsonl"))
    ]


def _drive(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    turn_sec: float,
    timeout_sec: int,
) -> None:
    """Run ``argv`` on a PTY and submit the scripted session into its TUI.

    The TUI is the only interface that reaches every record kind: a slash
    command is handled inside the CLI and never becomes an argument, so
    ``-p`` / ``codex exec`` cannot produce a model change or a compaction at
    all. :class:`~trackinizer.lib.posix.relay.ThreadedRelay` owns the master
    fd, which makes an injected line indistinguishable from a typed one --
    bracketed paste, then Enter as its own delayed write.

    Synchronization is on the session log, not the clock. The relay mirrors
    bytes without parsing them, so the TUI offers no readback to wait on --
    but the CLI appends to its session file as a turn progresses, and that
    file is exactly what this script captures. Waiting for the log to go
    quiet is therefore a direct measure of "the turn finished".

    A fixed sleep was tried first and silently lost the first two turns: they
    were submitted before the TUI had drawn its prompt, and a line typed into
    a terminal that is not ready is discarded with no error anywhere. Waiting
    for the log to appear removes that whole class of failure -- the file
    cannot exist until the session does.
    """
    if shutil.which(argv[0]) is None:
        print(f"{argv[0]}: not found in PATH; skipping", file=sys.stderr)
        return
    print(f"$ {argv[0]} ... (cwd={cwd}, {len(_SESSION_SCRIPT)} turns)", file=sys.stderr)
    _isolate_environment()
    # ``cwd`` is a real chdir in the forked child, not a ``PWD`` in ``env``:
    # claude names its session directory after the directory it asks the
    # kernel for at boot, and the isolation allowlist drops ``PWD`` anyway.
    relay = ThreadedRelay(argv, cwd=cwd, env=env)
    thread = threading.Thread(target=relay.run, daemon=True)
    thread.start()
    logs = Path(next(iter(env.values())))
    _await_logs(logs, deadline_sec=turn_sec)
    done = _completed_turns(logs)
    for index, line in enumerate(_SESSION_SCRIPT, start=1):
        relay.submit(line)
        print(f"  turn {index}/{len(_SESSION_SCRIPT)}: {line[:48]}", file=sys.stderr)
        done = _await_turn(logs, done=done, quiet_sec=20.0, deadline_sec=turn_sec)
    relay.submit("/exit")
    thread.join(timeout=timeout_sec)
    relay.terminate()


def _isolate_environment() -> None:
    """Reduce this process's environment to what a CLI needs to run.

    The child inherits the operator's full environment through
    :class:`~trackinizer.lib.posix.relay.ThreadedRelay`, which caused both
    failures this capture has hit: ``ANTHROPIC_API_KEY`` made claude open a
    "use this key?" prompt at boot that ate the first two turns, and a codex
    run read a live key out of the environment and wrote it into its own
    transcript.

    An allowlist rather than a denylist, because the leak is the variable
    nobody thought to name -- ``OPENAI_API_KEY`` and every future ``*_TOKEN``
    a tool learns to read are excluded without being listed. Authentication
    comes from the credentials copied into the throwaway config root, so
    nothing here needs a secret.

    ``PWD`` is deliberately absent: the caller chdirs first, and claude names
    its session directory after the resolved working directory, so a stale
    ``PWD`` would file the capture's log under the operator's shell cwd.
    """
    keep = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
    }
    for name in [key for key in os.environ if key not in keep]:
        del os.environ[name]


def _await_logs(root: Path, *, deadline_sec: float) -> None:
    """Block until the CLI has written a session file under ``root``."""
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        if _log_size(root) > 0:
            # The file exists, but the TUI draws its prompt slightly after
            # opening the log; submitting into that gap loses the first turn.
            time.sleep(3.0)
            return
        time.sleep(0.5)
    print(f"  no session log under {root} yet; proceeding", file=sys.stderr)


def _await_turn(root: Path, *, done: int, quiet_sec: float, deadline_sec: float) -> int:
    """Block until one more turn has completed; return the new completed count.

    Claude closes each turn with a ``system`` / ``turn_duration`` record, so
    counting those is an exact end-of-turn signal rather than an inference.
    Polling for silence instead cost the whole quiet window on EVERY turn --
    seven turns of a 20s window added ~140s to a session the model finishes
    in about 50 -- and still ended turns early, because a window short enough
    to be cheap is shorter than the 13.5s gaps that occur inside one turn.

    ``quiet_sec`` is the fallback for a CLI that writes no such marker (codex,
    and any turn claude ends without one): the log going quiet for that long
    is then the only available signal.
    """
    deadline = time.monotonic() + deadline_sec
    size = _log_size(root)
    settled = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.5)
        completed = _completed_turns(root)
        if completed > done:
            return completed
        grown = _log_size(root)
        if grown != size:
            size = grown
            settled = time.monotonic()
        elif time.monotonic() - settled >= quiet_sec:
            return done
    return done


def _log_size(root: Path) -> int:
    """Return the total size of every session log under ``root``."""
    return sum(path.stat().st_size for path in root.rglob("*.jsonl") if path.is_file())


def _completed_turns(root: Path) -> int:
    """Return how many ``turn_duration`` records the session logs carry.

    Read as raw text rather than parsed: this runs twice a second while the
    CLI appends, so a partially-written final line is expected, and counting
    a marker substring cannot fail on one.
    """
    total = 0
    for path in root.rglob("*.jsonl"):
        if path.parent.name == "subagents":
            continue  # A subagent's turns are not the session's turns.
        total += path.read_text(encoding="utf-8", errors="replace").count(
            '"subtype":"turn_duration"'
        )
    return total


def _prepare(root: Path, *, real_home: Path) -> tuple[Path, Path]:
    """Build a throwaway config root and workspace; return ``(home, work)``.

    Credentials are copied rather than shared: the CLI rewrites files in its
    config root (session indexes, caches), and pointing it at the operator's
    real one is what a hermetic capture exists to avoid.
    """
    home = root / "home"
    work = root / "work"
    home.mkdir(parents=True)
    work.mkdir(parents=True)
    for name in ("auth.json", "config.toml", ".credentials.json", "settings.json"):
        source = real_home / name
        if source.is_file():
            shutil.copy2(source, home / name)
    _seed_onboarding(home, work, real_home=real_home)
    (work / "tiny.png").write_bytes(_tiny_png())
    return home, work


def _seed_onboarding(home: Path, work: Path, *, real_home: Path) -> None:
    """Pre-answer claude's startup dialogs in a throwaway config root.

    ``-p`` skips these; the TUI does not, and each one absorbs a scripted
    line as a menu keystroke, so the capture silently runs turns short. Two
    dialogs exist and both must be seeded:

    * onboarding -- an unseeded root opens the OAuth login wizard instead of
      a prompt, so no session is ever written;
    * the per-workspace trust dialog ("Is this a project you trust?"), which
      fires on any directory claude has not seen -- and a throwaway workspace
      is new every run by construction.

    Every key but ``projects``, not a hand-picked few. A three-key subset was
    what this seeded first, and driven against the real binary it HANGS: the
    CLI wants ~80 further acknowledgement and migration flags, and paints a
    dialog for a missing one rather than reading the message. ``projects`` is
    the one key genuinely worth dropping -- it is 133 KB of the operator's own
    per-directory history, and a run without it answers identically (both
    measured).
    """
    source = Path.home() / ".claude.json"
    if real_home.name != ".claude" or not source.is_file():
        return
    real = DictCodec.coerce(json.loads(source.read_text(encoding="utf-8")))
    seeded: dict[str, object] = {
        key: value for key, value in real.items() if key != "projects"
    }
    seeded["projects"] = {
        str(work): {
            "hasTrustDialogAccepted": True,
            "hasCompletedProjectOnboarding": True,
            "projectOnboardingSeenCount": 1,
        }
    }
    (home / ".claude.json").write_text(json.dumps(seeded), encoding="utf-8")


def _tiny_png() -> bytes:
    """Return a 2x2 PNG: the smallest input that exercises an image read."""
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00\x00\x00\xff\x00\xff\x00\x00\xff")
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        _png_chunk(tag, body)
        for tag, body in ((b"IHDR", header), (b"IDAT", pixels), (b"IEND", b""))
    )


def _png_chunk(tag: bytes, body: bytes) -> bytes:
    """Return one length-prefixed, CRC-suffixed PNG chunk."""
    return (
        struct.pack(">I", len(body))
        + tag
        + body
        + struct.pack(">I", zlib.crc32(tag + body))
    )


# Credential shapes a transcript must never carry. A capture runs real tools
# against a real environment, so a model that reads a config file or a
# process listing can echo a live secret into its own output -- one run wrote
# a 1900-character key into its rollout.
#
# The vendor-prefixed patterns are a convenience, not the defence: a
# credential this list does not know is caught by the last two entries, which
# match on the FIELD (``api_key:``, ``Authorization:``) rather than on any
# issuer's format. That is what keeps the guard from being a list of the
# vendors somebody happened to think of.
_SECRETS: Final = (
    # Anthropic, OpenAI, Mistral, DeepSeek, and anything else using `sk-`.
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),  # Google
    re.compile(r"hf_[A-Za-z0-9]{20,}"),  # Hugging Face
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Issuer-agnostic: any bearer credential, and any field that names itself
    # a secret. These are what catch the vendor nobody listed above.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/-]{20,}"),
    re.compile(
        r"(?i)\"?(api[_-]?key|auth[_-]?token|access[_-]?token|client[_-]?secret"
        r"|secret|password|token)\"?\s*[:=]\s*\"[^\"]{20,}\""
    ),
)

# What a scrubbed credential is replaced with. Recognizable on sight, and
# distinguishable from a real secret by ``_reject_secrets`` -- the filler
# necessarily still matches the pattern that produced it.
_FILLER: Final = "deadbeef"


def _emit(source: Path, target: Path, *, home: Path, work: Path) -> Path:
    """Redact ``source`` into ``target`` and return the fixture path.

    The throwaway directories are named after a PID-unique temp path, so an
    unredacted fixture would differ on every capture and leak the operator's
    layout. The home directory is redacted too, and not only for the CLI's own
    envelope fields: a tool the model ran can print an absolute path from
    anywhere on the machine into its output (a Python traceback naming a
    site-packages file is how this was found).

    Account and organization identifiers are replaced too: a PTY capture
    writes a ``bridge-session`` record carrying ``ownerAccountUuid`` and
    ``ownerOrganizationUuid``, which name the operator's real account. Each
    is swapped for a fixed UUID, so the fixture keeps the field's shape --
    the thing a parser is tested against -- without its value.

    Every replacement is a JSON-safe literal of the same syntactic shape, so
    substituting in the raw text keeps each line parseable without
    re-serializing -- which would rewrite the CLI's own byte formatting, the
    thing under test.

    Raises:
      ValueError: The redacted text still matches a credential pattern. The
        fixture is not written: a capture that fails loudly costs one rerun,
        while one that silently commits a live key costs a rotation.

    """
    text = source.read_text(encoding="utf-8")
    for original, replacement in (
        (str(work), "/workspace"),
        (str(home), "/config"),
        (str(Path.home()), "/home/user"),
    ):
        text = text.replace(original, replacement)
    # Claude flattens the workspace path into a scratch directory name under
    # the system temp dir (``claude-<uid>/-tmp-run-scratch-work/``), so the
    # literal replacements above miss it: the separators are gone by then.
    # ``gettempdir`` rather than a hardcoded ``/tmp`` -- claude honors
    # ``TMPDIR``, so a host that sets one would leak the real path.
    scratch = re.escape(tempfile.gettempdir())
    text = re.sub(rf"{scratch}/claude-\d+/[^\"\\ ]*", "/workspace/.claude", text)
    for field in ("ownerAccountUuid", "ownerOrganizationUuid"):
        text = re.sub(
            rf'("{field}":\s*")[^"]*(")',
            r"\g<1>00000000-0000-0000-0000-000000000000\g<2>",
            text,
        )
    text = _scrub_secrets(text)
    _reject_secrets(text, source=source)
    target.write_text(text, encoding="utf-8")
    return target


def _scrub_secrets(text: str) -> str:
    """Replace every recognized credential with same-shape filler.

    The filler repeats ``deadbeef`` to the length of what it replaces and
    keeps the leading marker (``sk-``, ``ghp_``), so the record still looks
    like the thing the parser must handle: a fixed short token would change
    both the field's length and its shape, and a 1900-character key is
    exactly the case worth keeping in a fixture.
    """
    for pattern in _SECRETS:
        text = pattern.sub(_filler, text)
    return text


def _filler(found: re.Match[str]) -> str:
    """Return same-length filler preserving the match's issuer prefix."""
    secret = found.group()
    marker = re.match(r"[A-Za-z-]*[-_]", secret)
    prefix = marker.group() if marker else ""
    repeats = -(-(len(secret) - len(prefix)) // len(_FILLER))
    return prefix + (_FILLER * repeats)[: len(secret) - len(prefix)]


def _reject_secrets(text: str, *, source: Path) -> None:
    """Raise when ``text`` still carries a credential after scrubbing.

    Filler is exempt, and cannot be confused for a real secret: it is
    ``deadbeef`` repeated, which no issuer produces. Without the exemption
    this check fires on the scrub's own output, since the replacement keeps
    the shape that matched.

    Raises:
      ValueError: A credential survived scrubbing.

    """
    for pattern in _SECRETS:
        for found in pattern.finditer(text):
            if _FILLER in found.group():
                continue
            raise ValueError(
                f"{source}: credential-shaped text survived redaction "
                f"({found.group()[:12]}...); fixture not written"
            )


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
