"""Close the sessions whose agents died, and reopen any that come back.

A ``trax run`` ends its session when it exits -- unless it never gets the
chance: SIGKILL, an OOM kill, a crashed host. Its ``AgentSession`` then stayed
``active`` with ``ended`` NULL forever, and everything that reads the row shows
a dead agent as running. Nothing else could close it: the run was the only
party that would ever call ``end``.

The server already hears from every live run: it polls its inbound queue, and
its record uploads say the same. Each is noted in the ``session_liveness``
table, and this loop closes a session once nothing has been heard from it for
:data:`STALE_AFTER`, stamping ``ended`` with the last time it was. A session
that never polls -- a capture-only producer -- has no liveness row and is never
closed here, since its silence means nothing.

Silence is not proof of death: a run cut off from the server by a network
outage is alive and still working. So a closed session that polls or uploads
again is reopened by :func:`revive_if_reaped` before its request is served, and
carries on as the same session.

Liveness lives in the database, not in this process, so a server restart loses
neither half: a run that died while the server was down is still closed, and
one closed before a restart is still reopened after it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import asyncio
import logging


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


__all__ = [
    "REAPER_ACTOR",
    "REAP_INTERVAL_SEC",
    "STALE_AFTER",
    "reap_silent_sessions",
    "revive_if_reaped",
    "session_reaper_loop",
]


_logger: Final = logging.getLogger(__name__)

REAPER_ACTOR: Final = "trackinizer"
"""The audit author of a close or reopen the server made on its own."""

REAP_INTERVAL_SEC: Final = 60.0
"""How often the loop looks for silent sessions. Coarse against the silence
window it enforces, which is many minutes."""

STALE_AFTER: Final = timedelta(minutes=15)
"""How long a session that polls may go unheard before it counts as gone.

Far past the poller lease: a ``trax run`` polls continuously whether or not its
CLI is busy, but a session bridge polls only when its agent is ready for input,
and a long tool call can keep it away for minutes while the agent is alive. A
session closed too early is reopened when it next polls or uploads, so the cost
of the window is how long a dead agent still shows as live."""


async def reap_silent_sessions(
    store: Store,
    *,
    inbound: InboundQueue,
    now: datetime,
) -> int:
    """Close every session unheard from for :data:`STALE_AFTER` before ``now``.

    Args:
      store: Where the sessions and their liveness live.
      inbound: The queue holding messages for them, released once they close.
      now: The current time.

    Returns:
      closed: How many sessions this pass closed.

    """
    before = now - STALE_AFTER
    closed = 0
    for session_id in await store.silent_sessions(before=before):
        ended = await store.reap_session(
            session_id,
            before=before,
            actor=REAPER_ACTOR,
        )
        if ended is None:
            # Heard from, or ended by its own run, since the list was read.
            continue
        # No poller is left to drain what was queued for it.
        inbound.forget_poller(session_id)
        _ = inbound.drain(session_id)
        closed += 1
        _logger.info("closed silent session %s (last seen %s)", session_id, ended)
    return closed


async def revive_if_reaped(store: Store, *, session_id: UUID) -> bool:
    """Reopen ``session_id`` if the reaper closed it.

    Args:
      store: Where the session lives.
      session_id: A session that just polled or uploaded.

    Returns:
      revived: Whether the session was reopened.

    """
    revived = await store.revive_reaped_session(session_id, actor=REAPER_ACTOR)
    if revived:
        _logger.info("reopened session %s: its run came back", session_id)
    return revived


async def session_reaper_loop(
    store: Store,
    *,
    inbound: InboundQueue,
    interval_sec: float = REAP_INTERVAL_SEC,
) -> None:
    """Close silent sessions forever, one pass per ``interval_sec``.

    Args:
      store: Where the sessions and their liveness live.
      inbound: The queue holding messages for them.
      interval_sec: Seconds between passes.

    """
    while True:
        await asyncio.sleep(interval_sec)
        try:
            _ = await reap_silent_sessions(
                store,
                inbound=inbound,
                now=datetime.now(UTC),
            )
        except Exception:
            # A failed pass must not end the loop: a database blip leaves dead
            # sessions showing as live a little longer, never forever.
            _logger.exception("session reaper pass failed; retrying next cycle")
