"""The caller's own sessions, newest activity first, each with its run and its place.

What a person reads to find their work: every session they started that was active
lately or is still open, with whether a run drains it now and on which machine (the
poller's lease, which the server holds in memory), when it last took a turn, the goal
its CLI's log last reported, the first line a person wrote in it, the last line said in
it and by whom, and the canvas and workspace it sits in. The Issues the sessions were
produced by come with the Issues those narrow or were produced by, so a reader can
nest a meta-campaign's campaigns and their sessions from the rows' own edges. Nothing
here calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import uuid

from pydantic import BaseModel
from treekle import from_plain

from trackinizer.server.access_policy import inquiry_read_filter
from trackinizer.server.audience import visible_sql
from trackinizer.server.values import vetted_sql
from trackinizer.server.visuals.session_lines import (
    LastLine,
    SessionLines,
    session_lines,
)
from trackinizer.server.visuals.session_runs import live_run_states
from trackinizer.wire.wire_science_chat import chat_conversation_id
from trackinizer.wire.wire_sessions import CANVAS_LABEL_PREFIX, RunState


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.access_policy import AccessPolicy, Principal
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.inbound import InboundQueue


MAX_SESSIONS: Final = 500
"""The most sessions one read lists."""

MAX_DAYS: Final = 365
"""The widest window of activity one read looks back over."""


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
    """That canvas's workspace; None for one in none, which is the default
    workspace's, and for a session on no canvas."""

    produced_by: list[uuid.UUID]
    """The Issues it was produced by, workspaces left out, as ``issues`` lists them."""

    goal: str | None
    """The goal its CLI's log last reported, set, checked or met."""

    first_line: str | None
    """The first line a person wrote in it."""

    last_line: LastLine | None
    """The last line said in it, tool calls and their output left out."""

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
    reader: SessionReader,
    inbound: InboundQueue,
    days: int,
    limit: int,
    policy: AccessPolicy | None = None,
    principal: Principal | None = None,
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
      policy: Deployment policy; None preserves trusted reads.
      principal: The caller, as the access policy reads them: an Issue they may not
        read is not listed. None preserves trusted reads.

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
        lines = await session_lines(conn, session_ids=ids, account=email)
        places = _Places()
        produced = await _produced_by(
            conn,
            session_ids=ids,
            policy=policy,
            principal=principal,
        )
        issues = await _issues(
            conn,
            issue_ids=sorted({i for v in produced.values() for i in v}),
            policy=policy,
            principal=principal,
        )
        states = await live_run_states(conn, inbound=inbound, session_ids=ids)
    return SessionList(
        sessions=[
            _listed(
                row,
                lines=lines.get(session_id, SessionLines()),
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
class _Places:
    """The caller's canvases: the canvas of each chat, and the workspace of each.

    None on a server whose app keeps no canvases, so every session is on none.
    """

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


async def _produced_by(
    conn: Conn,
    *,
    session_ids: Sequence[uuid.UUID],
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Read the readable Issues each session was produced by, workspaces left out."""
    params: list[object] = [list(session_ids)]
    predicate = inquiry_read_filter(
        "issue.id",
        policy=policy,
        principal=principal,
        params=params,
    )
    rows = await conn.fetch(
        vetted_sql(
            "SELECT e.from_id, e.to_id FROM edges AS e "
            "JOIN inquiries AS issue ON issue.id = e.to_id "
            "WHERE e.from_id = ANY($1::uuid[]) AND e.edge_kind = 'produced_by' "
            "AND e.to_kind = 'Issue' "
            "AND NOT coalesce(issue.labels, '{}') @> ARRAY['workspace']::text[]",
            "" if predicate == "TRUE" else f" AND ({predicate})",
            " ORDER BY e.from_id, issue.seq, e.to_id",
        ),
        *params,
    )
    produced: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in rows:
        produced.setdefault(from_plain(row["from_id"], uuid.UUID), []).append(
            from_plain(row["to_id"], uuid.UUID),
        )
    return produced


async def _issues(
    conn: Conn,
    *,
    issue_ids: Sequence[uuid.UUID],
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[ListedIssue]:
    """Read ``issue_ids`` and the readable Issues each narrows or was produced by."""
    params: list[object] = [list(issue_ids)]
    predicate = inquiry_read_filter(
        "parent.id",
        policy=policy,
        principal=principal,
        params=params,
    )
    rows = await conn.fetch(
        vetted_sql(
            "WITH up AS (SELECT DISTINCT e.from_id AS child, e.to_id AS parent "
            "FROM edges AS e JOIN inquiries AS parent ON parent.id = e.to_id "
            "WHERE e.from_id = ANY($1::uuid[]) AND e.from_kind = 'Issue' "
            "AND e.to_kind = 'Issue' AND e.edge_kind IN ('narrows', 'produced_by') "
            "AND NOT coalesce(parent.labels, '{}') @> ARRAY['workspace']::text[]",
            "" if predicate == "TRUE" else f" AND ({predicate})",
            ") SELECT i.id, i.seq, i.title, i.status, coalesce(array_agg(up.parent "
            "ORDER BY up.parent) FILTER (WHERE up.parent IS NOT NULL), '{}') "
            "AS parents FROM inquiries AS i LEFT JOIN up ON up.child = i.id "
            "WHERE i.id = ANY($1::uuid[]) OR i.id IN (SELECT parent FROM up) "
            "GROUP BY i.id, i.seq, i.title, i.status ORDER BY i.seq, i.id",
        ),
        *params,
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
    lines: SessionLines,
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
        last_line=lines.last_line,
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
