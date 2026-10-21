"""Background loop that keeps the derived authority columns fresh.

Authority (PageRank) is a global fixed point, so it is recomputed wholesale
rather than per write. This loop coalesces bursts: it recomputes when at least
:data:`DIRTY_EDGES` edge mutations have landed since the last run OR
:data:`MAX_INTERVAL_SEC` has elapsed, and never more often than
:data:`MIN_INTERVAL_SEC`. An idle graph costs nothing; a busy one folds many
edge changes into one recompute.

Edge mutations are counted from ``change_log`` (every ``add_edge`` /
``remove_edge`` writes an audit row), so the write path gains no new coupling --
the loop polls the audit the store already keeps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import asyncio
import logging


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


__all__ = [
    "DIRTY_EDGES",
    "MAX_INTERVAL_SEC",
    "MIN_INTERVAL_SEC",
    "authority_sweep_loop",
    "edge_change_count",
]


_logger: Final = logging.getLogger(__name__)

DIRTY_EDGES: Final = 50
"""Recompute once this many edge mutations have accrued since the last run."""

MIN_INTERVAL_SEC: Final = 60.0
"""Floor between recomputes: a burst of edits coalesces into one run, so a busy
graph never pays PageRank on every write."""

MAX_INTERVAL_SEC: Final = 600.0
"""Ceiling: recompute at least this often even below the dirty threshold, so a
trickle of edits still refreshes within ten minutes."""

_EDGE_CHANGE_KINDS: Final = ("edge_added", "edge_removed")
"""``change_log.kind`` values a sweep treats as graph-dirtying."""


async def edge_change_count(store: Store) -> int:
    """Return the number of edge-mutation audit rows recorded so far.

    A monotonically non-decreasing dirtiness cursor: the loop recomputes when it
    has advanced by :data:`DIRTY_EDGES` since the last run.

    Args:
      store: The store whose ``change_log`` is polled.

    Returns:
      count: Total ``edge_added`` / ``edge_removed`` rows in ``change_log``.

    """
    async with store.engine.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM change_log WHERE kind = ANY($1::text[])",
            list(_EDGE_CHANGE_KINDS),
        )
    # ``count(*)`` is never NULL against Postgres; a None here is a stubbed or
    # unavailable connection, which reads as "no edges seen yet".
    return int(count) if isinstance(count, int) else 0  # pyrefly: ignore[unnecessary-type-conversion] -- The stub's type hides the runtime value (numpy/torch scalar or non-str key); the conversion is load-bearing.


async def authority_sweep_loop(
    store: Store,
    *,
    min_interval_sec: float = MIN_INTERVAL_SEC,
    max_interval_sec: float = MAX_INTERVAL_SEC,
    dirty_edges: int = DIRTY_EDGES,
) -> None:
    """Recompute authority forever, coalescing edge-change bursts.

    Sleeps ``min_interval_sec`` between checks. Recomputes when the edge-change
    count has advanced by ``dirty_edges`` since the last run, or when
    ``max_interval_sec`` has elapsed. Runs one recompute at startup so a fresh
    process serves scores immediately. Cancellation (shutdown) propagates.

    Args:
      store: The store to recompute against.
      min_interval_sec: Poll cadence and hard floor between recomputes.
      max_interval_sec: Force a recompute at least this often.
      dirty_edges: Edge-change delta that triggers a recompute early.

    """
    last_count = 0
    elapsed = max_interval_sec
    first_pass = True  # Recompute once at startup so a fresh process serves scores.
    while True:
        current = await edge_change_count(store)
        due = current - last_count >= dirty_edges or elapsed >= max_interval_sec
        if first_pass or due:
            try:
                written = await store.recompute_authority()
            except Exception:
                # A sweep failure must not kill the loop: log and retry next
                # cycle, so a transient DB blip degrades to stale scores, not a
                # permanently dead ranking. ``first_pass`` stays set so a failed
                # startup pass retries rather than waiting a full interval.
                _logger.exception("authority sweep failed; retrying next cycle")
            else:
                _logger.info("authority sweep wrote %d scores", written)
                last_count = current
                elapsed = 0.0
                first_pass = False
        await asyncio.sleep(min_interval_sec)
        elapsed += min_interval_sec
