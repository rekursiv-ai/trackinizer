"""The hooks ``trax run`` gives every claude it runs.

Three kinds of hook, one entry point:

- ``SessionStart`` writes a marker once claude is past its startup dialogs, so the
  run knows when a line typed in lands in the composer rather than in a dialog. A
  line typed into the trust dialog picks its highlighted "No, exit" and claude exits.
- ``UserPromptSubmit``, for a run placed on a station, tells the agent of the boxes
  the person drew there. Claude Code runs it before each prompt the person types and
  adds what it prints to that prompt's context. The person types into the CLI, never
  through the server, so this is the only place the agent can hear of a box: the
  same words, and the same rule (once, the first time after it is drawn), that a
  Chat partner follows.
- ``PreToolUse``, ``PermissionRequest``, ``PostToolUse``, ``Stop`` and ``SessionEnd``,
  for a run that syncs, hand each event to the run's prompt bridge
  (:mod:`~trackinizer.trax.run.prompt_bridge`), so a prompt claude asks reaches
  Chat and an answer from Chat reaches claude.

The hook never gets in the way of a prompt: any failure, or a server slower than
:data:`_BUDGET_SEC`, prints nothing and exits 0.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast

import contextlib
import json
import os
import shlex
import sys
import threading
import uuid

from treekle import PlainTree, from_plain, json_loads

from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.context import workspace_from_env
from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run import prompt_bridge
from trackinizer.trax.run.inbound import Box, render_box_lines


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from trackinizer.client.client import Client


_BUDGET_SEC: Final = 2.0
"""How long the hook waits for the server before it says nothing."""

_BOXES_PER_PROMPT: Final = 20
"""The newest this many are listed, as with a Chat line."""

_HOOK_TIMEOUT_SEC: Final = 4
"""Claude's own cap on the command, above :data:`_BUDGET_SEC` for starting python."""

_PROMPT_TIMEOUT_SEC: Final = 600
"""How long claude lets a ``PermissionRequest`` hook wait for Chat. Past it claude
ends the hook and asks in its TUI alone, as it would with no hook."""

_BRIDGED: Final = (
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)
"""The events handed to the prompt bridge, in the order a tool call raises them."""


@dataclass(frozen=True, slots=True, kw_only=True)
class HookFiles:
    """The files one run's hooks live in, in a directory of the run's own."""

    settings: Path
    """The file to hand claude's ``--settings``."""

    ready: Path
    """The marker the ``SessionStart`` hook writes; it exists once claude is ready."""

    socket: Path | None = None
    """Where the run's prompt bridge listens; ``None`` when the run has none."""


def prepare(
    *,
    profile: str | None,
    boxes: bool = True,
    user: Mapping[str, PlainTree] | None = None,
    bridge: bool = False,
) -> HookFiles:
    """Write the settings file for one claude run.

    Args:
      profile: The ``--profile`` the run was started with, if any.
      boxes: Whether the run is placed on a station, so its prompts are told of the
        boxes drawn there.
      user: The settings the person passed to ``--settings``, merged in, since
        claude takes one; ``None`` when they passed none.
      bridge: Whether the run bridges claude's prompts to Chat, so its hooks hand
        their events to a socket in the run's directory.

    Returns:
      files: The settings file, in a fresh directory of this run's own, which also
        holds the last box told, the readiness marker and the bridge's socket.

    """
    root = state_dir() / "rekursiv-ai" / "trax" / "run" / "box-hooks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = root / uuid.uuid4().hex
    run_dir.mkdir(mode=0o700)
    seen = run_dir / "seen"
    settings = run_dir / "settings.json"
    # Inside the 0700 directory: whoever can connect can answer a prompt. A state dir
    # too deep for a socket address is reached through a short alias of the directory.
    socket = socket_address(run_dir / "bridge.sock") if bridge else None
    _ = settings.write_text(
        settings_json(
            seen=seen,
            profile=profile,
            boxes=boxes,
            user=user,
            socket=socket,
        ),
    )
    return HookFiles(settings=settings, ready=ready_marker(seen), socket=socket)


