"""Tell a slash command typed in Chat from a line, by the provider that answers it.

A Chat post is typed into a ``claude`` or ``codex`` run's terminal. A line goes in
with its sender and the canvas, so the CLI reads ``/goal x`` there as a prompt. A
command goes in bare, so the CLI runs it as one of its own built-ins. The server
classifies each line with :func:`classify` before it queues it: a command is queued
as kind ``command``, a refusal is answered at once and nothing is queued, and any
other text stays a line.

Only a known built-in of either CLI (:data:`KNOWN_VERBS`) at the very start of the
text is a command, so ``/etc/hosts is broken`` and ``/tmp is full`` stay lines. Of
those, Chat runs only the ones that need no terminal: ``/goal`` and claude's
``/compact``. Every other one opens a menu, changes settings or switches the
session, which the person does at the terminal.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import re


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.wire.wire_science_chat import ChatProvider


__all__ = [
    "COMMAND_CHARS",
    "KNOWN_VERBS",
    "Classified",
    "Command",
    "Line",
    "Refused",
    "classify",
]


COMMAND_CHARS: Final = 800
"""The longest command Chat types. claude turns a longer paste into a
``[Pasted text #N]`` pill and then does not run it as a command; codex does the same
over 1,000."""

KNOWN_VERBS: Final = frozenset(
    {
        "goal",
        "compact",
        "clear",
        "new",
        "model",
        "effort",
        "permissions",
        "resume",
        "status",
        "config",
        "exit",
        "quit",
        "init",
        "review",
        "plan",
        "fork",
        "rename",
        "diff",
        "mcp",
        "agents",
        "cost",
        "help",
        "hooks",
        "skills",
        "usage",
        "export",
        "copy",
        "theme",
        "vim",
        "ide",
        "side",
        "btw",
        "rewind",
        "undo",
        "cd",
        "pwd",
        "stop",
        "ps",
        "login",
        "logout",
        "memory",
        "context",
        "fast",
        "doctor",
        "feedback",
        "statusline",
    },
)
"""The built-in slash commands of claude and codex. A custom command or skill is sent
as a line."""


_COMMAND: Final = re.compile(
    r"/(?P<verb>[A-Za-z][A-Za-z0-9_-]*)(?:[ \t]+(?P<args>.*))?",
)
"""A command's line: a slash, its verb, then its arguments after a space or tab."""

_GOAL_CLEARS: Final = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})
"""The words claude clears a goal with; codex knows ``clear`` alone."""

_MENU: Final = (
    "Chat cannot run /{verb}: it opens a menu, changes settings or switches the "
    "session. Use the terminal: trax run attach <run>"
)

_GOAL_BARE: Final = "Chat shows the goal above the box; /goal clear stops it"
_GOAL_EDIT: Final = (
    "Chat cannot run /goal edit: it opens an editor. Use the terminal: "
    "trax run attach <run>"
)

_TABLE: Final[Mapping[str, Mapping[ChatProvider, str | None]]] = {
    "goal": {"claude": None, "codex": None},
    # Claude opens a modal panel for a bare /goal.
    "goal bare": {"claude": _GOAL_BARE, "codex": _GOAL_BARE},
    "goal clear": {"claude": None, "codex": None},
    # Claude would take the word as the goal's condition.
    "goal pause": {
        "claude": "claude has no goal pause; /goal clear stops it",
        "codex": None,
    },
    "goal edit": {"claude": _GOAL_EDIT, "codex": _GOAL_EDIT},
    # Claude queues /compact until its turn ends; codex refuses it during a turn.
    "compact": {
        "claude": None,
        "codex": "codex runs /compact only between turns; attach to run it",
    },
}
"""What each CLI does with each form of a command Chat may run: ``None`` types it, and
text refuses it. A known verb with no row is refused as :data:`_MENU`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Line:
    """Text that is a person's line, delivered with its sender and the canvas."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Command:
    """A slash command the CLI runs, typed bare."""

    text: str
    """What is typed: the verb lowercased, then its arguments, as ``/goal ship it``."""

    verb: str
    args: str
    """Everything after the verb, trimmed; empty for none."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Refused:
    """A known command Chat does not send to this provider, and why, for the person."""

    reason: str


type Classified = Line | Command | Refused
"""What :func:`classify` makes of a typed text."""


def classify(text: str, provider: ChatProvider) -> Classified:
    """Say whether ``text`` typed in a chat answered by ``provider`` is a command.

    Args:
      text: What the person typed.
      provider: Who answers the chat: the chat's own provider, not the poster's.

    Returns:
      classified: ``Command`` for a command the provider runs from Chat, ``Refused``
        for a known command it does not, and ``Line`` for any other text.

    """
    lines = text.rstrip().splitlines()
    first = _COMMAND.fullmatch(lines[0]) if lines else None
    if first is None or first["verb"].lower() not in KNOWN_VERBS:
        return Line()
    if provider == "assistant":
        return Refused(
            reason="The assistant runs no slash commands. /goal works in a chat on "
            "claude or codex: pick one in the provider card.",
        )
    if len(lines) > 1 or len(lines[0]) > COMMAND_CHARS:
        return Refused(
            reason=f"A command is one line of at most {COMMAND_CHARS} characters; "
            "put a longer goal in a file and write /goal follow <path>",
        )
    verb = first["verb"].lower()
    row, args = _row(verb, args=(first["args"] or "").strip())
    reason = _TABLE.get(row, {}).get(provider, _MENU.format(verb=verb))
    if reason is not None:
        return Refused(reason=reason)
    return Command(text=f"/{verb} {args}" if args else f"/{verb}", verb=verb, args=args)


def _row(verb: str, *, args: str) -> tuple[str, str]:
    """Return a command's row of the table, and its arguments as they are typed."""
    if verb != "goal":
        return verb, args
    word = args.lower()
    if not args:
        return "goal bare", args
    if word in _GOAL_CLEARS:
        return "goal clear", "clear"
    if word in {"pause", "resume"}:
        return "goal pause", word
    if word == "edit":
        return "goal edit", word
    return "goal", args
