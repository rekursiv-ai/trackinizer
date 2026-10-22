"""How a server-delivered message reads to the agent that receives it.

Used by ``trax run``, which types it into the CLI's terminal, or hands it to the
driver of a headless Chat run. Kept apart from the PTY machinery so a reader of messages
does not import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, overload

import json

from treekle import from_plain, json_loads


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.wire.wire_sessions import (
        InboundKind,
        WorkspaceMark,
        WorkspaceMessageContext,
    )


# What the run's start note tells the CLI about moving the canvas, once. One line with
# no double quote or backslash, so it is a TOML string as it stands, for codex's ``-c``.
# The line after a compaction carries it again: the CLI may have lost the note.
CANVAS_COMMANDS: Final = (
    "Canvas: run these commands; describing them does nothing. "
    "`trax workspace` acts on your station, $TRAX_WORKSPACE (a line's Trackinizer "
    "context names it as workspace_id). RECORD_UUID is the record on screen "
    "(page.record.id in that context) or any record's uuid. "
    "Show a record: trax workspace navigate '#/lookup/RECORD_UUID'. "
    "Show its graph: trax workspace show trax.subgraph --record RECORD_UUID "
    "--placement side. "
    "Show its timeline: trax workspace show trax.timeline --record RECORD_UUID. "
    "Show an artifact: trax workspace show trax.artifact --record RECORD_UUID "
    "--placement main. "
    "Show its files and diffs: trax workspace show trax.files --record RECORD_UUID "
    "(narrow: --param path=DIR/ or a glob, --param changed=true, --param file=NAME). "
    "Open a terminal on your machine: trax workspace show trax.terminal "
    "--param machine=NAME --param start=true. "
    "Point at records: trax workspace highlight UUID[,UUID...] (highlight '' clears). "
    "Go to a view: trax workspace navigate '#/list/Issue' (or '#/graph', "
    "'#/activity', '#/console', '#/search/TEXT' with TEXT percent-encoded). "
    "Hide your Chat window so they look at the page: trax workspace hide chat "
    "(show chat brings it back). "
    "Hide a visual: trax workspace hide INSTANCE_UUID (never the page)."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Disclosure:
    """What a person's line says beyond their words.

    The default says everything but the canvas commands, which the run's start note
    holds. A line reads as it would typed at the CLI: the run says more only when the
    CLI could not know it (see ``InboundState.disclosure``).
    """

    sender: bool = True
    """Whether the line names its sender, which the CLI tells speakers apart by."""

    context: bool = True
    """Whether the line carries the Trackinizer context: it is the first, or changed."""

    canvas: bool = False
    """Whether the line repeats :data:`CANVAS_COMMANDS`: the CLI may have lost them."""


# A single PTY interleaves every room's messages into one input stream, so the agent
# needs the room and sender to know who is steering it. Renders ``[room] sender: text``
# (dropping whichever of room/sender is absent), so a direct session-id enqueue with no
# attested sender injects the bare text.
#
# Change envelopes are shaped per consumer HERE, at the client -- the server pushes one
# uniform JSON envelope to every session. A model-CLI session (``stream=False``)
# receives only the envelope's ``agent_message`` line: the remaining fields would spend
# the model's context on metadata it can fetch on demand (the line itself names the
# ``trax`` command). An IO-stream session (``stream=True``) receives the whole envelope
# to parse itself -- behind the same room/sender prefix as any other message, since a
# line-reading child needs to know who sent it just as much. Only the route-attested
# ``trackinizer`` sender unwraps -- ``source`` is stamped server-side from the
# principal, so another sender's JSON-looking text renders as a plain message.
#
# Only a person's line carries the prefix. A slash command or a prompt's answer is
# typed bare: with ``sender: `` before it, ``/compact`` reaches the model as a prompt
# and never runs as a command.
#
# A Chat line reads as one typed at the CLI (see :class:`Disclosure`): the canvas commands
# are in the run's start note, the context goes only when it changed, and the sender is
# named only when it tells speakers apart. Marks ride with their line every time, after
# the context, which readers of a transcript cut them off by.
@overload
def render_inbound(
    text: str,
    source: str | None,
    room: str | None,
    *,
    context: WorkspaceMessageContext | None = None,
    stream: bool = False,
    kind: Literal["line"] = "line",
    disclosure: Disclosure | None = None,
) -> str: ...


@overload
def render_inbound(
    text: str,
    source: str | None,
    room: str | None,
    *,
    context: WorkspaceMessageContext | None = None,
    stream: bool = False,
    kind: InboundKind,
    disclosure: Disclosure | None = None,
) -> str | bytes | None: ...


def render_inbound(
    text: str,
    source: str | None,
    room: str | None,
    *,
    context: WorkspaceMessageContext | None = None,
    stream: bool = False,
    kind: InboundKind = "line",
    disclosure: Disclosure | None = None,
) -> str | bytes | None:
    """Decorate an inbound message with its routing context for injection.

    Args:
      text: The message as sent.
      source: The route-attested sender, if any.
      room: The room it was sent to, if any.
      context: The canvas it was sent from, for a Chat message.
      stream: Whether the receiver is an IO-stream child rather than a model CLI.
      kind: What the message is; see
        :data:`~trackinizer.wire.wire_sessions.InboundKind`.
      disclosure: What of the context, the canvas commands and the sender the line
        carries; see :class:`Disclosure`.

    Returns:
      rendered: Text to type and submit with Enter; raw keys to send as they are
        (an interrupt is Escape); ``None`` for a kind that is not typed at all (a
        settings change).

    """
    match kind:
        case "command" | "answer":
            return text
        case "settings":
            return None
        case "interrupt":
            return b"\x1b"
        case "line":
            pass
    if context is None and source == "trackinizer" and not stream:
        agent_message = _envelope_agent_message(text)
        if agent_message is not None:
            return agent_message
    prefix = ""
    if scope := room or "":
        prefix += f"[{scope}] "
    sender = ""
    if named := source or "":
        sender = f"{named}: "
    if context is not None and context.relay is not None:
        # Told to agents, not asked of this one: no canvas to move, nothing to run.
        return (
            f"{prefix}{sender}[Console relay: told to these agents directly, not a "
            f"question for you; do not contradict it] {context.relay.line()}"
        )
    shown = disclosure or Disclosure()
    rendered = f"{prefix}{sender if shown.sender else ''}{text}"
    if context is not None:
        if shown.context:
            rendered += (
                f"\nTrackinizer context (verify with trax): {context.model_dump_json()}"
            )
        if context.marks:
            rendered += _render_marks(context.marks)
        if shown.canvas:
            rendered += f"\n{CANVAS_COMMANDS}"
    return rendered


def settings_from_payload(payload: Mapping[str, object] | None) -> tuple[str, str]:
    """Read a ``settings`` message's payload.

    Args:
      payload: ``{"model": str | None, "effort": str | None}``, or ``None`` for none.

    Returns:
      settings: The model and the effort to change to; empty for one left alone.

    Raises:
      ValueError: The payload holds a field it has no place for, or one of the wrong
        type.

    """
    read = from_plain(dict(payload or {}), _Settings)
    return read.model or "", read.effort or ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Box:
    """One red box as the agent is told of it."""

    id: str
    author: str
    page: str | None
    covers: tuple[str, ...]
    """What lies under it, each already worded and quoted by whoever knew it."""


def render_box_lines(boxes: Sequence[Box]) -> str:
    """Word the boxes drawn since the agent's last line, one per line.

    Args:
      boxes: The boxes, each with what it covers already worded.

    Returns:
      text: A line of heading then one line per box, starting with a newline.

    """
    lines = [
        (
            "\nBoxes drawn on the canvas since your last line"
            " (`mark remove ID` takes one away):"
        ),
    ]
    lines += [
        f"  {box.id} by {box.author} on {box.page or 'no page'};"
        f" covers: {', '.join(box.covers) or 'nothing'}"
        for box in boxes
    ]
    return "\n".join(lines)


def _envelope_agent_message(text: str) -> str | None:
    """Return the ``agent_message`` line of a change envelope, or None if not one."""
    try:
        payload = json_loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message = from_plain(payload, dict[str, object]).get("agent_message")
    return message if isinstance(message, str) else None


# A note's words are what its author wrote on the canvas, so they are quoted as data,
# never read as a command.
# Titles are what people wrote on the records, so they are quoted as data.
def _render_marks(marks: Sequence[WorkspaceMark]) -> str:
    """List the boxes drawn since the last line, one per line, with what they cover."""
    return render_box_lines(
        [
            Box(
                id=str(mark.id),
                author=mark.author,
                page=mark.page,
                covers=(
                    *(f"{r.kind}#{r.seq} {json.dumps(r.title)}" for r in mark.records),
                    *(f"{v.type} {v.id}" for v in mark.instances),
                ),
            )
            for mark in marks
        ],
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    """A ``settings`` message's payload: what Chat changes about the run."""

    model: str | None = None
    effort: str | None = None
