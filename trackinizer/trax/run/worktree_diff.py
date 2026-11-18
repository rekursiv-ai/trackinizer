"""Capture a session's net ``git diff`` of the files it edited, for its Trax page.

While a ``trax run`` session works in a git work tree, this turns the files the
session ITSELF edited into a net unified diff, refreshed at each turn, so the
session's web page can show what it changed. The diff is stored beside the
session's records (:func:`~trackinizer.trax.run.sink.Sink.emit_worktree_diff`)
and read by exactly the transcript's readers.

What is diffed is only the paths the session's own
:class:`~trackinizer.lib.agent.types.sessions.FileEditResult` /
:class:`~trackinizer.lib.agent.types.sessions.FileWriteResult` records name, inside the
tree -- so another agent's edits in a shared tree are never attributed here, and
a credential file (:func:`held`) is neither listed nor diffed.

What the tree changed is asked of git alone, run so it changes nothing: no
optional lock, no hook, no fsmonitor, no external diff or text conversion, no
pattern-expanded pathspec, ``--`` before every path, each command bounded in
time and output. The base is the ``HEAD`` sha pinned when the run starts, so the
diff is what THIS session changed, committed edits included; if a rebase drops
that sha the diff falls back to ``HEAD`` (and then a pull or rebase in the
session shows others' changes to the touched files).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import os
import stat
import subprocess

from trackinizer.lib.agent.sessions.udiff import parse_udiff
from trackinizer.lib.agent.types.sessions import FileEditResult, FileWriteResult, Splice


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trackinizer.types.streams import TraxRecord


__all__ = [
    "DIFF_BYTES",
    "REFRESH_INTERVAL_SEC",
    "WORKTREE_DIFF_PART",
    "EmitDiff",
    "Output",
    "Runner",
    "WorktreeDiff",
    "held",
    "open_worktree_diff",
    "run_git",
]


WORKTREE_DIFF_PART: Final = "worktree-diff"
"""The synthetic part name the diff records are stored under.

Not a real session-log file, so no capture ever produces it; the web reads this
same string to lift the diff out of the transcript into its Changes panel.
"""

REFRESH_INTERVAL_SEC: Final = 60.0
"""Seconds before a refresh runs on an unchanged touched set.

A formatter or pre-commit fixer edits a touched file without a new edit record,
so time alone must be able to trigger a refresh.
"""

MIN_REFRESH_SEC: Final = 2.0
"""The least a dirty set waits before a refresh re-runs git.

The drain loop ticks every ~50 ms, so a bare "refresh on a new edit" would
re-diff the whole touched set dozens of times a turn; this floor bounds git to
once every couple of seconds while still reflecting an edit promptly.
"""

GIT_SEC: Final = 20.0
"""Seconds one git command may run before it is killed."""

DIFF_BYTES: Final = 262_144
"""Bytes of one file's diff kept; a longer one is reported omitted, never cut."""

TOTAL_BYTES: Final = 1_048_576
"""Bytes of all diffs kept in one refresh; the rest are reported omitted."""

SOURCE_BYTES: Final = 1_048_576
"""A file larger than this on disk is reported omitted rather than diffed."""

MAX_FILES: Final = 200
"""Changed files shown in one refresh; the rest are counted, not drawn."""

_LISTING_BYTES: Final = 1_048_576
"""Bytes kept from a short git command (a sha, a path listing)."""

# Read-only, side-effect-free git: no pager, no index lock, no pattern-expanded
# pathspec, no fsmonitor, no hook. Prepended to every command.
_PREAMBLE: Final = (
    "--no-pager",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)

_HELD_NAMES: Final = frozenset(
    {
        *(".env", ".envrc", ".npmrc", ".netrc", ".pypirc", ".git-credentials"),
        *(".mcp.json", ".pgpass", ".vault-token", "kubeconfig"),
        *("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"),
    },
)
"""File names that hold credentials: env files, logins, cluster config, keys."""

_HELD_SUFFIXES: Final = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt")
"""Endings of key, certificate and keystore files."""

