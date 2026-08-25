"""Tests for the subscriber push sweep.

The task is a plain periodic sweep over ``change_log``: every interval it
drains the cursor query and copies each returned change -- as raw JSON --
into the inbound queue of each subscriber's live sessions. Tests run the
real task with a tiny interval, poll the real ``InboundQueue`` for the
expected outcome, then cancel; the store stub models the real cursor
contract (filter by ``(since, after_id)``, honor ``limit``) so a cursor
that fails to advance, advances wrongly, or skips pagination produces
wrong deliveries here -- not just different recorded calls.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast, override
from uuid import UUID

import asyncio
import json

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.subscriber import (
    _change_payload,
    _delivery_key,
    push_changes_to_live_subscribers,
)
from trackinizer.types.change_log import Change, Snapshot


def _change(
    *,
    subscribers: tuple[str, ...] = ("alice",),
    kind: Change.Kind = "created",
    created: datetime | None = None,
) -> Change:
    return Change(
        actor="bob",
        subject_id=new_uuid(),
        subject_kind="Issue",
        kind=kind,
        subscribers_snapshot=subscribers,
        created=created if created is not None else _future(),
    )


def _future(seconds: float = 60.0) -> datetime:
    """A timestamp safely past the task's boot cursor (``since = now()``)."""
    return datetime.now(UTC) + timedelta(seconds=seconds)


class _StubStore:
    """The two store methods the sweep consumes, modeling the real contract."""

    query_count: int

    # The subject seq the real query's LEFT JOIN resolves; one value
    # suffices for tests (None models a purged subject).
    subject_seq: int | None = 42

    def __init__(
        self,
        *,
        changes: list[Change],
        live_sessions: dict[str, list[UUID]],
    ) -> None:
        self.changes = list(changes)
        self._live = live_sessions
        self.query_count = 0

    async def what_changed_for_anyone(
        self,
        since: datetime,
        *,
        after_id: UUID | None = None,
        limit: int = 200,
    ) -> list[tuple[Change, int | None]]:
        self.query_count += 1
        cursor = (since, after_id if after_id is not None else UUID(int=0))
        rows = [
            c
            for c in self.changes
            if (c.created, c.id) > cursor and c.subscribers_snapshot
        ]
        rows.sort(key=lambda c: (c.created, c.id))
        return [(c, self.subject_seq) for c in rows[:limit]]

    async def resolve_live_sessions(
        self, actor: str, *, room: str | None = None
    ) -> list[tuple[UUID, tuple[str, ...]]]:
        del room
        return [(sid, ()) for sid in self._live.get(actor, [])]


