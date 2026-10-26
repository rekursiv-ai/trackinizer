"""The asks open in a session's records, and ending the ones nobody can answer.

An agent that needs the person to choose writes an ``$interaction`` record into its
chat's session (``runs.asks``), and whoever ends the ask writes an
``$interaction_resolved`` record naming its id. The session is the one place that
says an ask is open, so the server reads it there: for a badge, and to end the asks
of a run that went offline, since a card with no run behind it can never be answered.

The server never edits a run's file. It writes its own part (:data:`ASKS_PART`), with
no format, so a resume never replays it into a CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import asyncio
import logging
import time

from treekle import from_plain, immutable

from trackinizer.sagent.session.types import SystemMessage
from trackinizer.types.errors import ConflictError
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import CHAT_SESSION_PREFIX


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


__all__ = [
    "ASKS_PART",
    "RELEASE_INTERVAL_SEC",
    "ask_release_loop",
    "asked_of",
    "open_ask_ids",
    "release_gone_runs",
    "release_open_asks",
    "release_orphaned_asks",
    "waiting_sessions",
]

_logger = logging.getLogger(__name__)

ASKS_PART: Final = "trackinizer-asks"
"""The file name of the part the server writes its releases into."""

RELEASE_INTERVAL_SEC: Final = 5.0
"""How often the loop looks for runs that went offline. A card stays open about this
long after its run's lease ends, on top of the lease's own 45 seconds."""


# Audience: internal -- only ids of asks leave it, to the server's own release and to
# a caller that has already checked the session it names.
async def open_ask_ids(conn: Conn, *, session_id: UUID) -> list[str]:
    """Return the ids of the asks open in a session, oldest first.

    An ask is open from the record that opens it to the record that resolves it, in
    whatever part either one is. Only the live prefix of each part is read.

    Args:
      conn: A database connection.
      session_id: The session.

    Returns:
      ids: The id of each ask with an ``$interaction`` record and no
        ``$interaction_resolved`` record.

    """
    return (await _open_asks(conn, session_ids=[session_id])).get(session_id, [])


# Audience: internal -- the name it returns goes only to the answer check of a poster
# already allowed to post into the session's chat.
async def asked_of(conn: Conn, *, session_id: UUID, ask_id: str) -> str:
    """Return whom an ask asked, as its card names them in ``input.asker``.

    An assistant's card asks one person, whose own choices it lists; a run's card
    names nobody, since the run's owner answers it.

    Args:
      conn: A database connection.
      session_id: The session the card is in.
      ask_id: The card's id.

    Returns:
      asker: The person asked; empty when no card of that id names one.

    """
    asker = await conn.fetchval(
        "SELECT r.payload::jsonb #>> $3::text[] "
        "FROM session_records AS r JOIN session_manifests AS m "
        "ON m.session_id = r.session_id AND m.part = r.part "
        "WHERE r.session_id = $1 AND r.kind = 'SystemMessage' "
        "AND r.idx < m.records AND r.payload::jsonb #>> $4::text[] = $2 "
        "ORDER BY r.part DESC, r.idx DESC LIMIT 1",
        session_id,
        ask_id,
        ["extra", "$interaction", "input", "asker"],
        ["extra", "$interaction", "id"],
    )
    return from_plain(asker, str, default="")


# Audience: internal -- only the ids of sessions leave it, to the badge a session's own
# reader sees on a session they already read.
async def waiting_sessions(conn: Conn, *, session_ids: Sequence[UUID]) -> set[UUID]:
    """Say which of some sessions have an ask open.

    Args:
      conn: A database connection.
      session_ids: The sessions to look in.

    Returns:
      waiting: The ids among them with at least one ask open.

    """
    return set(await _open_asks(conn, session_ids=session_ids))


# Audience: internal -- the server's own sweep: it writes the end of asks that nobody
# can answer, and what it returns is ids.
async def release_open_asks(
    store: Store,
    *,
    session_id: UUID,
    at: datetime,
) -> list[str]:
    """End every ask open in a session as released.

    The ends go in the server's part of the session. A session that has ended, or was
    purged, takes no record, and has none to release for.

    Args:
      store: The store.
      session_id: The session whose run is gone.
      at: When the asks ended.

    Returns:
      ids: The asks released; empty when none was open or the session ended.

    """
    async with store.engine.acquire() as conn:
        ids = await open_ask_ids(conn, session_id=session_id)
    if not ids:
        return []
    manifest = await store.read_session_manifest(session_id, name=ASKS_PART)
    start = 0 if manifest is None else manifest.records
    try:
        part = await store.upsert_session_manifest(
            session_id,
            name=ASKS_PART,
            metadata=immutable({}),
            ir_id=uuid4() if manifest is None else manifest.ir_id,
            format="",
            records=start + len(ids),
        )
        _ = await store.append_session_records(
            session_id,
            [
                SessionRecordRow.of(
                    session_id=session_id,
                    part=part,
                    idx=start + number,
                    record=_released(ask_id, at=at),
                )
                for number, ask_id in enumerate(ids)
            ],
        )
    except ConflictError:
        return []
    return ids


async def release_gone_runs(
    store: Store,
    *,
    inbound: InboundQueue,
    at: datetime,
) -> int:
    """Release the asks of every run whose lease ended since the last pass.

    A session that takes no record, one that ended or a purge removed, has nothing to
    release and does not stop the others.

    Args:
      store: The store.
      inbound: The queue that knows which leases ended.
      at: When the asks ended.

    Returns:
      released: How many asks were released.

    """
    released = 0
    for session_id in inbound.take_gone():
        released += len(await release_open_asks(store, session_id=session_id, at=at))
    return released


# Audience: internal -- the server's own sweep: it writes the end of asks that nobody
# can answer, and what it returns is a count.
#
# Only the queue's lapse notes a run gone, and it holds leases in memory: a run that
# died while the server was down has none after the restart, and never polls to make
# one. A run that is alive polls within its lease's span, so a chat still without one
# after that span has no run.
async def release_orphaned_asks(
    store: Store,
    *,
    inbound: InboundQueue,
    at: datetime,
) -> int:
    """Release the asks of live CLI chats that no run holds a lease on.

    Call it once a lease's span after the queue started, when every run that is alive
    has polled again. The assistant's chats are left alone: it releases its own cards,
    and it polls only when ready for input, so its lease is often absent.

    Args:
      store: The store.
      inbound: The queue that knows which sessions are polled.
      at: When the asks ended.

    Returns:
      released: How many asks were released.

    """
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sess.id FROM inquiries AS sess "
            "WHERE sess.kind = 'AgentSession' AND sess.agentsession_ended IS NULL "
            "AND sess.agentsession_cli = ANY($1::text[]) "
            "AND starts_with(sess.agentsession_cli_session_id, $2) "
            "AND EXISTS (SELECT 1 FROM session_records AS r "
            "JOIN session_manifests AS m "
            "ON m.session_id = r.session_id AND m.part = r.part "
            "WHERE r.session_id = sess.id AND r.kind = 'SystemMessage' "
            "AND r.idx < m.records "
            "AND r.payload::jsonb #>> $3::text[] IS NOT NULL) "
            "ORDER BY sess.created, sess.id",
            ["claude", "codex"],
            CHAT_SESSION_PREFIX,
            ["extra", "$interaction", "id"],
        )
    released = 0
    for row in rows:
        session_id = from_plain(row["id"], UUID)
        if inbound.has_poller(session_id):
            continue
        released += len(await release_open_asks(store, session_id=session_id, at=at))
    return released


async def ask_release_loop(
    store: Store,
    *,
    inbound: InboundQueue,
    interval_sec: float = RELEASE_INTERVAL_SEC,
    sweep_after_sec: float | None = None,
) -> None:
    """Release the asks of runs that went offline, one pass per interval.

    Once, ``sweep_after_sec`` after it starts, a pass also releases the asks of the
    chats whose run died while the server was down (:func:`release_orphaned_asks`).

    Args:
      store: The store.
      inbound: The queue that knows which leases ended.
      interval_sec: Seconds between passes.
      sweep_after_sec: How long after the start the one-time sweep runs; the queue's
        lease span by default.

    """
    sweep_at = time.monotonic() + (
        inbound.poller_ttl_sec if sweep_after_sec is None else sweep_after_sec
    )
    swept = False
    while True:
        await asyncio.sleep(interval_sec)
        try:
            now = datetime.now(UTC)
            _ = await release_gone_runs(store, inbound=inbound, at=now)
            if not swept and time.monotonic() >= sweep_at:
                _ = await release_orphaned_asks(store, inbound=inbound, at=now)
                swept = True
        except Exception:
            # A failed pass must not end the loop: the sessions it took are lost to
            # it, and their cards stay open until their run's session ends.
            _logger.exception("ask release pass failed; retrying next cycle")


# It has the shape ``runs.asks.resolved_record`` writes for a release, which the
# server may not import; ``asks_test`` holds the two equal.
def _released(ask_id: str, *, at: datetime) -> SystemMessage:
    """Return the record that ends an ask because its run is gone."""
    return SystemMessage(
        timestamp=at.isoformat(),
        subtype="interaction_resolved",
        content="Released",
        extra={
            "$interaction_resolved": {
                "id": ask_id,
                "option": "",
                "by": "released",
                "who": "",
                "decision": "released",
            },
        },
    )


# Audience: internal -- it returns ids of asks and sessions to the two readers above.
async def _open_asks(
    conn: Conn,
    *,
    session_ids: Sequence[UUID],
) -> dict[UUID, list[str]]:
    """Return the open asks of each of some sessions, in order; none for the rest."""
    rows = await conn.fetch(
        "SELECT r.session_id, r.payload::jsonb #>> $2::text[] AS opened, "
        "r.payload::jsonb #>> $3::text[] AS closed "
        "FROM session_records AS r JOIN session_manifests AS m "
        "ON m.session_id = r.session_id AND m.part = r.part "
        "WHERE r.session_id = ANY($1::uuid[]) AND r.kind = 'SystemMessage' "
        "AND r.idx < m.records "
        "AND (r.payload::jsonb #>> $2::text[] IS NOT NULL "
        "OR r.payload::jsonb #>> $3::text[] IS NOT NULL) "
        "ORDER BY r.session_id, r.part, r.idx",
        list(session_ids),
        ["extra", "$interaction", "id"],
        ["extra", "$interaction_resolved", "id"],
    )
    opened: dict[UUID, dict[str, None]] = {}
    closed: set[tuple[UUID, str]] = set()
    for row in rows:
        session_id = from_plain(row["session_id"], UUID)
        if (ask_id := from_plain(row["opened"], str, default="")) != "":
            opened.setdefault(session_id, {})[ask_id] = None
        if (ask_id := from_plain(row["closed"], str, default="")) != "":
            closed.add((session_id, ask_id))
    return {
        session_id: still
        for session_id, asks in opened.items()
        if (still := [each for each in asks if (session_id, each) not in closed])
    }
