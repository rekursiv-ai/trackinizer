"""Tests for the in-process inbound-message queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import threading
import time
import uuid

from trackinizer.server.inbound import Inbound, InboundQueue


if TYPE_CHECKING:
    import pytest


class TestInboundQueue:
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
        q.enqueue(sid, Inbound(text="3"))  # evicts "1"
        assert [m.text for m in q.drain(sid)] == ["2", "3"]

    def test_cap_drop_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        # An overflow drop loses an undelivered message (a stuck/absent poller)
        # -- it must WARN, not silently swallow, or the lost-message signal is
        # invisible. One warning per dropped message.
        q = InboundQueue(max_per_session=1)
        sid = uuid.uuid4()
        q.enqueue(sid, Inbound(text="1"))
        with caplog.at_level("WARNING"):
            q.enqueue(sid, Inbound(text="2"))  # evicts "1"
            q.enqueue(sid, Inbound(text="3"))  # evicts "2"
        drops = [r for r in caplog.records if "dropped oldest" in r.message]
        assert len(drops) == 2

    def test_inbound_carries_room(self) -> None:
        q = InboundQueue()
        sid = uuid.uuid4()
        q.enqueue(sid, Inbound(text="hi", source="alice@x", room="sear"))
        (msg,) = q.drain(sid)
        assert (msg.text, msg.source, msg.room) == ("hi", "alice@x", "sear")


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

    def test_wakes_on_an_enqueue_from_another_thread(self) -> None:
        """The wait ends when the message arrives, not when a timer expires."""
        queue = InboundQueue()
        session = uuid.uuid4()

        async def run() -> tuple[list[str], float]:
            loop = asyncio.get_running_loop()

            def send_soon() -> None:
                time.sleep(0.1)
                _ = queue.enqueue(session, Inbound(text="from elsewhere"))

            threading.Thread(target=send_soon, daemon=True).start()
            started = loop.time()
            drained = await queue.await_messages(session, timeout_sec=10.0)
            return ([m.text for m in drained], loop.time() - started)

        texts, elapsed = asyncio.run(run())
        assert texts == ["from elsewhere"]
        # Woken by the enqueue: far below the timeout, above the send delay.
        assert elapsed < 2.0, f"waited {elapsed:.2f}s for a message sent at 0.1s"

    def test_returns_empty_at_the_timeout(self) -> None:
        """A quiet session returns empty so the caller can re-arm.

        A held request cannot be held forever: proxies and load balancers cut
        idle connections, and the caller needs a turn to notice ``stop``.
        """
        queue = InboundQueue()

        started = time.monotonic()
        drained = asyncio.run(queue.await_messages(uuid.uuid4(), timeout_sec=0.2))

        assert drained == []
        assert time.monotonic() - started >= 0.2

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
            await asyncio.sleep(0.1)
            _ = queue.enqueue(session, Inbound(text="one message"))
            both = await asyncio.gather(first, second)
            return [[m.text for m in drained] for drained in both]

        results = asyncio.run(run())
        delivered = [texts for texts in results if texts]
        assert delivered == [["one message"]], f"message delivered twice: {results}"


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
