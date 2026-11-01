"""What a session's records say of it: its goal, its first person's line, its last line.

The session list (``session_list.py``) names a person's own sessions by these, and a
chat's head (``science_chats.py``) carries them to the chat's other readers, so a chat
goes by the same words wherever it shows. Nothing here calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import uuid

from pydantic import BaseModel
from treekle import from_plain


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn


__all__ = ["LINE_CHARS", "LastLine", "SessionLines", "session_lines"]


LINE_CHARS: Final = 200
"""The most characters of a first line, a last line or a goal a read carries."""


class LastLine(BaseModel):
    """The last line said in a session, a person's or the agent's, on one line."""

    author: str | None
    """The person who said it: the one Chat delivered it from, else the session's
    account, who typed it in its CLI; None for the agent's answer."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionLines:
    """What a session's records say of it: its goal, its first person's line, its last."""

    goal: str | None = None
    first_line: str | None = None
    last_line: LastLine | None = None


# A CLI writes user turns no person typed, the ones ``client/conversation.py`` calls
# its harness's; the first line and the last pass over them, and the last over a turn
# with no words, as an answer that only called tools.
# The goal is the condition claude's ``goal_status`` attachment names, or the objective
# codex's ``thread_goal_updated`` event names: the fields the agent runner normalises.
# Audience: gated -- reads only the sessions its caller found the reader reads.
_LINES: Final = (
    "SELECT s.id, person.content, person.room, person.source, goal.text AS goal, "
    "last.kind AS last_kind, last.content AS last_content, last.sender AS last_sender, "
    "last.room AS last_room, last.source AS last_source "
    "FROM unnest($1::uuid[]) AS s(id) LEFT JOIN LATERAL ("
    "SELECT r.kind, r.payload->>'content' AS content, "
    "r.payload->'extra'->'$inbound'->>'room' AS room, "
    "r.payload->'extra'->'$inbound'->>'source' AS source "
    "FROM session_records AS r "
    "JOIN session_manifests AS m ON m.session_id = r.session_id AND m.part = r.part "
    "WHERE r.session_id = s.id AND r.idx < m.records "
    "AND r.kind IN ('UserMessage', 'AgentToAgentMessage') "
    "AND r.text ~ '\\S' AND r.text !~ "
    "'^\\s*(<|# AGENTS\\.md instructions|Caveat: The messages below were generated)' "
    "ORDER BY r.part, r.idx LIMIT 1) AS person ON TRUE LEFT JOIN LATERAL ("
    "SELECT said.text FROM session_records AS r "
    "JOIN session_manifests AS m ON m.session_id = r.session_id AND m.part = r.part "
    "CROSS JOIN LATERAL (SELECT coalesce("
    "nullif(r.payload->'extra'->'attachment'->>'condition', ''), "
    "nullif(r.payload->'payload'->'goal'->>'objective', '')) AS text) AS said "
    "WHERE r.session_id = s.id AND r.idx < m.records AND said.text IS NOT NULL "
    "AND ((r.kind = 'ContextState' AND r.payload->>'kind' = 'goal_status') "
    "OR (r.kind = 'UncategorizedRecord' "
    "AND r.payload->>'kind' = 'event_msg/thread_goal_updated')) "
    "ORDER BY r.part DESC, r.idx DESC LIMIT 1) AS goal ON TRUE LEFT JOIN LATERAL ("
    "SELECT r.kind, r.payload->>'content' AS content, r.payload->>'sender' AS sender, "
    "r.payload->'extra'->'$inbound'->>'room' AS room, "
    "r.payload->'extra'->'$inbound'->>'source' AS source "
    "FROM session_records AS r "
    "JOIN session_manifests AS m ON m.session_id = r.session_id AND m.part = r.part "
    "WHERE r.session_id = s.id AND r.idx < m.records "
    "AND r.kind IN ('UserMessage', 'AgentToAgentMessage', 'AssistantMessage') "
    "AND r.text ~ '\\S' AND (r.kind = 'AssistantMessage' OR r.text !~ "
    "'^\\s*(<|# AGENTS\\.md instructions|Caveat: The messages below were generated)') "
    "ORDER BY r.part DESC, r.idx DESC LIMIT 1) AS last ON TRUE"
)


# Audience: gated -- reads only the sessions its caller found the reader reads.
async def session_lines(
    conn: Conn,
    *,
    session_ids: Sequence[uuid.UUID],
    account: str,
) -> dict[uuid.UUID, SessionLines]:
    """Read each session's goal, the first line a person wrote in it, and its last.

    Args:
      conn: A database connection.
      session_ids: The sessions, each one the caller found the reader reads.
      account: The sessions' own account, who typed in their CLIs whatever no one
        delivered from Chat: the last line's author when no sender is named.

    Returns:
      lines: What each session's records say of it, by its id; one per session.

    """
    rows = await conn.fetch(_LINES, list(session_ids))
    return {
        from_plain(row["id"], uuid.UUID): SessionLines(
            goal=_line(from_plain(row["goal"], str, default="")),
            first_line=_line(_delivered(row, prefix="")),
            last_line=_last_line(row, account=account),
        )
        for row in rows
    }


def _last_line(row: Record, *, account: str) -> LastLine | None:
    """Return the session's last line, on one line and cut short, and who said it."""
    kind = from_plain(row["last_kind"], str, default="")
    if not kind:
        return None
    if kind == "AssistantMessage":
        author = None
        said = from_plain(row["last_content"], str, default="")
    else:
        author = (
            from_plain(row["last_sender"], str, default="")
            or from_plain(row["last_source"], str, default="")
            or account
        )
        said = _delivered(row, prefix="last_")
    return LastLine(author=author, text=" ".join(said.split())[:LINE_CHARS])


# A line Chat delivered reads ``[room] sender: words``, then a trailer of context the
# agent verifies: what ``client/conversation.py::delivered_text`` takes off too.
# ``prefix`` names the line's columns: ``""`` for the first line's, ``last_`` for the
# last line's.
def _delivered(row: Record, *, prefix: str) -> str:
    """Return what a person wrote, unwrapped from how the agent runner delivered it."""
    room = from_plain(row[f"{prefix}room"], str, default="")
    source = from_plain(row[f"{prefix}source"], str, default="")
    return (
        from_plain(row[f"{prefix}content"], str, default="")
        .removeprefix(f"[{room}] " if room else "")
        .removeprefix(f"{source}: " if source else "")
        .split(_TRAILER, 1)[0]
    )


_TRAILER: Final = "\nTrackinizer context (verify with trax): "
"""Where a delivered line's context trailer begins (``client/conversation.py``)."""


def _line(text: str) -> str | None:
    """Return the first line of ``text`` with words, cut short; None for none."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:LINE_CHARS] or None
