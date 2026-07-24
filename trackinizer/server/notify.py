"""Transaction primitive plus post-commit ``LISTEN/NOTIFY`` fanout."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

import json
import logging

import asyncpg

from trackinizer.lib.postgres import Conn, DatabaseEngine


# asyncpg errors that mean the connection itself is gone, not that the SQL was
# rejected: a ROLLBACK hitting one of these is moot (a dead/closed connection
# has already discarded its transaction), so the error-path cleanup swallows
# them and lets the ORIGINAL exception -- the real failure the caller is
# mid-raise on -- propagate. ``InternalClientError`` covers asyncpg's
# "protocol is in an unexpected state" when pglite drops the socket mid-reply
# under load.
_DEAD_CONN_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
)


__all__ = [
    "NOTIFICATION_BUFFER",
    "NOTIFY_CHANNEL",
    "Notification",
    "iter_sse_events",
    "notify_after_commit",
    "tx",
]


NOTIFY_CHANNEL = "trackinizer"
"""Postgres ``LISTEN/NOTIFY`` channel for post-commit inquiry change fan-out."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Notification:
    engine: DatabaseEngine
    subject_id: UUID


NOTIFICATION_BUFFER: ContextVar[list[Notification] | None] = ContextVar(
    "trackinizer_notification_buffer",
    default=None,
)


@asynccontextmanager
async def tx(conn: Conn) -> AsyncGenerator[None]:
    """Explicit ``BEGIN``/``COMMIT``; pglite hangs on asyncpg's ``transaction()``.

    The error-path ``ROLLBACK`` goes through the EXTENDED protocol
    (``conn.fetch``) rather than the simple-query path (``conn.execute``): after a
    statement aborts the transaction, ``pglite`` 0.5 frames the reply to a
    *simple* ``ROLLBACK`` with no ``CommandComplete`` status tag, which crashes
    asyncpg's parser (``'NoneType' has no attribute 'decode'``) and corrupts the
    connection -- swallowing the real error the caller is mid-raise on. The
    extended protocol is framed correctly. ``BEGIN``/``COMMIT`` run after a
    *successful* statement, which pglite frames correctly, so they stay on the
    simple path.

    The error-path ``ROLLBACK`` is best-effort. The statement that aborted the
    transaction can also leave the connection itself half-dead -- under load
    pglite may drop the socket mid-reply, so even the extended-protocol
    ``ROLLBACK`` raises ``ConnectionDoesNotExistError`` /
    ``InternalClientError``. A dead connection has already discarded its
    transaction, so that secondary failure is moot; swallowing it (see
    :data:`_DEAD_CONN_ERRORS`) lets the ORIGINAL exception -- the real failure --
    propagate instead of being masked by a cleanup error.
    """
    await conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        with suppress(*_DEAD_CONN_ERRORS):
            _ = await conn.fetch("ROLLBACK")
        raise
    else:
        await conn.execute("COMMIT")


@asynccontextmanager
async def notify_after_commit() -> AsyncGenerator[None]:
    """Buffer notifications until the surrounding transaction commits.

    Each buffered :class:`Notification` carries its own engine, so this
    manager needs none. After a clean commit they fan out via
    :func:`_publish_notifications`; failures there are logged, not raised.
    """
    outer = NOTIFICATION_BUFFER.get()
    if outer is not None:
        yield
        return
    notifications: list[Notification] = []
    token = NOTIFICATION_BUFFER.set(notifications)
    ok = False
    try:
        yield
        ok = True
    finally:
        NOTIFICATION_BUFFER.reset(token)
    if ok:
        await _publish_notifications(notifications)


async def _publish_notifications(
    notifications: Sequence[Notification],
) -> None:
    """Publish one post-commit NOTIFY per affected inquiry.

    Failures are logged and swallowed: the transaction already committed, so
    raising would surface a spurious 500 over durable data. Subscribers
    reconcile any missed edge via ``what_changed_for_me``.

    The buffer dedups by ``subject_id`` first: a cascade over N ancestors
    buffers N+1 entries with repeats (the changed row plus its own emit), and
    the SSE payload carries only the id, so a second NOTIFY for the same id is
    pure redundant round-trip latency. The first-seen engine per id wins
    (a buffer never spans engines within one commit).
    """
    by_subject: dict[UUID, Notification] = {}
    for notification in notifications:
        by_subject.setdefault(notification.subject_id, notification)
    for notification in by_subject.values():
        try:
            await notification.engine.notify(
                NOTIFY_CHANNEL,
                _notify_payload(notification.subject_id),
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "post-commit NOTIFY failed for %s: %s",
                notification.subject_id,
                exc,
            )


def _notify_payload(subject_id: UUID) -> str:
    """JSON payload broadcast on ``NOTIFY_CHANNEL`` per inquiry change."""
    return json.dumps({"id": str(subject_id)})


async def iter_sse_events(engine: DatabaseEngine) -> AsyncIterator[bytes]:
    r"""Relay each ``NOTIFY_CHANNEL`` payload as one SSE ``data:`` frame.

    Each frame is ``{"id": "<uuid>"}`` -- the shape ``_notify_payload``
    emits and the SPA's ``EventSource`` parses. Both SSE routes call this so
    they share one generator and one wire contract.
    """
    async for payload in engine.listen(NOTIFY_CHANNEL):
        try:
            subject_id = json.loads(payload)["id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            # Drop one bad payload rather than kill the stream, but LOG it: a
            # silent ``continue`` would hide a payload-shape regression (the
            # producer and this relay drifting apart).
            logging.getLogger(__name__).warning(
                "dropping malformed NOTIFY payload on %s: %r",
                NOTIFY_CHANNEL,
                payload,
            )
            continue
        yield f"data: {json.dumps({'id': subject_id})}\n\n".encode()