def ready_marker(seen: Path) -> Path:
    """Return the marker the ``SessionStart`` hook writes beside ``seen``."""
    return seen.with_name("ready")


def read_settings(value: str) -> dict[str, PlainTree] | None:
    """Read what a person passed to claude's ``--settings``: a file, or JSON text.

    Args:
      value: The flag's value.

    Returns:
      settings: The settings, or ``None`` when they cannot be read or their hooks
        are not claude's shape, which leaves the person's flag as they gave it.

    """
    # Read as a file first; JSON text is no file, and a long one is no file NAME
    # either, which the filesystem refuses with an error of its own.
    try:
        text = Path(value).read_text()
    except OSError:
        text = value
    try:
        settings = json_loads(text, dict[str, PlainTree])
        _ = from_plain(settings.get("hooks"), dict[str, list[PlainTree]], default={})
    except ValueError:
        return None
    return settings


def settings_json(
    *,
    seen: Path,
    profile: str | None,
    boxes: bool = True,
    user: Mapping[str, PlainTree] | None = None,
    socket: Path | None = None,
) -> str:
    """Return claude's settings holding the hooks that run :func:`main`.

    Args:
      seen: Where the hook keeps the newest box it told this run; the readiness
        marker goes beside it.
      profile: The trax profile the hook reads the station with; ``None`` for the
        environment's, as ``trax`` itself resolves it.
      boxes: Whether to add the hook that tells each prompt of the station's boxes.
      user: Settings of the person's own to keep: every key, and every hook of
        theirs before ours on the same event.
      socket: The prompt bridge's socket, which the bridged events are handed to;
        ``None`` adds none of them.

    Returns:
      settings: The JSON text.

    """
    # This package's own name, so an export that renames the package still runs it.
    argv = [sys.executable, "-m", __name__.rpartition(".")[0], "--seen", str(seen)]
    ours = {"SessionStart": [_hook_entry([*argv, "--event", "session-start"])]}
    if boxes:
        prompt = argv if profile is None else [*argv, "--profile", profile]
        ours["UserPromptSubmit"] = [_hook_entry(prompt)]
    if socket is not None:
        bridged = [*argv, "--event", "bridge", "--socket", str(socket)]
        for event in _BRIDGED:
            # The one hook that waits on a person; the rest hold up a tool call.
            timeout = (
                _PROMPT_TIMEOUT_SEC
                if event == "PermissionRequest"
                else _HOOK_TIMEOUT_SEC
            )
            ours[event] = [_hook_entry(bridged, timeout=timeout)]
    kept = dict(user or {})
    theirs = from_plain(kept.get("hooks"), dict[str, list[PlainTree]], default={})
    kept["hooks"] = {
        **theirs,
        **{
            event: [*theirs.get(event, []), *entries] for event, entries in ours.items()
        },
    }
    return json.dumps(kept)


def render_boxes(state: dict[str, PlainTree], *, after: int) -> tuple[str, int]:
    """Word the boxes of ``state`` drawn after the one numbered ``after``.

    Args:
      state: The station as the server returns it.
      after: The newest box already told.

    Returns:
      told: The text to print, empty when nothing is new.
      newest: The newest box number seen, ``after`` when there is none.

    """
    labels = from_plain(state.get("record_labels"), dict[str, str], default={})
    visuals = {
        from_plain(v.get("id"), str, default=""): from_plain(
            v.get("type"),
            str,
            default="",
        )
        for v in from_plain(
            state.get("visuals"),
            list[dict[str, PlainTree]],
            default=[],
        )
    }
    marks = [
        mark
        for mark in from_plain(
            state.get("marks"),
            list[dict[str, PlainTree]],
            default=[],
        )
        if from_plain(mark.get("seq"), int, default=0) > after
    ]
    newest = max([after, *(from_plain(m.get("seq"), int, default=0) for m in marks)])
    boxes: list[Box] = []
    for mark in marks:
        tile = from_plain(mark.get("tile"), str, default="")
        if tile and tile not in visuals:
            continue
        covers = from_plain(mark.get("covers"), dict[str, list[str]], default={})
        # A label is a title people wrote, so it is quoted as data.
        named = [
            json.dumps(labels[r]) for r in covers.get("records", []) if r in labels
        ]
        named += [
            f"{visuals[each]} {each}"
            for each in covers.get("instances", [])
            if each in visuals
        ]
        boxes.append(
            Box(
                id=from_plain(mark.get("id"), str, default=""),
                author=from_plain(mark.get("author"), str, default=""),
                page=from_plain(mark.get("page"), str, default="") or None,
                covers=tuple(named),
            ),
        )
    if not boxes:
        return "", newest
    return render_box_lines(boxes[-_BOXES_PER_PROMPT:]).removeprefix("\n"), newest


