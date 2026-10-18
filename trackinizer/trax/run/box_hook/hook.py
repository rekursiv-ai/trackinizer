"""Tell a ``trax run`` agent about the boxes the person drew on its station.

Claude Code runs the hook before each prompt the person types and adds what it prints
to that prompt's context. The person types into the CLI, never through the server, so
this is the only place the agent can hear of a box: the same words, and the same rule
(once, the first time after it is drawn), that a Chat partner follows.

The hook never gets in the way of a prompt: any failure, or a server slower than
:data:`_BUDGET_SEC`, prints nothing and exits 0.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

import contextlib
import json
import os
import shlex
import sys
import threading
import uuid

from treekle import PlainTree, from_plain

from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.context import workspace_from_env
from trackinizer.trax.run.inbound import Box, render_box_lines


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trackinizer.client.client import Client


_BUDGET_SEC: Final = 2.0
"""How long the hook waits for the server before it says nothing."""

_BOXES_PER_PROMPT: Final = 20
"""The newest this many are listed, as with a Chat line."""

_HOOK_TIMEOUT_SEC: Final = 4
"""Claude's own cap on the command, above :data:`_BUDGET_SEC` for starting python."""


def prepare(*, profile: str | None) -> Path:
    """Write the settings file for one run placed on a station.

    Args:
      profile: The ``--profile`` the run was started with, if any.

    Returns:
      settings: The file to hand claude's ``--settings``, in a fresh directory of this
        run's own, which also holds the last box told.

    """
    root = state_dir() / "rekursiv-ai" / "trax" / "run" / "box-hooks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = root / uuid.uuid4().hex
    run_dir.mkdir(mode=0o700)
    settings = run_dir / "settings.json"
    _ = settings.write_text(settings_json(seen=run_dir / "seen", profile=profile))
    return settings


def settings_json(*, seen: Path, profile: str | None) -> str:
    """Return claude's settings holding the hook that runs :func:`main`.

    Args:
      seen: Where the hook keeps the newest box it told this run.
      profile: The trax profile the hook reads the station with; ``None`` for the
        environment's, as ``trax`` itself resolves it.

    Returns:
      settings: The JSON text.

    """
    # This package's own name, so an export that renames the package still runs it.
    argv = [sys.executable, "-m", __name__.rpartition(".")[0], "--seen", str(seen)]
    if profile is not None:
        argv += ["--profile", profile]
    return json.dumps(
        {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": shlex.join(argv),
                                "timeout": _HOOK_TIMEOUT_SEC,
                            },
                        ],
                    },
                ],
            },
        },
    )


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
    """Print the boxes drawn since this run last heard of one; always exit 0.

    Args:
      argv: ``--seen FILE`` and optionally ``--profile NAME``.
      connect: Builds the client from the flags and environment, as ``trax`` does.

    Returns:
      status: 0, whatever happened: a prompt is never held up by this.

    """
    parser = ArgumentParser()
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args(argv))
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


class _Flags(Protocol):
    """Parsed command-line flags."""

    seen: Path
    profile: str | None


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
