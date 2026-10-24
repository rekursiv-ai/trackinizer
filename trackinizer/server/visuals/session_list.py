"""The caller's own sessions, newest activity first, each with its run and its place.

What a person reads to find their work: every session they started that was active
lately or is still open, with whether a run drains it now and on which machine (the
poller's lease, which the server holds in memory), when it last took a turn, the goal
its CLI's log last reported, the first line a person wrote in it, and the canvas and
workspace it sits in. The Issues the sessions were produced by come with the Issues
those narrow or were produced by, so a reader can nest a meta-campaign's campaigns
and their sessions from the rows' own edges. Nothing here calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import uuid

from pydantic import BaseModel
from treekle import from_plain

from trackinizer.server.audience import visible_sql
from trackinizer.server.values import vetted_sql
from trackinizer.server.visuals.partners import live_run_states
from trackinizer.wire.wire_science_chat import chat_conversation_id
from trackinizer.wire.wire_sessions import CANVAS_LABEL_PREFIX, RunState


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.inbound import InboundQueue


MAX_SESSIONS: Final = 500
"""The most sessions one read lists."""

MAX_DAYS: Final = 365
"""The widest window of activity one read looks back over."""

LINE_CHARS: Final = 200
"""The most characters of a first line or a goal a read carries."""


class ListedSession(BaseModel):
    """One of the caller's sessions, as the Console and the session switcher list it."""

    id: uuid.UUID
    seq: int
    title: str
    status: str
    cli: str | None
    created: datetime
    ended: datetime | None
    last_activity: datetime
    """When its last turn was written, or when it was made before any."""

    conversation_id: uuid.UUID | None
    """The chat it is a session of; None for a session that is no chat's."""

    canvas_id: uuid.UUID | None
    """The caller's canvas it sits in: its chat's, or the one it was placed in."""

    workspace_id: uuid.UUID | None
    """That canvas's workspace; None for Scratch, and for a session on no canvas."""

    produced_by: list[uuid.UUID]
    """The Issues it was produced by, workspaces left out, as ``issues`` lists them."""

    goal: str | None
    """The goal its CLI's log last reported, set, checked or met."""

    first_line: str | None
    """The first line a person wrote in it."""

    live: bool
    """A run drains it now: its poller's lease holds."""

    machine: str | None
    """The machine the live run names, else the one it last ran on since the server
    started; None when no run of it named one."""

    state: RunState
    """What the live run is doing; ``idle`` when no run is live."""


class ListedIssue(BaseModel):
    """An Issue a listed session was produced by, or one such an Issue narrows."""

    id: uuid.UUID
    seq: int
    title: str
    status: str
    parent_ids: list[uuid.UUID]
    """The Issues it narrows or was produced by, workspaces left out; only those
    a listed session was produced by have theirs read."""


class SessionList(BaseModel):
    """The caller's sessions, newest activity first, and the Issues above them."""

    sessions: list[ListedSession]
    issues: list[ListedIssue]
    more: bool
    """More sessions matched than the read's limit."""


async def list_own_sessions(
    engine: DatabaseEngine,
    *,
    email: str,
    user_id: uuid.UUID,
    reader: SessionReader,
    inbound: InboundQueue,
    days: int,
    limit: int,
) -> SessionList:
    """List the caller's sessions active within ``days`` or still open.

    Args:
      engine: Database connection source.
      email: The caller's account, which their sessions carry.
      user_id: The caller, whose canvases place the sessions.
      reader: The caller as the transcript test reads them; a session they do not
        read is left out.
      inbound: In-process poller leases, which say which runs are live and where.
      days: How far back the last activity may be, 1 to :data:`MAX_DAYS`.
      limit: The most sessions to list, 1 to :data:`MAX_SESSIONS`.

    Returns:
      listed: The sessions, newest activity first, and the Issues above them.

    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with engine.acquire() as conn:
        found = await _recent(
            conn,
            email=email,
            since=since,
            limit=limit + 1,
            reader=reader,
        )
        rows = found[:limit]
        ids = [from_plain(row["id"], uuid.UUID) for row in rows]
        lines = await _lines(conn, session_ids=ids)
        places = await _places(conn, user_id=user_id)
        produced = await _produced_by(conn, session_ids=ids)
        issues = await _issues(
            conn,
            issue_ids=sorted({i for v in produced.values() for i in v}),
        )
        states = await live_run_states(conn, inbound=inbound, session_ids=ids)
    return SessionList(
        sessions=[
            _listed(
                row,
                lines=lines.get(session_id, _Lines()),
                places=places,
                produced_by=produced.get(session_id, []),
                inbound=inbound,
                state=states.get(session_id),
            )
            for row, session_id in zip(rows, ids, strict=True)
        ],
        issues=issues,
        more=len(found) > limit,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Lines:
    """What a session's records say of it: its goal and its first person's line."""

    goal: str | None = None
    first_line: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Places:
    """The caller's canvases: the canvas of each chat, and the workspace of each."""

    by_chat: dict[uuid.UUID, uuid.UUID] = field(
        default_factory=dict[uuid.UUID, uuid.UUID],
    )
    workspace: dict[uuid.UUID, uuid.UUID | None] = field(
        default_factory=dict[uuid.UUID, uuid.UUID | None],
    )