def main(
    argv: Sequence[str],
    *,
    connect: Callable[[Namespace], Client],
) -> int:
    """Run the hook for one event; always exit 0.

    On ``SessionStart`` it writes the readiness marker. Before a prompt it prints
    the boxes drawn since this run last heard of one. A bridged event goes to the
    run's prompt bridge, and what the bridge replies is printed for claude.

    Args:
      argv: ``--seen FILE``, and optionally ``--profile NAME``, ``--event`` and
        ``--socket PATH``.
      connect: Builds the client from the flags and environment, as ``trax`` does.

    Returns:
      status: 0, whatever happened: a prompt is never held up by this.

    """
    parser = ArgumentParser()
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args(argv))
    if flags.event == "bridge":
        reply = (
            ""
            if flags.socket is None
            else prompt_bridge.ask(flags.socket, sys.stdin.read())
        )
        sys.stdout.write(reply)
        sys.stdout.flush()
        return 0
    if flags.event == "session-start":
        # A marker the run cannot write leaves it to its fallback wait, which is
        # all a failure here may cost.
        with contextlib.suppress(OSError):
            ready_marker(flags.seen).touch()
        return 0
    station = workspace_from_env(os.environ)
    if station is None:
        return 0
    found: list[dict[str, PlainTree]] = []
    reader = threading.Thread(
        target=_read_station,
        name="box-hook-read",
        kwargs={
            "connect": connect,
            "profile": flags.profile,
            "station": station,
            "found": found,
        },
        daemon=True,
    )
    reader.start()
    reader.join(_BUDGET_SEC)
    if not found:
        return 0
    try:
        after = int(flags.seen.read_text())
    except (OSError, ValueError):
        after = 0
    text, newest = render_boxes(found[0], after=after)
    if text:
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()
        _ = flags.seen.write_text(str(newest))
    return 0


def _read_station(
    *,
    connect: Callable[[Namespace], Client],
    profile: str | None,
    station: uuid.UUID,
    found: list[dict[str, PlainTree]],
) -> None:
    """Append the station's state to ``found``; any failure leaves it empty."""
    # Nothing here may hold up the person's prompt, whatever fails.
    with contextlib.suppress(Exception):
        client = connect(Namespace(profile=profile, host=None, port=None))
        found.append(client.read_workspace(station))


def _hook_entry(argv: list[str], *, timeout: int = _HOOK_TIMEOUT_SEC) -> PlainTree:
    """Return one settings entry running ``argv`` as a command hook."""
    return {
        "hooks": [
            {
                "type": "command",
                "command": shlex.join(argv),
                "timeout": timeout,
            },
        ],
    }


class _Flags(Protocol):
    """Parsed command-line flags."""

    seen: Path
    profile: str | None
    event: Literal["prompt", "session-start", "bridge"]
    socket: Path | None


def _add_arguments(parser: ArgumentParser) -> None:
    """Register flags on ``parser``."""
    _ = parser.add_argument(
        "--seen",
        type=Path,
        required=True,
        help="File holding the newest box already told.",
    )
    _ = parser.add_argument(
        "--profile",
        default=None,
        help="Trax profile to read with.",
    )
    _ = parser.add_argument(
        "--event",
        choices=("prompt", "session-start", "bridge"),
        default="prompt",
        help="The claude hook event this runs for; bridge hands it to --socket.",
    )
    _ = parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="The run's prompt bridge, which a bridge event goes to.",
    )
