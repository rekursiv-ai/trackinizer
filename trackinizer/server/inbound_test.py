"""Tests for the in-process inbound-message queue."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import override

import asyncio
import threading
import time
import uuid

import pytest

from trackinizer.server.inbound import Inbound, InboundQueue, PollerRun
from trackinizer.wire.wire_sessions import CanvasMessageContext


class TestInboundQueue:
    def test_poller_lease_expires_without_another_poll(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()

        assert not queue.has_poller(session)
        queue.mark_poller(session)
        assert queue.has_poller(session)
        now[0] = 144.0
        assert queue.has_poller(session)
        now[0] = 145.0
        assert not queue.has_poller(session)
        queue.mark_poller(session)
        assert queue.has_poller(session)

    def test_a_refusal_is_kept_until_a_drain_is_let_in(self) -> None:
        queue = InboundQueue(max_refusals=2)
        session, other, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        assert queue.refusal(session) is None
        queue.refuse(session, reason="Another run is draining this chat")
        first = queue.refusal(session)
        assert first is not None
        assert first.reason == "Another run is draining this chat"
        queue.refuse(session, reason="This chat was closed")
        again = queue.refusal(session)
        # A new refusal is told from one a reader saw by when it was made.
        assert again is not None
        assert (again.reason, again.at > first.at) == ("This chat was closed", True)
        queue.mark_poller(session)
        assert queue.refusal(session) is None
        queue.refuse(session, reason="a")
        queue.refuse(other, reason="b")
        queue.refuse(third, reason="c")
        assert queue.refusal(session) is None
        assert queue.refusal(third) is not None

    def test_active_poller_ids_lists_only_unexpired_leases(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        early, late = uuid.uuid4(), uuid.uuid4()
        queue.mark_poller(early)
        now[0] = 120.0
        queue.mark_poller(late)
        now[0] = 145.0
        assert queue.active_poller_ids() == [late]
        now[0] = 165.0
        assert queue.active_poller_ids() == []

    def test_enqueue_then_drain_is_fifo(self) -> None:
        q = InboundQueue()
        sid = uuid.uuid4()
        assert q.enqueue(sid, Inbound(text="first")) == 1
        assert q.enqueue(sid, Inbound(text="second", source="scientist")) == 2
        drained = q.drain(sid)
        assert [(m.text, m.source) for m in drained] == [
            ("first", None),
            ("second", "scientist"),
        ]

    def test_drain_empties_the_queue(self) -> None:
        q = InboundQueue()
        sid = uuid.uuid4()
        q.enqueue(sid, Inbound(text="x"))
        q.drain(sid)
        assert q.pending(sid) == 0
        assert q.drain(sid) == []

    def test_queues_are_per_session(self) -> None:
        q = InboundQueue()
        a, b = uuid.uuid4(), uuid.uuid4()
        q.enqueue(a, Inbound(text="for-a"))
        assert q.pending(b) == 0
        assert [m.text for m in q.drain(a)] == ["for-a"]

    def test_cap_drops_oldest(self) -> None:
        q = InboundQueue(max_per_session=2)
        sid = uuid.uuid4()
        q.enqueue(sid, Inbound(text="1"))
        q.enqueue(sid, Inbound(text="2"))
        q.enqueue(sid, Inbound(text="3"))  # Evicts "1"
        assert [m.text for m in q.drain(sid)] == ["2", "3"]

    def test_cap_drop_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        # An overflow drop loses an undelivered message (a stuck/absent poller)
        # -- it must WARN, not silently swallow, or the lost-message signal is
        # invisible. One warning per dropped message.
        q = InboundQueue(max_per_session=1)
        sid = uuid.uuid4()
        q.enqueue(sid, Inbound(text="1"))
        with caplog.at_level("WARNING"):
            q.enqueue(sid, Inbound(text="2"))  # Evicts "1"
            q.enqueue(sid, Inbound(text="3"))  # Evicts "2"
        drops = [r for r in caplog.records if "dropped oldest" in r.message]
        assert len(drops) == 2

    def test_inbound_carries_room(self) -> None:
        q = InboundQueue()
        sid = uuid.uuid4()
        q.enqueue(sid, Inbound(text="hi", source="alice@x", room="sear"))
        (msg,) = q.drain(sid)
        assert (msg.text, msg.source, msg.room) == ("hi", "alice@x", "sear")

    def test_a_queue_is_full_exactly_at_its_cap_and_drains_to_room(self) -> None:
        queue = InboundQueue(max_per_session=2)
        session = uuid.uuid4()

        assert not queue.is_full(session)
        _ = queue.enqueue(session, Inbound(text="one"))
        assert not queue.is_full(session)
        _ = queue.enqueue(session, Inbound(text="two"))
        assert queue.is_full(session)
        assert not queue.is_full(uuid.uuid4())
        _ = queue.drain(session)
        assert not queue.is_full(session)


class TestAwaitMessages:
    """Waiting for a message rather than asking for one repeatedly.

    A poller asking every 0.5s is one request per session per half-second
    whether or not anything was sent, and still delivers up to 0.5s late. A
    wait that returns the moment a message is enqueued costs one held request
    and delivers immediately.
    """

    def test_returns_immediately_when_messages_are_pending(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        _ = queue.enqueue(session, Inbound(text="already here"))

        started = time.monotonic()
        drained = asyncio.run(queue.await_messages(session, timeout_sec=5.0))

        assert [m.text for m in drained] == ["already here"]
        assert time.monotonic() - started < 0.5, "waited despite a pending message"
        assert not queue._waiters

    def test_enqueue_after_initial_drain_is_not_missed(self) -> None:
        """An enqueue in the drain-to-wait gap must wake the same poll."""
        drained = threading.Event()
        sent = threading.Event()
        session = uuid.uuid4()

        class PausedDrainQueue(InboundQueue):
            @override
            def drain(self, session_id: uuid.UUID) -> list[Inbound]:
                messages = super().drain(session_id)
                drained.set()
                assert sent.wait(timeout=5)
                return messages

        queue = PausedDrainQueue()
        message = Inbound(text="arrived after the initial drain")

        def send() -> None:
            assert drained.wait(timeout=5)
            queue.enqueue(session, message)
            sent.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            sender = executor.submit(send)
            received = asyncio.run(queue.await_messages(session, timeout_sec=0.02))
            sender.result(timeout=5)

        assert received == [message]
        assert queue.pending(session) == 0
        assert not queue._waiters

    def test_cancellation_releases_the_waiter(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()

        async def run() -> None:
            waiter = asyncio.create_task(queue.await_messages(session, timeout_sec=5))
            await asyncio.sleep(0)
            assert queue._waiters[session]
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

        asyncio.run(run())
        assert not queue._waiters

    @pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
    def test_initial_drain_error_releases_the_waiter(
        self,
        error_type: type[Exception],
    ) -> None:
        class FailedDrainQueue(InboundQueue):
            @override
            def drain(self, session_id: uuid.UUID) -> list[Inbound]:
                raise error_type(f"drain failed for {session_id}")

        queue = FailedDrainQueue()
        with pytest.raises(error_type, match="drain failed"):
            asyncio.run(queue.await_messages(uuid.uuid4(), timeout_sec=5))
        assert not queue._waiters

    def test_wakes_on_an_enqueue_from_another_thread(self) -> None:
        """The wait ends when the message arrives, not when a timer expires."""
        queue = InboundQueue()
        session = uuid.uuid4()

        async def run() -> tuple[list[str], float]:
            loop = asyncio.get_running_loop()
            started = loop.time()
            waiter = asyncio.create_task(
                queue.await_messages(session, timeout_sec=10.0),
            )
            # Run the waiter through registration before the sending thread starts.
            await asyncio.sleep(0)
            sender = threading.Thread(
                target=queue.enqueue,
                args=(session, Inbound(text="from elsewhere")),
                daemon=True,
            )
            sender.start()
            drained = await waiter
            sender.join()
            return ([m.text for m in drained], loop.time() - started)

        texts, elapsed = asyncio.run(run())
        assert texts == ["from elsewhere"]
        assert elapsed < 2.0, f"waited {elapsed:.2f}s despite an enqueued message"

    def test_returns_empty_at_the_timeout(self) -> None:
        """A quiet session returns empty so the caller can re-arm.

        A held request cannot be held forever: proxies and load balancers cut
        idle connections, and the caller needs a turn to notice ``stop``.
        """
        queue = InboundQueue()

        started = time.monotonic()
        drained = asyncio.run(queue.await_messages(uuid.uuid4(), timeout_sec=0.01))

        assert drained == []
        assert time.monotonic() - started >= 0.01
        assert not queue._waiters

    def test_a_second_waiter_does_not_steal_the_first_wake(self) -> None:
        """Two waiters on one session both see the queue drained once.

        Only one holds the messages -- ``drain`` is destructive -- but neither
        may hang past its timeout because the other consumed the wakeup.
        """
        queue = InboundQueue()
        session = uuid.uuid4()

        async def run() -> list[list[str]]:
            first = asyncio.create_task(queue.await_messages(session, timeout_sec=2.0))
            second = asyncio.create_task(queue.await_messages(session, timeout_sec=2.0))
            await asyncio.sleep(0)
            _ = queue.enqueue(session, Inbound(text="one message"))
            both = await asyncio.wait_for(asyncio.gather(first, second), timeout=0.5)
            return [[m.text for m in drained] for drained in both]

        results = asyncio.run(run())
        delivered = [texts for texts in results if texts]
        assert delivered == [["one message"]], f"message delivered twice: {results}"


class TestRevokingTheLeaseEndsTheHold:
    """A session's end releases the request its own poller is parked in.

    The run ends its session as it exits, with its poller parked in a hold; a
    hold that outlived the lease kept every exit waiting out its full length.
    """

    def test_the_hold_returns_when_the_lease_is_revoked(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        queue.mark_poller(session)

        async def run() -> list[Inbound]:
            hold = asyncio.create_task(queue.await_messages(session, timeout_sec=3_600))
            await asyncio.sleep(0)
            assert queue._waiters[session]
            queue.forget_poller(session)
            return await asyncio.wait_for(hold, timeout=5)

        assert asyncio.run(run()) == []
        assert not queue._waiters

    def test_a_released_hold_takes_nothing(self) -> None:
        """A message queued as the poller leaves stays queued, not handed to it.

        The leaving run can no longer type what it receives, so a hold that
        drained on release would consume the message and drop it.
        """
        queue = InboundQueue()
        session = uuid.uuid4()
        queue.mark_poller(session)

        async def run() -> list[Inbound]:
            hold = asyncio.create_task(queue.await_messages(session, timeout_sec=3_600))
            await asyncio.sleep(0)
            assert queue._waiters[session]
            _ = queue.enqueue(session, Inbound(text="sent as the run exits"))
            queue.forget_poller(session)
            return await asyncio.wait_for(hold, timeout=5)

        assert asyncio.run(run()) == []
        assert queue.pending(session) == 1


class TestWhereAPollerRuns:
    """A lease keeps where its run says it runs, and forgets it with the lease."""

    def test_a_lease_answers_where_its_run_is_until_it_expires(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()

        assert queue.poller_run(session) is None
        _mark_at(queue, session)
        assert queue.poller_run(session) == _AT
        now[0] = 145.0
        assert queue.poller_run(session) is None
        # Renewed saying nothing, the lease says nothing of where it ran before.
        queue.mark_poller(session)
        assert queue.poller_run(session) == PollerRun()

    def test_a_revoked_lease_forgets_where_its_run_was(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        _mark_at(queue, session)

        queue.forget_poller(session)

        assert queue.poller_run(session) is None
        assert not queue.has_poller(session)

    def test_the_sweep_of_expired_leases_takes_their_runs_with_them(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        expired, live = uuid.uuid4(), uuid.uuid4()
        _mark_at(queue, expired)
        now[0] = 120.0
        _mark_at(queue, live)
        now[0] = 150.0

        assert queue.active_poller_ids() == [live]
        assert queue.poller_run(live) == _AT
        assert expired not in queue._leases


class TestWhichCliPolls:
    """A lease keeps the CLI and run instance its drain named, and nothing else says."""

    def test_a_lease_answers_which_cli_and_run_polls_it(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()

        queue.mark_poller(session, cli="codex", instance="a" * 32)

        assert queue.poller_run(session) == PollerRun(cli="codex", instance="a" * 32)
        # A renewal that names neither leaves the lease nameless, as an older trax's.
        queue.mark_poller(session)
        assert queue.poller_run(session) == PollerRun()

    def test_active_poller_ids_can_be_asked_for_one_clis_alone(self) -> None:
        queue = InboundQueue()
        claude, codex, nameless = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        queue.mark_poller(claude, cli="claude")
        queue.mark_poller(codex, cli="codex")
        queue.mark_poller(nameless)

        assert queue.active_poller_ids(cli="claude") == [claude]
        assert queue.active_poller_ids(cli="codex") == [codex]
        assert queue.active_poller_ids(cli="sagent") == []
        assert queue.active_poller_ids() == [claude, codex, nameless]

    def test_a_run_is_held_off_by_another_runs_live_lease_alone(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()
        first, second = "a" * 32, "b" * 32

        assert not queue.held_by_another(session, cli="claude", instance=first)
        queue.mark_poller(session, cli="claude", instance=first)
        assert not queue.held_by_another(session, cli="claude", instance=first)
        assert queue.held_by_another(session, cli="claude", instance=second)
        # A lease of another CLI is a run the chat no longer takes lines from.
        assert not queue.held_by_another(session, cli="codex", instance=second)
        # A lease that names no run, as the assistant's, holds nobody off.
        queue.mark_poller(session, cli="claude")
        assert not queue.held_by_another(session, cli="claude", instance=second)
        # Nor does one that lapsed, or one that was revoked.
        queue.mark_poller(session, cli="claude", instance=first)
        now[0] = 145.0
        assert not queue.held_by_another(session, cli="claude", instance=second)
        queue.mark_poller(session, cli="claude", instance=first)
        queue.forget_poller(session)
        assert not queue.held_by_another(session, cli="claude", instance=second)


class TestWhatTheRunReportsOfItself:
    """A run says idle, running, waiting for the person, or taken over in a terminal."""

    def test_the_state_a_run_reports_stays_through_its_next_polls(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        run = "a" * 32
        queue.mark_poller(session, cli="claude", instance=run)
        assert queue.poller_run(session) == PollerRun(cli="claude", instance=run)

        assert queue.report_state(session, instance=run, state="requires_action")
        queue.mark_poller(session, cli="claude", instance=run)

        held = queue.poller_run(session)
        assert held is not None
        assert held.state == "requires_action"

    def test_a_report_of_another_run_or_of_no_lease_changes_nothing(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()
        run = "a" * 32

        assert not queue.report_state(session, instance=run, state="running")
        queue.mark_poller(session, cli="claude", instance=run)
        assert not queue.report_state(session, instance="b" * 32, state="taken_over")
        now[0] = 145.0
        assert not queue.report_state(session, instance=run, state="running")

        assert queue.poller_run(session) is None

    def test_a_run_that_polls_as_another_starts_idle(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        queue.mark_poller(session, cli="claude", instance="a" * 32)
        _ = queue.report_state(session, instance="a" * 32, state="taken_over")

        queue.mark_poller(session, cli="claude", instance="b" * 32)
        later = queue.poller_run(session)
        queue.mark_poller(session, cli="claude")
        nameless = queue.poller_run(session)

        assert later is not None
        assert later.state == "idle"
        assert nameless == PollerRun(cli="claude")


class TestRunsThatWentOffline:
    """The server is told which runs stopped polling, to release what they asked."""

    def test_a_lease_that_lapses_is_listed_once(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        lapsed, live = uuid.uuid4(), uuid.uuid4()
        queue.mark_poller(lapsed, cli="claude")
        now[0] = 120.0
        queue.mark_poller(live, cli="claude")
        now[0] = 150.0

        assert queue.take_gone() == [lapsed]
        assert queue.take_gone() == []
        assert queue.has_poller(live)

    def test_a_lease_read_after_it_lapsed_is_listed_too(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()
        queue.mark_poller(session, cli="claude")
        now[0] = 200.0

        assert not queue.has_poller(session)
        assert queue.take_gone() == [session]

    def test_a_revoked_lease_is_listed_and_a_renewed_one_is_not(self) -> None:
        queue = InboundQueue()
        revoked, renewed = uuid.uuid4(), uuid.uuid4()
        queue.mark_poller(revoked, cli="claude")
        queue.mark_poller(renewed, cli="claude")
        queue.mark_poller(renewed, cli="claude")

        queue.forget_poller(revoked)

        assert queue.take_gone() == [revoked]

    def test_a_session_that_never_polled_is_not_listed(self) -> None:
        queue = InboundQueue()

        queue.forget_poller(uuid.uuid4())

        assert queue.take_gone() == []

    def test_the_assistants_lease_is_not_a_run_that_went_offline(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        assistant, revoked = uuid.uuid4(), uuid.uuid4()
        queue.mark_poller(assistant)
        queue.mark_poller(revoked)
        queue.forget_poller(revoked)
        now[0] = 200.0

        assert queue.take_gone() == []
        assert not queue.has_poller(assistant)

    def test_the_list_keeps_the_newest_sessions_when_nothing_takes_it(self) -> None:
        queue = InboundQueue(max_gone=2)
        first, second, third = (uuid.uuid4() for _ in range(3))
        for session in (first, second, third):
            queue.mark_poller(session, cli="claude")
            queue.forget_poller(session)

        assert queue.take_gone() == [second, third]


class TestReplacingARun:
    """A run the server replaces is turned away when it drains again."""

    def test_the_replaced_run_is_held_off_though_no_lease_names_it(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        old, new = "a" * 32, "b" * 32
        queue.mark_poller(session, cli="claude", instance=old)

        queue.retire_run(session)

        assert queue.poller_run(session) is None
        assert queue.held_by_another(session, cli="claude", instance=old)
        assert not queue.held_by_another(session, cli="claude", instance=new)
        queue.mark_poller(session, cli="claude", instance=new)
        assert queue.held_by_another(session, cli="claude", instance=old)

    def test_a_run_whose_lease_lapsed_is_held_off_by_where_it_last_ran(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()
        old, new = "a" * 32, "b" * 32
        queue.mark_poller(session, cli="claude", machine="box", instance=old)
        now[0] = 200.0
        assert queue.take_gone() == [session]

        queue.retire_run(session)

        assert queue.held_by_another(session, cli="claude", instance=old)
        assert not queue.held_by_another(session, cli="claude", instance=new)

    def test_a_session_with_no_run_retires_nobody(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()

        queue.retire_run(session)

        assert not queue.held_by_another(session, cli="claude", instance="a" * 32)

    def test_the_server_keeps_the_newest_retired_runs_alone(self) -> None:
        queue = InboundQueue(max_retired=1)
        first, second = uuid.uuid4(), uuid.uuid4()
        for session in (first, second):
            queue.mark_poller(session, cli="claude", instance="a" * 32)
            queue.retire_run(session)

        assert not queue.held_by_another(first, cli="claude", instance="a" * 32)
        assert queue.held_by_another(second, cli="claude", instance="a" * 32)


class TestTheRunsOnAMachine:
    """A machine's owner sees the live runs that say they run on it, and stops them."""

    def test_a_machine_lists_its_owners_live_cli_runs_alone(self) -> None:
        queue = InboundQueue()
        mine, theirs, elsewhere, assistant = (uuid.uuid4() for _ in range(4))
        queue.mark_poller(mine, machine="box", cli="claude", account="Dan@x.ai")
        queue.mark_poller(theirs, machine="box", cli="claude", account="eve@x.ai")
        queue.mark_poller(elsewhere, machine="gx10", cli="codex", account="dan@x.ai")
        queue.mark_poller(assistant, machine="box", account="dan@x.ai")

        assert queue.runs_on("box", account="dan@X.ai") == {
            mine: PollerRun(machine="box", cli="claude"),
        }

    def test_a_run_whose_lease_lapsed_is_not_listed(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        queue.mark_poller(uuid.uuid4(), machine="box", cli="claude", account="d@x.ai")
        now[0] = 200.0

        assert queue.runs_on("box", account="d@x.ai") == {}

    def test_a_stopped_run_is_refused_and_a_new_run_of_its_session_is_not(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        old, new = "a" * 32, "b" * 32
        queue.mark_poller(session, machine="box", cli="claude", instance=old)

        queue.stop_run(session)

        assert queue.poller_run(session) is None
        assert queue.stopped(session, instance=old)
        assert not queue.stopped(session, instance=new)
        assert not queue.stopped(uuid.uuid4(), instance=old)

    def test_a_session_with_no_live_run_stops_nobody(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()

        queue.stop_run(session)

        assert not queue.stopped(session, instance="a" * 32)

    def test_the_server_keeps_the_newest_stopped_runs_alone(self) -> None:
        queue = InboundQueue(max_stopped=1)
        first, second = uuid.uuid4(), uuid.uuid4()
        for session in (first, second):
            queue.mark_poller(session, cli="claude", instance="a" * 32)
            queue.stop_run(session)

        assert not queue.stopped(first, instance="a" * 32)
        assert queue.stopped(second, instance="a" * 32)


class TestWhereARunWas:
    """Where a run said it ran outlives its lease, for a run to start there again."""

    def test_the_place_stays_after_the_lease_lapses(self) -> None:
        now = [100.0]
        queue = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
        session = uuid.uuid4()
        _mark_at(queue, session)
        now[0] = 200.0

        assert queue.poller_run(session) is None
        assert queue.last_run(session) == _AT

    def test_a_poll_that_names_no_place_leaves_the_last_one(self) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        assert queue.last_run(session) is None
        _mark_at(queue, session)

        queue.mark_poller(session, cli="claude")

        assert queue.last_run(session) == _AT

    def test_the_server_keeps_the_newest_places_alone(self) -> None:
        queue = InboundQueue(max_places=1)
        first, second = uuid.uuid4(), uuid.uuid4()
        _mark_at(queue, first)
        _mark_at(queue, second)

        assert queue.last_run(first) is None
        assert queue.last_run(second) == _AT


_AT = PollerRun(run="chat-1a2b3c4d", machine="gx10", cwd="~/work")
"""Where a detached run on gx10, working in ``~/work``, says it is."""


def _mark_at(queue: InboundQueue, session_id: uuid.UUID) -> None:
    """Renew ``session_id``'s lease as a poll from the run at :data:`_AT` does."""
    queue.mark_poller(session_id, run=_AT.run, machine=_AT.machine, cwd=_AT.cwd)


class TestSendIdempotency:
    """``send_once`` dedups a replayed key without re-enqueuing."""

    def test_send_once_seen_keys_are_fifo_evicted(self) -> None:
        # The dedup record is bounded + FIFO-evicted: the oldest key falls out
        # once the cap is exceeded, so its replay is treated as new; a key still
        # in the window dedups. Two fresh queues isolate each case from the
        # eviction cascade a replay's own re-record would trigger.
        evicted_q = InboundQueue(max_seen_keys=2)
        keys = [uuid.uuid4() for _ in range(3)]
        sid = uuid.uuid4()
        for k in keys:
            evicted_q.send_once(k, [(sid, Inbound(text="x"))])
        # keys[0] was pushed out by keys[2] -> its replay re-enqueues.
        evicted_q.send_once(keys[0], [(sid, Inbound(text="x"))])
        assert evicted_q.pending(sid) == 4

        kept_q = InboundQueue(max_seen_keys=2)
        sid2 = uuid.uuid4()
        survivor = uuid.uuid4()
        kept_q.send_once(survivor, [(sid2, Inbound(text="x"))])
        # Within the window -> replay dedups, no second message.
        kept_q.send_once(survivor, [(sid2, Inbound(text="x"))])
        assert kept_q.pending(sid2) == 1

    def test_send_once_replays_without_re_enqueue(self) -> None:
        q = InboundQueue()
        key = uuid.uuid4()
        sid = uuid.uuid4()
        first = q.send_once(key, [(sid, Inbound(text="hi"))])
        assert first == [sid]
        # Same key again: replay the receipt, enqueue nothing more.
        second = q.send_once(key, [(sid, Inbound(text="hi"))])
        assert second == [sid]
        assert q.pending(sid) == 1

    def test_a_key_remembers_who_first_sent_under_it(self) -> None:
        q = InboundQueue(max_seen_keys=1)
        key, other = uuid.uuid4(), uuid.uuid4()
        sid = uuid.uuid4()
        q.send_once(key, [(sid, Inbound(text="hi", source="ada@example.com"))])
        q.send_once(key, [(sid, Inbound(text="hi", source="jan@other.org"))])

        assert q.key_sender(key) == "ada@example.com"
        assert q.key_sender(other) is None
        # A key the bound pushed out is forgotten, as is one that had no sender.
        q.send_once(other, [(sid, Inbound(text="anon"))])
        assert (q.key_sender(key), q.key_sender(other)) == (None, None)

    def test_send_once_concurrent_same_key_enqueues_once(self) -> None:
        # Two concurrent same-key sends must not BOTH pass the dedup check and
        # double-enqueue: send_once holds the lock across check+enqueue+record,
        # so exactly one delivery lands. A barrier maximizes the overlap.
        q = InboundQueue()
        key = uuid.uuid4()
        sid = uuid.uuid4()
        barrier = threading.Barrier(2)
        results: list[list[uuid.UUID]] = []
        lock = threading.Lock()

        def _send() -> None:
            barrier.wait()
            delivered = q.send_once(key, [(sid, Inbound(text="race"))])
            with lock:
                results.append(delivered)

        threads = [threading.Thread(target=_send) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Both callers get the same receipt; exactly one message is queued.
        assert results == [[sid], [sid]]
        assert q.pending(sid) == 1


class TestAChatsLinesOnTheirWay:
    """What a delete counts as posted: lines queued for a chat, or taken and unrecorded."""

    _CHAT = uuid.UUID(int=1)
    _OTHER = uuid.UUID(int=2)

    def _line(self, sender: str, *, chat: uuid.UUID = _CHAT) -> Inbound:
        return Inbound(
            text="q",
            source=sender,
            context=CanvasMessageContext(
                canvas_id=uuid.UUID(int=9),
                visible_visuals=[],
                conversation_id=chat,
            ),
        )

    def test_a_line_is_counted_queued_then_taken_until_its_chat_records_it(
        self,
    ) -> None:
        queue = InboundQueue()
        session = uuid.uuid4()
        queue.enqueue(session, self._line("Bob@example.com"))
        queue.enqueue(session, self._line("ada@example.com", chat=self._OTHER))
        queue.enqueue(session, Inbound(text="no chat", source="kim@example.com"))

        queued = queue.conversation_senders(self._CHAT)
        queue.hand_out(queue.drain(session), taker=None)
        taken = queue.conversation_senders(self._CHAT)
        queue.recorded(self._CHAT, senders=["bob@example.com", "zed@example.com"])

        assert queued == taken == ["Bob@example.com"]
        assert queue.conversation_senders(self._CHAT) == []
        assert queue.conversation_senders(self._OTHER) == ["ada@example.com"]

    def test_recording_forgets_one_taken_line_per_sender(self) -> None:
        queue = InboundQueue()
        queue.hand_out([self._line("bob@example.com")] * 2, taker=None)

        queue.recorded(self._CHAT, senders=["bob@example.com"])

        assert queue.conversation_senders(self._CHAT) == ["bob@example.com"]

    def test_a_chats_delete_drops_its_queued_lines_and_forgets_its_taken_ones(
        self,
    ) -> None:
        queue = InboundQueue(max_per_session=2)
        session = uuid.uuid4()
        queue.hand_out([self._line(f"p{n}@example.com") for n in range(3)], taker=None)
        queue.enqueue(session, self._line("bob@example.com"))
        queue.enqueue(session, self._line("ada@example.com", chat=self._OTHER))

        capped = queue.conversation_senders(self._CHAT)
        dropped = queue.drop_conversation(self._CHAT)

        assert capped == ["bob@example.com", "p1@example.com", "p2@example.com"]
        assert dropped == 1
        assert queue.conversation_senders(self._CHAT) == []
        assert [line.source for line in queue.drain(session)] == ["ada@example.com"]

    def test_a_taken_line_never_recorded_stops_counting_after_its_ttl(self) -> None:
        now = [0.0]
        queue = InboundQueue(handed_ttl_sec=600.0, _clock=lambda: now[0])
        queue.hand_out([self._line("bob@example.com")], taker=None)

        now[0] = 599.0
        within = queue.conversation_senders(self._CHAT)
        now[0] = 600.0

        assert (within, queue.conversation_senders(self._CHAT)) == (
            ["bob@example.com"],
            [],
        )

    def test_over_the_cap_the_chat_handed_a_line_longest_ago_is_forgotten(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = InboundQueue(max_handed_chats=2)
        third = uuid.UUID(int=3)
        queue.hand_out([self._line("ada@example.com")], taker=None)
        queue.hand_out([self._line("bob@example.com", chat=self._OTHER)], taker=None)
        queue.hand_out([self._line("kim@example.com")], taker=None)

        with caplog.at_level("WARNING"):
            queue.hand_out([self._line("zed@example.com", chat=third)], taker=None)

        assert [
            queue.conversation_senders(chat)
            for chat in (self._CHAT, self._OTHER, third)
        ] == [["ada@example.com", "kim@example.com"], [], ["zed@example.com"]]
        assert f"forgot those of chat {self._OTHER}" in caplog.text


class TestTheKeyThatTookALine:
    """Whose chat line each key took, which lets that key act for the line's poster."""

    _TAKER = uuid.UUID(int=7)
    _CHAT = uuid.UUID(int=1)
    _BOB = "bob@example.com"

    def _line(self, sender: str, *, chat: uuid.UUID | None = _CHAT) -> Inbound:
        context = CanvasMessageContext(
            canvas_id=uuid.UUID(int=9),
            visible_visuals=[],
            conversation_id=chat,
        )
        return Inbound(text="q", source=sender, context=context)

    def test_only_the_taker_holds_the_posters_turn(self) -> None:
        queue = InboundQueue()

        queue.hand_out([self._line("Bob@example.com")], taker=self._TAKER)

        assert queue.took_line(self._TAKER, poster="BOB@example.com")
        assert not queue.took_line(uuid.UUID(int=8), poster=self._BOB)
        assert not queue.took_line(self._TAKER, poster="ada@example.com")

    def test_a_line_outside_a_chat_or_from_no_one_holds_no_turn(self) -> None:
        queue = InboundQueue()
        anonymous = self._line(self._BOB)

        queue.hand_out([self._line(self._BOB, chat=None)], taker=self._TAKER)
        queue.hand_out([Inbound(text="q", source="kim@example.com")], taker=self._TAKER)
        queue.hand_out(
            [Inbound(text="q", context=anonymous.context)],
            taker=self._TAKER,
        )

        assert not queue.took_line(self._TAKER, poster=self._BOB)
        assert not queue.took_line(self._TAKER, poster="kim@example.com")

    def test_a_browser_drain_holds_no_turn_and_evicts_none(self) -> None:
        queue = InboundQueue(max_turns=1)
        queue.hand_out([self._line(self._BOB)], taker=self._TAKER)

        queue.hand_out([self._line("ada@example.com")], taker=None)

        assert queue.took_line(self._TAKER, poster=self._BOB)

    def test_a_turn_outlives_the_lines_record_and_ends_at_its_ttl(self) -> None:
        now = [0.0]
        queue = InboundQueue(turn_ttl_sec=3_600.0, _clock=lambda: now[0])
        queue.hand_out([self._line(self._BOB)], taker=self._TAKER)
        queue.recorded(self._CHAT, senders=[self._BOB])

        now[0] = 3_599.0
        within = queue.took_line(self._TAKER, poster=self._BOB)
        now[0] = 3_600.0

        assert (within, queue.took_line(self._TAKER, poster=self._BOB)) == (True, False)

    def test_a_later_line_renews_the_turn(self) -> None:
        now = [0.0]
        queue = InboundQueue(turn_ttl_sec=10.0, _clock=lambda: now[0])
        queue.hand_out([self._line(self._BOB)], taker=self._TAKER)
        now[0] = 8.0
        queue.hand_out([self._line(self._BOB)], taker=self._TAKER)

        now[0] = 15.0

        assert queue.took_line(self._TAKER, poster=self._BOB)

    def test_over_the_cap_the_turn_taken_longest_ago_is_forgotten(self) -> None:
        queue = InboundQueue(max_turns=2)
        for sender in ("ada@example.com", self._BOB, "ada@example.com"):
            queue.hand_out([self._line(sender)], taker=self._TAKER)

        queue.hand_out([self._line("kim@example.com")], taker=self._TAKER)

        assert [
            queue.took_line(self._TAKER, poster=each)
            for each in ("ada@example.com", self._BOB, "kim@example.com")
        ] == [True, False, True]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