# Audience: gated -- ``visible_sql`` keeps out every session the caller does not read,
# and the turn time read for each is of a session it keeps.
_RECENT: Final = (
    "SELECT s.id, s.seq, s.title, s.status, s.labels, s.created, "
    "s.agentsession_cli AS cli, s.agentsession_cli_session_id AS chat, "
    "s.agentsession_ended AS ended, coalesce(turn.at, s.created) AS active "
    "FROM inquiries AS s LEFT JOIN LATERAL ("
    "SELECT coalesce(r.timestamp, r.created) AS at FROM session_records AS r "
    "JOIN session_manifests AS m ON m.session_id = r.session_id AND m.part = r.part "
    "WHERE r.session_id = s.id AND r.idx < m.records "
    "AND r.kind IN ('UserMessage', 'AssistantMessage') "
    "ORDER BY r.part DESC, r.idx DESC LIMIT 1) AS turn ON TRUE "
    "WHERE s.kind = 'AgentSession' AND s.account = $1 "
    "AND ((s.status = 'active' AND s.agentsession_ended IS NULL) "
    "OR coalesce(turn.at, s.created) >= $2) AND "
)


# Audience: gated -- reads only the sessions ``_RECENT`` kept for the caller.
async def _recent(
    conn: Conn,
    *,
    email: str,
    since: datetime,
    limit: int,
    reader: SessionReader,
) -> list[Record]:
    """Read the caller's sessions active since ``since`` or still open, newest first."""
    params: list[object] = [email, since, limit]
    visible = visible_sql("s", params=params, reader=reader)
    return await conn.fetch(
        vetted_sql(_RECENT, visible, " ORDER BY active DESC, s.id DESC LIMIT $3"),
        *params,
    )


# A CLI writes user turns no person typed, the ones ``client/conversation.py`` calls
# its harness's; the first line passes over them.
# The goal is the condition claude's ``goal_status`` attachment names, or the objective
# codex's ``thread_goal_updated`` event names: the fields the agent runner normalises.
# Audience: gated -- reads only the sessions ``_RECENT`` kept for the caller.
_LINES: Final = (
    "SELECT s.id, person.kind, person.content, person.room, person.source, goal.text "
    "AS goal FROM unnest($1::uuid[]) AS s(id) LEFT JOIN LATERAL ("
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
    "ORDER BY r.part DESC, r.idx DESC LIMIT 1) AS goal ON TRUE"
)


# Audience: gated -- reads only the sessions ``_RECENT`` kept for the caller.
async def _lines(
    conn: Conn,
    *,
    session_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, _Lines]:
    """Read each session's goal and the first line a person wrote in it."""
    rows = await conn.fetch(_LINES, list(session_ids))
    return {
        from_plain(row["id"], uuid.UUID): _Lines(
            goal=_line(from_plain(row["goal"], str, default="")),
            first_line=_line(_delivered(row)),
        )
        for row in rows
    }