async def _run_sweep_until(
    store: _StubStore,
    inbound: InboundQueue,
    done: Callable[[], bool],
    *,
    page_size: int = 200,
    timeout_sec: float = 5.0,
) -> None:
    """Drive the real task until ``done()`` (or fail at ``timeout_sec``)."""
    task = asyncio.create_task(
        push_changes_to_live_subscribers(
            cast(Any, store),
            inbound,
            page_size=page_size,
            sweep_interval_sec=0.01,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while not done():
            assert asyncio.get_running_loop().time() < deadline, (
                "sweep never produced the expected deliveries"
            )
            await asyncio.sleep(0.005)
        # One extra beat so an over-delivering bug has a chance to surface
        # before the assertions run.
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class TestPushChanges:
    @pytest.mark.asyncio
    async def test_change_reaches_subscribers_live_session(self) -> None:
        """One committed change lands, as JSON, in the subscriber's queue."""
        session_id = new_uuid()
        change = _change()
        store = _StubStore(changes=[change], live_sessions={"alice": [session_id]})
        inbound = InboundQueue()

        await _run_sweep_until(store, inbound, lambda: inbound.pending(session_id) > 0)

        drained = inbound.drain(session_id)
        assert len(drained) == 1
        assert drained[0].source == "trackinizer"
        payload = json.loads(drained[0].text)
        assert payload["kind"] == "created"
        assert payload["subject_id"] == str(change.subject_id)

    @pytest.mark.asyncio
    async def test_delivery_is_logged_for_diagnosis(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each subscriber delivery emits one INFO naming change + subscriber.

        The bisect line for "alice never got the event": present means the
        server half worked (suspect the client poller); absent means the
        sweep never delivered.
        """
        session_id = new_uuid()
        change = _change()
        store = _StubStore(changes=[change], live_sessions={"alice": [session_id]})
        inbound = InboundQueue()

        with caplog.at_level("INFO", logger="trackinizer.server.subscriber"):
            await _run_sweep_until(
                store, inbound, lambda: inbound.pending(session_id) > 0
            )

        delivered = [r for r in caplog.records if "delivered change" in r.message]
        assert len(delivered) == 1
        assert str(change.id) in delivered[0].getMessage()
        assert "alice" in delivered[0].getMessage()

    @pytest.mark.asyncio
    async def test_no_live_session_drops_silently(self) -> None:
        """A subscriber with no live session gets nothing; the task survives."""
        store = _StubStore(changes=[_change()], live_sessions={})
        inbound = InboundQueue()

        # Wait for the sweep to have run at least twice, then verify nothing
        # was enqueued anywhere.
        await _run_sweep_until(store, inbound, lambda: store.query_count >= 2)
        assert inbound.pending(new_uuid()) == 0

    @pytest.mark.asyncio
    async def test_delivered_change_is_not_redelivered(self) -> None:
        """The cursor advances: later sweeps do not re-deliver.

        Asserted on OUTCOMES against the contract-modeling stub (exactly one
        delivery across many sweeps) -- a wrong-but-moving cursor fails this
        by re-delivering or skipping.
        """
        session_id = new_uuid()
        store = _StubStore(changes=[_change()], live_sessions={"alice": [session_id]})
        inbound = InboundQueue()

        # Run until several sweeps past the delivery have completed.
        await _run_sweep_until(store, inbound, lambda: store.query_count >= 5)

        assert inbound.pending(session_id) == 1

    @pytest.mark.asyncio
    async def test_burst_larger_than_one_page_fully_drains(self) -> None:
        """One sweep drains the whole backlog, paging until a short page."""
        session_id = new_uuid()
        changes = [_change(created=_future(60 + i)) for i in range(5)]
        store = _StubStore(changes=changes, live_sessions={"alice": [session_id]})
        inbound = InboundQueue()

        await _run_sweep_until(
            store,
            inbound,
            lambda: inbound.pending(session_id) >= 5,
            page_size=2,
        )
        assert inbound.pending(session_id) == 5

    @pytest.mark.asyncio
    async def test_poison_change_is_skipped_not_blocking(self) -> None:
        """An undeliverable change is skipped; everything behind it flows.

        The cursor must advance past a change whose delivery raises --
        skipping is safe (the durable log keeps the row) while a pinned
        cursor blocks every later change for every subscriber forever.
        """
        good_sid = new_uuid()
        poison = _change(subscribers=("alice",), created=_future(60))
        good = _change(subscribers=("bob",), created=_future(61))
        store = _StubStore(
            changes=[poison, good],
            live_sessions={"alice": [new_uuid()], "bob": [good_sid]},
        )

        class _PoisonInbound(InboundQueue):
            @override
            def send_once(
                self,
                key: UUID | None,
                targets: list[tuple[UUID, Inbound]],
            ) -> list[UUID]:
                if any(str(poison.subject_id) in t.text for _, t in targets):
                    raise RuntimeError("undeliverable payload")
                return super().send_once(key, targets)

        inbound = _PoisonInbound()

        await _run_sweep_until(store, inbound, lambda: inbound.pending(good_sid) > 0)
        assert inbound.pending(good_sid) == 1

    @pytest.mark.asyncio
    async def test_redelivery_is_idempotent_per_change_and_subscriber(self) -> None:
        """A replayed read of the same change enqueues nothing new.

        ``send_once`` dedups on the deterministic (change, subscriber) key,
        so a stuck cursor (crashed-and-restarted read) does not double-inject
        (within the queue's bounded receipt window).
        """
        session_id = new_uuid()
        change = _change()

        class _StuckCursorStore(_StubStore):
            """Models a crashed-and-restarted read: the cursor never moves."""

            @override
            async def what_changed_for_anyone(
                self,
                since: datetime,
                *,
                after_id: UUID | None = None,
                limit: int = 200,
            ) -> list[tuple[Change, int | None]]:
                del since, after_id, limit
                self.query_count += 1
                return [(c, self.subject_seq) for c in self.changes]

        store = _StuckCursorStore(
            changes=[change], live_sessions={"alice": [session_id]}
        )
        inbound = InboundQueue()

        await _run_sweep_until(store, inbound, lambda: store.query_count >= 5)
        assert inbound.pending(session_id) == 1

    @pytest.mark.asyncio
    async def test_multiple_subscribers_each_receive(self) -> None:
        """Every actor in the snapshot with a live session gets its own copy."""
        alice_sid, carol_sid = new_uuid(), new_uuid()
        store = _StubStore(
            changes=[_change(subscribers=("alice", "carol"))],
            live_sessions={"alice": [alice_sid], "carol": [carol_sid]},
        )
        inbound = InboundQueue()

        await _run_sweep_until(
            store,
            inbound,
            lambda: inbound.pending(alice_sid) > 0 and inbound.pending(carol_sid) > 0,
        )
        assert inbound.pending(alice_sid) == 1
        assert inbound.pending(carol_sid) == 1

    @pytest.mark.asyncio
    async def test_one_bad_sweep_does_not_kill_the_task(self) -> None:
        """A failing catch-up query is logged; the next sweep still works."""
        session_id = new_uuid()

        class _FlakyStore(_StubStore):
            @override
            async def what_changed_for_anyone(
                self,
                since: datetime,
                *,
                after_id: UUID | None = None,
                limit: int = 200,
            ) -> list[tuple[Change, int | None]]:
                if self.query_count == 0:
                    self.query_count += 1
                    raise RuntimeError("transient query failure")
                return await super().what_changed_for_anyone(
                    since, after_id=after_id, limit=limit
                )

        store = _FlakyStore(changes=[_change()], live_sessions={"alice": [session_id]})
        inbound = InboundQueue()

        await _run_sweep_until(store, inbound, lambda: inbound.pending(session_id) > 0)
        assert inbound.pending(session_id) == 1


class TestPayloadAndKey:
    def test_change_payload_is_a_compact_envelope(self) -> None:
        """The payload is metadata only: who did what to which row, when."""
        change = _change(kind="status")
        payload = json.loads(_change_payload(change, 42))
        assert payload["kind"] == "status"
        assert payload["actor"] == "bob"
        assert payload["subject_kind"] == "Issue"
        assert payload["subject_id"] == str(change.subject_id)
        assert payload["id"] == str(change.id)
        assert "null" not in _change_payload(change, 42)

    def test_payload_addresses_the_subject_by_short_ref(self) -> None:
        """Refs use the short per-kind seq, not a 36-char UUID.

        The consumer is often a model; ``issue 42`` is the address every
        trax verb takes and a fraction of the tokens. ``agent_message`` leads with a
        one-line human-readable summary, ``row`` is the runnable fetch, and
        ``subject_ref`` carries the bare address for programmatic reuse.
        """
        change = _change(kind="status")
        payload = json.loads(_change_payload(change, 42))
        assert payload["agent_message"] == "FYI: trax issue 42 status changed (by bob)"
        assert payload["subject_ref"] == "issue 42"
        assert payload["row"] == "trax issue 42"

    def test_payload_falls_back_to_uuid_for_purged_subjects(self) -> None:
        """A purged subject has no seq (LEFT JOIN miss); the UUID still works."""
        change = _change(kind="purged")
        payload = json.loads(_change_payload(change, None))
        assert payload["subject_ref"] == f"issue {change.subject_id}"
        assert payload["row"] == f"trax issue {change.subject_id}"

    def test_agent_message_reads_naturally_for_event_kinds(self) -> None:
        """Event kinds (created, edge_added) read bare -- no 'changed' suffix."""
        payload = json.loads(_change_payload(_change(kind="edge_added"), 7))
        assert payload["agent_message"] == "FYI: trax issue 7 edge_added (by bob)"

    def test_payload_is_bounded_regardless_of_field_sizes(self) -> None:
        """The envelope never carries the delta, so field sizes cannot leak in.

        The payload rides one line-oriented injection; the record (including
        unbounded text like descriptions) stays on the durable row, reachable
        via the ``next`` command.
        """
        change = Change(
            actor="bob",
            subject_id=new_uuid(),
            subject_kind="Issue",
            kind="description",
            subscribers_snapshot=("alice",),
            new=Snapshot(description="x" * 40_000),
        )
        payload = _change_payload(change, 42)
        assert len(payload) < 512, "envelope must be metadata-sized"
        assert "xxxx" not in payload, "the delta must not ride the push"

    def test_payload_does_not_leak_the_subscriber_roster(self) -> None:
        """Recipients must not learn who else subscribes."""
        change = _change(subscribers=("alice", "bob", "carol"))
        assert "carol" not in _change_payload(change, 42)

    def test_delivery_key_is_deterministic_per_change_and_subscriber(self) -> None:
        change_id = new_uuid()
        assert _delivery_key(change_id, "alice") == _delivery_key(change_id, "alice")
        assert _delivery_key(change_id, "alice") != _delivery_key(change_id, "bob")
        assert _delivery_key(new_uuid(), "alice") != _delivery_key(change_id, "alice")


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
