"""Transaction primitive plus post-commit ``LISTEN/NOTIFY`` fanout."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

import asyncio
import json
import logging

from treekle import from_plain, json_loads

import asyncpg

from trackinizer.lib.postgres import DatabaseEngine


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    from trackinizer.lib.postgres import Conn


# ``asyncpg`` errors that mean the connection itself is gone, not that the SQL was
# rejected: a ROLLBACK hitting one of these is moot (a dead/closed connection
# has already discarded its transaction), so the error-path cleanup swallows
# them and lets the ORIGINAL exception -- the real failure the caller is
# mid-raise on -- propagate. ``InternalClientError`` is asyncpg's error for a
# reply it cannot frame ("protocol is in an unexpected state"), after which the
# connection is unusable.
_DEAD_CONN_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
)


__all__ = [
    "NOTIFICATION_BUFFER",
    "NOTIFY_CHANNEL",
    "SSE_PING",
    "Hears",
    "Notification",
    "changed_id",
    "iter_changed_ids",
    "iter_sse_events",
    "notify_after_commit",
    "tx",
]


NOTIFY_CHANNEL: Final = "trackinizer"
"""Postgres ``LISTEN/NOTIFY`` channel for post-commit inquiry change fan-out."""

SSE_PING: Final = b"event: ping\ndata: {}\n\n"
"""The keep-alive: a named event, because ``EventSource`` never shows the page a comment."""

type Hears = Callable[[UUID, str], Awaitable[bool]]
"""Whether a live stream's viewer hears of a write to a session's transcript, given
the session's id and the audience the write's ping names."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Notification:
    """A notification event for database changes.

    Attributes:
      engine: Where to publish it.
      subject_id: The inquiry that changed.
      audience: For a write to a session's transcript, the audience a viewer must read
        to hear of it; ``None`` for any other change, which every viewer hears.

    """

    engine: DatabaseEngine
    subject_id: UUID
    audience: str | None = None


NOTIFICATION_BUFFER: ContextVar[list[Notification] | None] = ContextVar(
    "trackinizer_notification_buffer",
    default=None,
)


@asynccontextmanager
async def tx(conn: Conn) -> AsyncGenerator[None]:
    """Explicit ``BEGIN``/``COMMIT``; pglite hangs on asyncpg's ``transaction()``.

    The error-path ``ROLLBACK`` goes through the EXTENDED protocol
    (``conn.fetch``) rather than the simple-query path (``conn.execute``). PGlite
    answers a statement that fails with an extra ReadyForQuery; the PGlite
    substrate drops it (``trackinizer.lib.postgres.substrate._write_manager_js``), but a
    server that let it through would misframe the reply to a *simple*
    ``ROLLBACK`` sent right after, crashing asyncpg's parser and swallowing the
    real error the caller is mid-raise on. ``BEGIN``/``COMMIT`` follow a
    *successful* statement, so they stay on the simple path.

    The error-path ``ROLLBACK`` is best-effort. The statement that aborted the
    transaction can also leave the connection itself dead, so even the
    extended-protocol ``ROLLBACK`` raises ``ConnectionDoesNotExistError`` /
    ``InternalClientError``. A dead connection has already discarded its
    transaction, so that secondary failure is moot; swallowing it (see
    :data:`_DEAD_CONN_ERRORS`) lets the ORIGINAL exception -- the real failure --
    propagate instead of being masked by a cleanup error.

    Args:
      conn: Database connection (asyncpg or pglite).

    Yields:
      item: (nothing; the yield resumes body and expects cleanup).

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

    Yields:
      item: (nothing; the yield resumes body for buffering).

    """
    outer = NOTIFICATION_BUFFER.get()
    if outer is not None:
        yield
        return
    notifications: list[Notification] = []
    token = NOTIFICATION_BUFFER.set(notifications)
    try:
        yield
    finally:
        NOTIFICATION_BUFFER.reset(token)
    # No committed/failed flag: an exception from the body -- including one
    # thrown into the yield by ``__aexit__``, ``aclose``, or cancellation --
    # propagates out of the generator rather than resuming after the ``try``,
    # so this line runs only on the clean-commit path. Adding an ``except``
    # clause that SUPPRESSES would break that and require a flag again.
    await _publish_notifications(notifications)