# A line Chat delivered reads ``[room] sender: words``, then a trailer of context the
# agent verifies: what ``client/conversation.py::delivered_text`` takes off too.
def _delivered(row: Record) -> str:
    """Return what a person wrote, unwrapped from how the agent runner delivered it."""
    room = from_plain(row["room"], str, default="")
    source = from_plain(row["source"], str, default="")
    return (
        from_plain(row["content"], str, default="")
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


async def _places(conn: Conn, *, user_id: uuid.UUID) -> _Places:
    """Read where the caller's canvases stand: the chat each holds, and its workspace."""
    rows = await conn.fetch(
        "SELECT id, metadata->>'conversation_id' AS chat, "
        "metadata->>'workspace_id' AS workspace FROM visual_canvases "
        "WHERE user_id = $1",
        user_id,
    )
    places = _Places()
    for row in rows:
        canvas = from_plain(row["id"], uuid.UUID)
        workspace = from_plain(row["workspace"], str, default=None)
        places.workspace[canvas] = None if workspace is None else uuid.UUID(workspace)
        chat = from_plain(row["chat"], str, default=None)
        if chat is not None:
            places.by_chat[uuid.UUID(chat)] = canvas
    return places


async def _produced_by(
    conn: Conn,
    *,
    session_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Read the Issues each session was produced by, workspaces left out, by seq."""
    rows = await conn.fetch(
        "SELECT e.from_id, e.to_id FROM edges AS e "
        "JOIN inquiries AS issue ON issue.id = e.to_id "
        "WHERE e.from_id = ANY($1::uuid[]) AND e.edge_kind = 'produced_by' "
        "AND e.to_kind = 'Issue' "
        "AND NOT coalesce(issue.labels, '{}') @> ARRAY['workspace']::text[] "
        "ORDER BY e.from_id, issue.seq, e.to_id",
        list(session_ids),
    )
    produced: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in rows:
        produced.setdefault(from_plain(row["from_id"], uuid.UUID), []).append(
            from_plain(row["to_id"], uuid.UUID),
        )
    return produced


async def _issues(conn: Conn, *, issue_ids: Sequence[uuid.UUID]) -> list[ListedIssue]:
    """Read ``issue_ids`` and the Issues each narrows or was produced by, by seq."""
    rows = await conn.fetch(
        "WITH up AS (SELECT DISTINCT e.from_id AS child, e.to_id AS parent "
        "FROM edges AS e JOIN inquiries AS parent ON parent.id = e.to_id "
        "WHERE e.from_id = ANY($1::uuid[]) AND e.from_kind = 'Issue' "
        "AND e.to_kind = 'Issue' AND e.edge_kind IN ('narrows', 'produced_by') "
        "AND NOT coalesce(parent.labels, '{}') @> ARRAY['workspace']::text[]) "
        "SELECT i.id, i.seq, i.title, i.status, coalesce(array_agg(up.parent "
        "ORDER BY up.parent) FILTER (WHERE up.parent IS NOT NULL), '{}') AS parents "
        "FROM inquiries AS i LEFT JOIN up ON up.child = i.id "
        "WHERE i.id = ANY($1::uuid[]) OR i.id IN (SELECT parent FROM up) "
        "GROUP BY i.id, i.seq, i.title, i.status ORDER BY i.seq, i.id",
        list(issue_ids),
    )
    return [_issue(row) for row in rows]


def _issue(row: Record) -> ListedIssue:
    """Build one listed Issue from its row."""
    return ListedIssue(
        id=from_plain(row["id"], uuid.UUID),
        seq=from_plain(row["seq"], int),
        title=from_plain(row["title"], str),
        status=from_plain(row["status"], str),
        parent_ids=from_plain(row["parents"], list[uuid.UUID]),
    )


def _listed(
    row: Record,
    *,
    lines: _Lines,
    places: _Places,
    produced_by: list[uuid.UUID],
    inbound: InboundQueue,
    state: RunState | None,
) -> ListedSession:
    """Build one listed session from its row and what was read beside it."""
    session_id = from_plain(row["id"], uuid.UUID)
    conversation = chat_conversation_id(from_plain(row["chat"], str, default=None))
    canvas = (
        places.by_chat.get(conversation)
        if conversation
        else _placed(row, places=places)
    )
    where = inbound.poller_run(session_id) or inbound.last_run(session_id)
    return ListedSession(
        id=session_id,
        seq=from_plain(row["seq"], int),
        title=from_plain(row["title"], str),
        status=from_plain(row["status"], str),
        cli=from_plain(row["cli"], str, default=None),
        created=from_plain(row["created"], datetime),
        ended=from_plain(row["ended"], datetime, default=None),
        last_activity=from_plain(row["active"], datetime),
        conversation_id=conversation,
        canvas_id=canvas,
        workspace_id=None if canvas is None else places.workspace[canvas],
        produced_by=produced_by,
        goal=lines.goal,
        first_line=lines.first_line,
        live=state is not None,
        machine=None if where is None else where.machine,
        state=state or "idle",
    )


def _placed(row: Record, *, places: _Places) -> uuid.UUID | None:
    """Return the caller's canvas a placed session's ``canvas:`` label names, if any."""
    for label in from_plain(row["labels"], list[str], default=[]):
        if label.startswith(CANVAS_LABEL_PREFIX):
            try:
                canvas = uuid.UUID(label.removeprefix(CANVAS_LABEL_PREFIX))
            except ValueError:
                continue
            if canvas in places.workspace:
                return canvas
    return None
