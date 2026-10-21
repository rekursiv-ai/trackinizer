"""The slash commands Chat sends a ``trax run`` CLI, and the goal the CLI follows.

The server decides what is a command (it knows which provider answers the chat), so a
``command`` item reaches the run already classified and is typed bare: with a sender
before it, the CLI reads ``/goal ship it`` as a prompt. No CLI writes a record of a
command it ran, so the run writes one of its own (:func:`command_record`) on its part,
for Chat to show the command and settle the line it sent.

:class:`GoalWatch` follows the goal each CLI logs, so a codex ``/goal`` that would
replace an open one is refused rather than typed: codex asks "Replace goal?" in a modal,
and the next line Chat types would answer it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

import threading

from treekle import PlainTree, from_plain

from trackinizer.lib.agent.types.sessions import (
    ContextState,
    SystemMessage,
    UncategorizedRecord,
)
from trackinizer.trax.run.slash import SlashCommand


if TYPE_CHECKING:
    from trackinizer.types.streams import TraxRecord


__all__ = [
    "CommandState",
    "GoalWatch",
    "command_record",
    "parse_command",
    "replaces_goal",
]


type CommandState = Literal["typed", "refused"]


# The words codex's ``/goal`` takes for itself rather than as an objective. The server
# types every word that stops a goal as ``clear``, so no other one arrives here.
_GOAL_WORDS: Final = frozenset({"clear", "edit", "pause", "resume"})

# Codex goal statuses under which the goal is still being followed. ``complete`` and
# ``budget_limited`` end it; a cleared goal is logged nowhere.
_CODEX_OPEN: Final = frozenset({"active", "paused", "blocked", "usage_limited"})


def parse_command(text: str) -> SlashCommand:
    """Return the verb and arguments of a command's text, as ``/goal ship it`` spells.

    Args:
      text: The command as the server sent it, starting with ``/``.

    Returns:
      command: The lowercased verb without its slash, and the rest of the line.

    """
    verb, _, args = text.strip().removeprefix("/").partition(" ")
    return SlashCommand(command=verb.lower(), args=args.strip())


def replaces_goal(command: SlashCommand) -> bool:
    """Whether ``command`` sets a new goal, which replaces any goal still open."""
    return (
        command.command == "goal"
        and bool(command.args)
        and command.args.lower() not in _GOAL_WORDS
    )


def command_record(
    text: str,
    *,
    source: str | None,
    room: str | None,
    state: CommandState,
    reason: str | None = None,
    at: datetime | None = None,
) -> SystemMessage:
    """Return the record of a command the run typed into its CLI, or refused to.

    Args:
      text: The command as it was sent.
      source: Who sent it, as the route attested; ``trax run`` for one the run sent.
      room: The room it was sent to, if any.
      state: Whether it was typed or refused.
      reason: Why it was refused, or why the run sent it.
      at: When; now when omitted.

    Returns:
      record: A ``command`` system message whose ``extra["$command"]`` says all of it.

    """
    command = parse_command(text)
    return SystemMessage(
        timestamp=(at or datetime.now(UTC)).isoformat(),
        subtype="command",
        content=text,
        extra={
            "$command": {
                "text": text,
                "verb": command.command,
                "args": command.args,
                "source": source,
                "room": room,
                "state": state,
                "reason": reason,
            },
        },
    )


class GoalWatch:
    """Whether the CLI follows a goal, as its own log says.

    Fed every record the run captures. Claude logs ``goal_status`` attachments: a
    sentinel that is not met opens a goal, and one that is met or failed closes it.
    Codex logs ``thread_goal_updated`` events with the goal's status, but nothing when
    a goal is cleared, so the run closes it itself when it types ``/goal clear``.
    """

    def __init__(self) -> None:
        self._open = threading.Event()

    @property
    def open(self) -> bool:
        """Whether a goal is being followed."""
        return self._open.is_set()

    def observe(self, record: TraxRecord) -> None:
        """Follow the goal ``record`` reports, if it reports one.

        Args:
          record: A record the CLI wrote, as the run captured it.

        """
        match record:
            case ContextState(kind="goal_status"):
                state = from_plain(
                    record.extra.get("attachment"),
                    dict[str, PlainTree],
                    default={},
                )
                if state.get("met") is True or state.get("failed") is True:
                    self._open.clear()
                elif state.get("sentinel") is True:
                    self._open.set()
            case UncategorizedRecord(kind="event_msg/thread_goal_updated"):
                goal = from_plain(
                    record.payload.get("goal"),
                    dict[str, PlainTree],
                    default={},
                )
                status = from_plain(goal.get("status"), str, default="")
                if status in _CODEX_OPEN:
                    self._open.set()
                elif status:
                    self._open.clear()
            case _:
                pass

    def cleared(self) -> None:
        """Note that ``/goal clear`` was typed, which codex logs nowhere."""
        self._open.clear()
