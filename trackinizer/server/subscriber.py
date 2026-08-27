"""Push committed changes into subscribers' live sessions.

The routing step between ``change_log`` (the durable audit) and the inbound
queue (live-session steering): a lifespan task sweeps the change log on a
short interval and copies each new change -- as raw change JSON, one object
per line -- into the inbound mailbox of every actor in its
``subscribers_snapshot`` with a live session.

A plain sweep, no LISTEN/NOTIFY: the doorbell was tried and cut. A dead
LISTEN connection raises nothing (the generator just stops yielding,
``substrate.py``), so a periodic sweep had to exist anyway as the liveness
backstop -- at which point the doorbell bought only latency, and delivery is
gated by the *client-side* inbound poller (~0.5s tick) regardless, so the
saving was unobservable end to end. One indexed query per sweep (the partial
index ``idx_change_log_subscribed_created_id`` serves it) is the entire cost.

Delivery inherits inbound semantics by design: live sessions only,
drop-if-absent, capped queues. A subscriber with no running session misses
events; offline catch-up is the polling surface (``what_changed_for_me``),
not this pipe. The cursor starts at task boot, so changes committed while
the server was down are never pushed (nobody had a live poller then either).

The lifespan starts one sweep per app instance, so ``--workers N`` runs N
independent sweeps over the same rows. That is redundant when healthy and
actively wrong at delivery: :func:`_delivery_key` is deterministic, but the
receipts it is checked against live in each worker's own ``InboundQueue``,
so every worker enqueues the same change. See the
``TODO(inbound-multiworker)`` in ``inbound.py`` -- that queue owns the
defect and the candidate fixes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

import asyncio
import json
import logging

from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.change_log import Change


__all__ = ["push_changes_to_live_subscribers"]


logger = logging.getLogger(__name__)


async def push_changes_to_live_subscribers(
    store: Store,
    inbound: InboundQueue,
    *,
    page_size: int = 200,
    sweep_interval_sec: float = 0.5,
    max_backoff_sec: float = 60.0,
) -> None:
    """Sweep the change log forever; started and cancelled by the lifespan.

    Three structural invariants, each earned by a review finding:

    - Every sweep drains the cursor query to a short page, so a burst wider
      than ``page_size`` never strands its tail.
    - A change whose delivery fails is logged and SKIPPED -- the cursor
      always advances. Skipping is safe because the change_log row is
      durable (``what_changed_for_me`` still serves it); a pinned cursor
      would block every later change for every subscriber forever.
    - A failing QUERY (as opposed to a failing delivery) backs off, and logs
      its stack once rather than once per attempt. Observed in production: a
      full log partition stopped PostgreSQL, and this loop then retried on
      its fixed interval with a full asyncpg traceback each time, once per
      uvicorn worker -- ~33,000 lines/sec, of which tracebacks were 35% of
      the bytes. The database is down either way; the logging is what turned
      that into a second incident on the same partition.

    Deliveries are deduped by a deterministic per-``(change, subscriber)``
    key, so a replayed read does not double-inject (within the inbound
    queue's bounded receipt window).
    """
    since = datetime.now(UTC)
    after_id: UUID | None = None
    failures = 0
    logger.info(
        "subscriber push sweeping every %.1fs (page_size=%d)",
        sweep_interval_sec,
        page_size,
    )
    while True:
        since, after_id, ok = await _drain_pending_changes(
            store, inbound, since, after_id, page_size=page_size, failures=failures
        )
        if ok:
            if failures:
                logger.info(
                    "subscriber push catch-up query recovered after %d failure(s)",
                    failures,
                )
            failures = 0
            delay = sweep_interval_sec
        else:
            failures += 1
            # Doubling from the base interval, capped: a 12-hour outage costs
            # ~720 attempts instead of ~86,400 per worker.
            delay = min(sweep_interval_sec * 2**failures, max_backoff_sec)
        await asyncio.sleep(delay)


async def _drain_pending_changes(
    store: Store,
    inbound: InboundQueue,
    since: datetime,
    after_id: UUID | None,
    *,
    page_size: int,
    failures: int = 0,
) -> tuple[datetime, UUID | None, bool]:
    """Deliver every change past the cursor; return the advanced cursor.

    Pages until a short page. The cursor advances past EVERY change,
    delivered or not (failing rows are logged and skipped; the durable row
    remains readable via polling). A failing query returns the cursor
    unchanged and ``ok=False`` -- the caller backs off and retries.

    ``failures`` is the caller's consecutive-failure count, used only to
    decide whether this failure is the first of an outage (stack included)
    or a repeat (one line, no stack).
    """
    while True:
        try:
            changes = await store.what_changed_for_anyone(
                since, after_id=after_id, limit=page_size
            )
        except Exception as exc:
            if failures == 0:
                logger.warning(
                    "subscriber push catch-up query failed; will retry",
                    exc_info=True,
                )
            else:
                # The stack is identical every attempt and was 35% of the
                # bytes that filled the log partition; the exception's own
                # repr is the part that changes and the part worth keeping.
                logger.warning(
                    "subscriber push catch-up query still failing (%d consecutive): %r",
                    failures + 1,
                    exc,
                )
            return since, after_id, False
        for change, subject_seq in changes:
            try:
                await _deliver_change(store, inbound, change, subject_seq)
            except Exception:
                logger.warning(
                    "undeliverable change %s skipped (recoverable via "
                    "what_changed_for_me)",
                    change.id,
                    exc_info=True,
                )
            since, after_id = change.created, change.id
        if len(changes) < page_size:
            return since, after_id, True


async def _deliver_change(
    store: Store,
    inbound: InboundQueue,
    change: Change,
    subject_seq: int | None,
) -> None:
    """Enqueue one change to every snapshot subscriber's live sessions.

    One INFO per subscriber delivery and one DEBUG per no-live-session skip:
    a "subscriber never got the event" report bisects on these -- present
    means the server half worked (look at the client poller); absent means
    the sweep never delivered (look at the cursor / subscription).
    """
    payload = _change_payload(change, subject_seq)
    for subscriber in change.subscribers_snapshot:
        sessions = await store.resolve_live_sessions(subscriber)
        targets = [
            (session_id, Inbound(text=payload, source="trackinizer"))
            for session_id, _rooms in sessions
        ]
        if targets:
            delivered = inbound.send_once(_delivery_key(change.id, subscriber), targets)
            logger.info(
                "delivered change %s (%s on %s) to %s: %d session(s)",
                change.id,
                change.kind,
                change.subject_kind,
                subscriber,
                len(delivered),
            )
        else:
            logger.debug(
                "change %s: subscriber %s has no live session; dropped",
                change.id,
                subscriber,
            )


def _change_payload(change: Change, subject_seq: int | None) -> str:
    """One change as a compact JSON envelope: metadata, not the record.

    The push is a notification; the durable row is the record. Only the
    envelope ships -- no ``old``/``new`` delta, so unbounded text fields
    (descriptions, abstracts) never ride the injected line, and no
    ``subscribers_snapshot``, so recipients do not learn the roster.

    ``agent_message`` leads: the one line a model-CLI session receives
    (the ``trax run`` poller injects only it there, keeping the model's
    context clean; IO-stream sessions get this whole envelope and parse it
    themselves). It addresses the row the way every trax verb does -- the
    short ``issue 42`` ref, not a 36-char UUID (a token-waste for the model
    reading it). The seq is resolved by the sweep's JOIN; a purged subject
    (no inquiries row) falls back to the UUID, which stays correct forever.
    """
    subject_ref = (
        f"{(change.subject_kind or 'inquiry').lower()} "
        f"{subject_seq if subject_seq is not None else change.subject_id}"
    )
    # Field-edit kinds read "<field> changed"; event kinds (created,
    # edge_added, ...) already name the happening and read bare.
    happening = (
        change.kind
        if change.kind is None or change.kind.endswith("ed")
        else f"{change.kind} changed"
    )
    envelope: dict[str, object] = {
        "agent_message": f"FYI: trax {subject_ref} {happening} (by {change.actor})",
        "id": str(change.id),
        "created": change.created.isoformat(),
        "actor": change.actor,
        "kind": change.kind,
        "subject_kind": change.subject_kind,
        "subject_id": str(change.subject_id),
        "subject_ref": subject_ref,
        "reason": change.reason,
    }
    if change.caused_by is not None:
        envelope["caused_by"] = str(change.caused_by)
    envelope["row"] = f"trax {subject_ref}"
    # TODO: also carry a ``delta`` follow-up command (this change's old/new
    # diff) once a ``trax change <id>`` verb exists to serve it; the server
    # route (GET /api/change_log/{id}) is already there.
    return json.dumps(
        {k: v for k, v in envelope.items() if v not in (None, "")},
        separators=(",", ":"),
    )


def _delivery_key(change_id: UUID, subscriber: str) -> UUID:
    """Deterministic dedup key: one delivery per (change, subscriber).

    ``uuid5`` with the change id as the namespace: unconventional (the slot
    usually holds a ``NAMESPACE_*`` constant) but any UUID is a valid
    namespace, and (change_id, subscriber) -> UUID is exactly the
    deterministic keyed hash the dedup needs.
    """
    return uuid5(change_id, subscriber)