async def iter_sse_events(
    engine: DatabaseEngine,
    *,
    hears: Hears,
    keepalive_sec: float = 25.0,
) -> AsyncGenerator[bytes]:
    r"""Relay each ``NOTIFY_CHANNEL`` payload as one SSE ``data:`` frame.

    Each frame is ``{"id": "<uuid>"}`` -- the shape ``_notify_payload``
    emits and the SPA's ``EventSource`` parses. Both SSE routes call this so
    they share one generator and one wire contract.

    A payload that names an audience, as a write to a session's transcript does, is
    relayed only when ``hears`` says the viewer hears of it: a live stream would
    otherwise tell every viewer when a session they may not read is being written.

    The stream opens with an SSE comment and sends a ``ping`` event after every
    ``keepalive_sec`` without a frame. ``EventSource`` never shows a comment to
    the page, so the page hears the event and treats a stream silent for 2.5
    times ``keepalive_sec`` as dead; an older page ignores it.
    A proxy in front of production holds the response headers until the first
    body byte, and the Cloudflare edge answers 524 after 125 s without one and
    cuts a stream idle for 125 s, so a quiet stream never opened in the browser.

    The opening comment follows the subscription, so every change notified after
    it reaches the stream: a client may treat a read it starts after ``open`` as
    covered from then on.

    Args:
      engine: Database connection to listen on.
      hears: Whether the viewer hears of a write to a transcript.
      keepalive_sec: Longest silence before a ping; must stay
        well under the edge's 125 s idle cutoff.

    Yields:
      item: SSE-formatted frames (bytes with id and newline), comments and pings.

    """
    payloads = engine.listen(NOTIFY_CHANNEL)
    # One ``anext`` stays pending across keep-alives: ``asyncio.wait_for``
    # would cancel it, which throws into the listen generator and ends the
    # subscription.
    pending = asyncio.ensure_future(anext(payloads))
    try:
        # The bus registers a listener when its generator first runs, which is this
        # ``anext``'s first step; one pass of the loop takes it there. Saying
        # ``open`` before it lost the changes in between, so every client read
        # everything again on every open.
        await asyncio.sleep(0)
        yield b": open\n\n"
        while True:
            done, _ = await asyncio.wait({pending}, timeout=keepalive_sec)
            if not done:
                yield SSE_PING
                continue
            try:
                payload = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.ensure_future(anext(payloads))
            frame = await _sse_frame(payload, hears=hears)
            if frame:
                yield frame
    finally:
        pending.cancel()
        await asyncio.wait({pending})
        await payloads.aclose()


async def iter_changed_ids(
    engine: DatabaseEngine,
    *,
    hears: Hears,
) -> AsyncGenerator[str]:
    """Yield the inquiry id of each ``NOTIFY_CHANNEL`` payload, as the web stream relays it.

    Args:
      engine: Database connection to listen on.
      hears: Whether the viewer hears of a write to a transcript; a payload naming
        one it does not is skipped.

    Yields:
      id: An inquiry id; a malformed payload is logged and skipped.

    """
    payloads = engine.listen(NOTIFY_CHANNEL)
    try:
        async for payload in payloads:
            if (subject_id := await changed_id(payload, hears=hears)) is not None:
                yield subject_id
    finally:
        await payloads.aclose()


async def changed_id(payload: str, *, hears: Hears) -> str | None:
    """Read the inquiry id out of a NOTIFY payload a viewer may hear of.

    Args:
      payload: The payload ``_publish_notifications`` sent.
      hears: Whether the viewer hears of a write to a transcript.

    Returns:
      id: The inquiry id; None for a malformed payload, and for a write to a
        transcript the viewer does not hear of.

    """
    try:
        payload_data = json_loads(payload, dict[str, object])
        subject_id = from_plain(payload_data["id"], str)
        audience = from_plain(payload_data.get("audience"), str, default=None)
        transcript = None if audience is None else (UUID(subject_id), audience)
    except (KeyError, TypeError, ValueError):
        # Drop one bad payload rather than kill the stream, but LOG it: a
        # silent drop would hide a payload-shape regression (the producer
        # and this relay drifting apart).
        logging.getLogger(__name__).warning(
            "dropping malformed NOTIFY payload on %s: %r",
            NOTIFY_CHANNEL,
            payload,
        )
        return None
    if transcript is None or await hears(*transcript):
        return subject_id
    return None


async def _sse_frame(payload: str, *, hears: Hears) -> bytes:
    """One SSE ``data:`` frame for a NOTIFY payload; empty when the viewer gets none."""
    subject_id = await changed_id(payload, hears=hears)
    if subject_id is None:
        return b""
    return f"data: {json.dumps({'id': subject_id})}\n\n".encode()


# Failures are logged and swallowed: the transaction already committed, so raising would
# surface a spurious 500 over durable data. Subscribers reconcile any missed edge via
# ``what_changed_for_me``.
#
# The buffer dedups by ``subject_id`` first: a cascade over N ancestors buffers N+1
# entries with repeats (the changed row plus its own emit), and the SSE payload carries
# only the id, so a second NOTIFY for the same id is pure redundant round-trip latency.
# The first-seen engine per id wins (a buffer never spans engines within one commit).
# A ping that names no audience wins over one that does, for the same id: it reaches
# every viewer, the readers among them, where the other is dropped for every viewer who
# does not read its audience.
async def _publish_notifications(
    notifications: Sequence[Notification],
) -> None:
    """Publish one post-commit NOTIFY per affected inquiry."""
    by_subject: dict[UUID, Notification] = {}
    for notification in notifications:
        kept = by_subject.get(notification.subject_id)
        if kept is None or (
            kept.audience is not None and notification.audience is None
        ):
            by_subject[notification.subject_id] = notification
    for notification in by_subject.values():
        payload: dict[str, str] = {"id": str(notification.subject_id)}
        if notification.audience is not None:
            payload["audience"] = notification.audience
        try:
            await notification.engine.notify(NOTIFY_CHANNEL, json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 -- best-effort post-commit fanout.
            logging.getLogger(__name__).warning(
                "post-commit NOTIFY failed for %s: %s",
                notification.subject_id,
                exc,
            )