_HELD_DIRECTORIES: Final = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})
"""Directories that hold keys, cloud and cluster credentials, registry logins."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Output:
    """What one git command printed, and how it ended.

    Attributes:
      returncode: Its exit status; ``-1`` when it ran out of time or could not
        start.
      stdout: What it printed, at most the byte bound it was read to.
      cut: Whether the output reached that bound and was truncated.

    """

    returncode: int
    stdout: bytes
    cut: bool


type Runner = Callable[[Sequence[str], int], Output]
"""Runs one git command ``argv``, its output kept to ``max_bytes``."""

type EmitDiff = Callable[[Sequence[FileEditResult]], None]
"""Uploads one refresh's diff records to the session's store."""


def run_git(argv: Sequence[str], max_bytes: int) -> Output:
    """Run one git command, read-only, bounded in time and output.

    Args:
      argv: The full command, ``git`` first.
      max_bytes: Where its output is cut.

    Returns:
      output: What it printed and how it ended.

    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(  # noqa: S603 -- fixed argv, no shell.
            list(argv),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        return Output(
            returncode=-1,
            stdout=stdout[:max_bytes],
            cut=len(stdout) > max_bytes,
        )
    except OSError:
        return Output(returncode=-1, stdout=b"", cut=False)
    return Output(
        returncode=done.returncode,
        stdout=done.stdout[:max_bytes],
        cut=len(done.stdout) > max_bytes,
    )


def open_worktree_diff(cwd: Path, *, run: Runner = run_git) -> WorktreeDiff | None:
    """Open a diff capture for the work tree holding ``cwd``, or ``None``.

    Args:
      cwd: The directory the run started in.
      run: Runs each git command; the real subprocess runner by default.

    Returns:
      diff: The capture, or ``None`` when ``cwd`` is in no git work tree.

    """
    top_line = _line(run(_argv(cwd, ("rev-parse", "--show-toplevel")), _LISTING_BYTES))
    if top_line is None:
        return None
    top = Path(top_line).resolve()
    base = _line(run(_argv(top, ("rev-parse", "HEAD")), _LISTING_BYTES)) or "HEAD"
    return WorktreeDiff(top=top, run=run, base=base)


def held(path: str) -> bool:
    """Say whether ``path`` names a credential file, by any of its segments.

    Args:
      path: A relative, ``/``-separated path.

    Returns:
      held: Whether a segment is an env, credential or key file's, or a
        directory of keys, case ignored.

    """
    return any(
        segment in _HELD_NAMES
        or segment in _HELD_DIRECTORIES
        or segment.startswith(".env.")
        or segment.endswith(_HELD_SUFFIXES)
        for segment in path.casefold().split("/")
    )


class WorktreeDiff:
    """Accumulate a session's edited paths and render their net diff on demand.

    Stateful and single-threaded: the drain thread (the one sink writer) calls
    :meth:`note` for each emitted record and :meth:`refresh` at each turn
    boundary, so no locking is needed here.

    Args:
      top: The work tree's root, resolved.
      run: Runs each git command.
      base: The ``HEAD`` sha pinned at the run's start, or ``"HEAD"`` when none
        was resolved (an empty repository).

    """

    def __init__(self, *, top: Path, run: Runner, base: str) -> None:
        self._top = top
        self._run = run
        self._base = base
        self._touched: set[str] = set()
        self._dirty = False
        self._last_refresh = float("-inf")
        # The rendered diff last uploaded, so an unchanged refresh uploads nothing.
        self._last_rendered = ""

    @property
    def top(self) -> Path:
        """The work tree's root."""
        return self._top

    @property
    def base(self) -> str:
        """The base revision the diff is taken against."""
        return self._base

    @property
    def touched(self) -> frozenset[str]:
        """The tree-relative paths the session has edited so far."""
        return frozenset(self._touched)

    def note(self, record: TraxRecord) -> None:
        """Track one emitted record's path, when it is an edit inside the tree.

        Args:
          record: A record the session just emitted.

        """
        if not isinstance(record, FileEditResult | FileWriteResult):
            return
        path = record.path
        if path is None or not path:
            return
        rel = self._relpath(path)
        if rel is None or held(rel):
            return
        self._touched.add(rel)
        self._dirty = True

    def refresh(self, emit: EmitDiff, *, now: float, force: bool = False) -> None:
        """Render the net diff and upload it, when due and when it changed.

        Args:
          emit: Uploads the diff records.
          now: Monotonic seconds, for the refresh interval.
          force: Render even when not otherwise due (a final refresh at exit);
            an unchanged diff is still not re-uploaded.

        """
        if not force and not self._due(now):
            return
        self._last_refresh = now
        self._dirty = False
        records, rendered = self._build()
        if rendered == self._last_rendered:
            return
        self._last_rendered = rendered
        emit(records)

    def _due(self, now: float) -> bool:
        """Whether a refresh should run: a recent edit, or time on a non-empty set."""
        gap = now - self._last_refresh
        if self._dirty:
            return gap >= MIN_REFRESH_SEC
        return bool(self._touched) and gap >= REFRESH_INTERVAL_SEC

    def _build(self) -> tuple[list[FileEditResult], str]:
        """Render each touched file's diff, within the file and total byte caps."""
        base = self._effective_base()
        records: list[FileEditResult] = []
        parts: list[str] = []
        used = 0
        for rel in sorted(self._touched):
            if len(records) >= MAX_FILES:
                records.append(
                    _note(
                        "(more)",
                        f"{len(self._touched) - MAX_FILES} more changed files omitted",
                    ),
                )
                break
            made = self._file(rel, base)
            if made is None:
                continue
            record, key, cost = made
            if used + cost > TOTAL_BYTES:
                records.append(
                    _note("(more)", "remaining diffs omitted: over the total size cap"),
                )
                break
            records.append(record)
            parts.append(f"{rel}\n{key}")
            used += cost
        return records, "\n".join(parts)

    def _file(self, rel: str, base: str) -> tuple[FileEditResult, str, int] | None:
        """One file's record, its dedup key and byte cost; ``None`` when unchanged."""
        size = self._size(rel)
        if size is not None and size > SOURCE_BYTES:
            return _note(rel, "large file; diff omitted"), "large", 0
        text, cut = self._diff_text(rel, base, size)
        if text is None:
            return None
        if cut:
            return _note(rel, "diff omitted: over the size cap"), "omitted", 0
        edits = parse_udiff(_hunks(text))
        if not edits:
            return _note(rel, "binary or no textual change"), "binary", 0
        return (
            FileEditResult(call_id="", path=rel, edits=edits),
            text,
            len(text.encode()),
        )

    def _diff_text(
        self,
        rel: str,
        base: str,
        size: int | None,
    ) -> tuple[str | None, bool]:
        """One file's diff against ``base``; ``(None, False)`` when it is unchanged."""
        tracked = self._git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            base,
            "--",
            rel,
        )
        if tracked.stdout:
            return _decode(tracked.stdout), tracked.cut
        if self._tracked(rel) or size is None or size > SOURCE_BYTES:
            return None, False
        # Untracked and small: compared against nothing, so it shows as all added.
        added = self._git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-index",
            "--",
            os.devnull,
            rel,
        )
        return (_decode(added.stdout), added.cut) if added.stdout else (None, False)

    def _tracked(self, rel: str) -> bool:
        """Whether git tracks ``rel`` (so an empty diff means unchanged, not new)."""
        return self._git("ls-files", "--error-unmatch", "--", rel).returncode == 0

    def _effective_base(self) -> str:
        """Return the pinned base sha while it still resolves, else ``HEAD``."""
        if self._base == "HEAD":
            return "HEAD"
        found = self._git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"{self._base}^{{commit}}",
        )
        return self._base if found.returncode == 0 and found.stdout else "HEAD"

    def _size(self, rel: str) -> int | None:
        """Return the size of ``rel`` as a regular file, by ``lstat``; ``None`` else."""
        try:
            info = (self._top / rel).lstat()
        except OSError:
            return None
        return info.st_size if stat.S_ISREG(info.st_mode) else None

    def _relpath(self, raw: str) -> str | None:
        """``raw`` as a path relative to the tree root, or ``None`` when outside it."""
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._top / candidate
        try:
            return str(candidate.resolve().relative_to(self._top))
        except ValueError:
            return None

    def _git(self, *args: str, max_bytes: int = DIFF_BYTES) -> Output:
        """Run one git command in the tree, read-only."""
        return self._run(_argv(self._top, args), max_bytes)


def _note(path: str, message: str) -> FileEditResult:
    """Build a placeholder edit whose line explains why a file's diff is not shown."""
    return FileEditResult(call_id="", path=path, edits=(Splice(lead=f"({message})\n"),))


def _argv(directory: Path, args: Sequence[str]) -> list[str]:
    """Build a read-only git command run in ``directory``."""
    return ["git", *_PREAMBLE, "-C", str(directory), *args]


def _line(out: Output) -> str | None:
    """Return a one-line git answer without its newline, or ``None`` when git failed."""
    if out.returncode != 0 or out.cut:
        return None
    text = out.stdout.decode(errors="replace").strip()
    return text or None


def _decode(data: bytes) -> str:
    """Git diff bytes as text, NULs dropped: no document carries a NUL."""
    return data.decode(errors="replace").replace("\0", "")


def _hunks(text: str) -> str:
    """Return a file's diff hunks -- from its first ``@@`` -- without git's header."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("@@ "):
            return "\n".join(lines[index:])
    return ""
