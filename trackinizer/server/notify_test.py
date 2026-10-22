"""Tests for transactional notify plumbing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast, override
from uuid import UUID

import asyncio
import json

import pytest

from trackinizer.conftest import (
    FakeEngine,
    executed_sql,
    make_conn,
    new_uuid,
)
from trackinizer.lib.postgres import Conn, DatabaseEngine
from trackinizer.server.notify import (
    NOTIFICATION_BUFFER,
    NOTIFY_CHANNEL,
    Notification,
    _publish_notifications,
    iter_changed_ids,
    iter_sse_events,
    notify_after_commit,
    tx,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class TestTx:
    @pytest.mark.asyncio
    async def test_code_changes_on_success(self) -> None:
        conn = make_conn()
        async with tx(cast(Conn, conn)):
            pass
        assert executed_sql(conn) == ["BEGIN", "COMMIT"]

    @pytest.mark.asyncio
    async def test_rollback_on_exception(self) -> None:
        conn = make_conn()
        with pytest.raises(ValueError, match="boom"):
            async with tx(cast(Conn, conn)):
                raise ValueError("boom")
        assert executed_sql(conn) == ["BEGIN", "ROLLBACK"]

    @pytest.mark.asyncio
    async def testnotify_after_commit_suppresses_rollback_notifications(self) -> None:
        engine = FakeEngine()

        async def fail_after_buffering() -> None:
            async with notify_after_commit():
                buffer = NOTIFICATION_BUFFER.get()
                assert buffer is not None
                buffer.append(
                    Notification(
                        engine=cast(DatabaseEngine, engine),
                        subject_id=new_uuid(),
                    ),
                )
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await fail_after_buffering()
        assert engine.notify_calls == []

    @pytest.mark.asyncio
    async def test_publish_notifications_swallows_engine_errors(self) -> None:
        engine = FakeEngine()
        engine.notify_calls.clear()
        engine.notify_error = RuntimeError("network down")
        # Must not raise -- the transaction has committed; notify failures
        # are best-effort post-commit fanout.
        await _publish_notifications(
            [
                Notification(
                    engine=cast(DatabaseEngine, engine),
                    subject_id=new_uuid(),
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_publish_dedups_by_subject_id(self) -> None:
        # A cascade over N ancestors buffers N+1 entries, and a subject often
        # repeats (the changed row plus its own emit). Publishing one NOTIFY
        # per buffered entry costs N+1 round-trips for K distinct subjects.
        # Dedup by subject_id so each affected inquiry wakes its subscribers
        # exactly once -- the SSE relay carries only the id, so a second NOTIFY
        # for the same id is pure redundant latency.
        engine = FakeEngine()
        engine.notify_calls.clear()
        a, b = new_uuid(), new_uuid()
        await _publish_notifications(
            [
                Notification(engine=cast(DatabaseEngine, engine), subject_id=a),
                Notification(engine=cast(DatabaseEngine, engine), subject_id=b),
                Notification(engine=cast(DatabaseEngine, engine), subject_id=a),
                Notification(engine=cast(DatabaseEngine, engine), subject_id=b),
                Notification(engine=cast(DatabaseEngine, engine), subject_id=a),
            ],
        )
        published = {json.loads(payload)["id"] for _, payload in engine.notify_calls}
        assert published == {str(a), str(b)}
        assert len(engine.notify_calls) == 2, (
            "expected one NOTIFY per distinct subject_id, not one per buffered entry"
        )


class TestPingAudience:
    @pytest.mark.asyncio
    async def test_a_ping_names_the_audience_its_viewers_must_read(self) -> None:
        engine = FakeEngine()
        engine.notify_calls.clear()
        hidden, plain = new_uuid(), new_uuid()
        await _publish_notifications(
            [
                Notification(
                    engine=cast(DatabaseEngine, engine),
                    subject_id=hidden,
                    audience="staff",
                ),
                Notification(engine=cast(DatabaseEngine, engine), subject_id=plain),
            ],
        )
        assert [json.loads(payload) for _, payload in engine.notify_calls] == [
            {"id": str(hidden), "audience": "staff"},
            {"id": str(plain)},
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "audiences",
        [("staff", None), (None, "staff")],
        ids=["audience first", "audience last"],
    )
    async def test_a_ping_for_every_viewer_covers_the_one_for_readers(
        self,
        audiences: tuple[str | None, str | None],
    ) -> None:
        # The ping for every viewer already wakes the readers, so the second says
        # nothing more; and it must not be the only one sent, which would wake no
        # viewer who does not read the audience.
        engine = FakeEngine()
        engine.notify_calls.clear()
        subject = new_uuid()
        await _publish_notifications(
            [
                Notification(
                    engine=cast(DatabaseEngine, engine),
                    subject_id=subject,
                    audience=audience,
                )
                for audience in audiences
            ],
        )
        assert [json.loads(payload) for _, payload in engine.notify_calls] == [
            {"id": str(subject)},
        ]

    @pytest.mark.asyncio
    async def test_a_stream_relays_a_transcripts_ping_only_when_its_viewer_hears_it(
        self,
    ) -> None:
        """Every ping that names no audience is relayed, and its viewer is not asked."""
        engine = FakeEngine()
        hidden, shown, plain = new_uuid(), new_uuid(), new_uuid()
        engine.listen_messages = [
            json.dumps({"id": str(hidden), "audience": "private"}),
            json.dumps({"id": str(shown), "audience": "open"}),
            json.dumps({"id": str(plain)}),
        ]
        hearing = _Hearing(heard=frozenset({shown}))

        relayed = [
            frame
            async for frame in iter_sse_events(
                cast(DatabaseEngine, engine),
                hears=hearing.hears,
            )
        ]

        assert relayed == [b": open\n\n", _frame(shown), _frame(plain)]
        assert hearing.asked == [(hidden, "private"), (shown, "open")]

    @pytest.mark.asyncio
    async def test_the_canvas_stream_drops_it_too(self) -> None:
        engine = FakeEngine()
        hidden, shown = new_uuid(), new_uuid()
        engine.listen_messages = [
            json.dumps({"id": str(hidden), "audience": "staff"}),
            json.dumps({"id": str(shown)}),
        ]

        changed = [
            subject
            async for subject in iter_changed_ids(
                cast(DatabaseEngine, engine),
                hears=_Hearing(heard=frozenset()).hears,
            )
        ]

        assert changed == [str(shown)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        ['{"id": "abc-123", "audience": 5}', '{"id": "abc-123", "audience": "open"}'],
        ids=["audience no name", "session no uuid"],
    )
    async def test_a_transcripts_ping_that_names_no_session_is_logged_and_dropped(
        self,
        payload: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        engine = FakeEngine()
        engine.listen_messages = [payload]
        hearing = _Hearing(heard=frozenset())
        with caplog.at_level("WARNING"):
            frames = [
                frame
                async for frame in iter_sse_events(
                    cast(DatabaseEngine, engine),
                    hears=hearing.hears,
                )
            ]
        assert frames == [b": open\n\n"]
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert hearing.asked == []


class TestSseEvents:
    @pytest.mark.asyncio
    async def test_malformed_payload_is_logged_not_silently_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A malformed NOTIFY payload (not JSON, or missing ``id``) is dropped
        # so one bad row can't kill the stream -- but it must be LOGGED, not
        # silently swallowed, or a payload-shape regression is invisible.
        engine = FakeEngine()
        engine.listen_messages = [
            "not json",
            '{"no_id": true}',
            '{"id": "abc-123"}',
        ]
        with caplog.at_level("WARNING"):
            frames = [
                frame
                async for frame in iter_sse_events(
                    cast(DatabaseEngine, engine),
                    hears=_DEAF.hears,
                )
            ]
        # Only the well-formed payload yields a frame.
        assert frames == [b": open\n\n", b'data: {"id": "abc-123"}\n\n']
        # Both malformed payloads were logged (one per drop), naming the
        # channel and the payload, so an operator can find the producer.
        assert [
            (r.name, r.getMessage()) for r in caplog.records if r.levelname == "WARNING"
        ] == [
            (
                "trackinizer.server.notify",
                f"dropping malformed NOTIFY payload on {NOTIFY_CHANNEL}: {payload!r}",
            )
            for payload in ["not json", '{"no_id": true}']
        ]

    @pytest.mark.asyncio
    async def test_quiet_stream_opens_at_once(self) -> None:
        # A proxy in front of production holds response headers until the first
        # body byte; a stream with no change never opened and got a 524 at 125 s.
        engine = _QueueEngine()
        stream = iter_sse_events(cast(DatabaseEngine, engine), hears=_DEAF.hears)
        async with asyncio.timeout(1):
            assert await anext(stream) == b": open\n\n"
        await stream.aclose()

    @pytest.mark.asyncio
    async def test_quiet_stream_sends_keepalives_and_still_relays(self) -> None:
        # A keep-alive must not end the subscription: cancelling a pending
        # ``anext`` would close the engine's listen generator.
        engine = _QueueEngine()
        stream = iter_sse_events(
            cast(DatabaseEngine, engine),
            hears=_DEAF.hears,
            keepalive_sec=0.01,
        )
        async with asyncio.timeout(1):
            assert await anext(stream) == b": open\n\n"
            assert await anext(stream) == b"event: ping\ndata: {}\n\n"
            assert await anext(stream) == b"event: ping\ndata: {}\n\n"
            engine.queue.put_nowait('{"id": "abc-123"}')
            frame = await anext(stream)
            while frame == b"event: ping\ndata: {}\n\n":
                frame = await anext(stream)
        assert frame == b'data: {"id": "abc-123"}\n\n'
        assert engine.channels == [NOTIFY_CHANNEL]
        await stream.aclose()

    @pytest.mark.asyncio
    async def test_a_change_right_after_open_reaches_the_stream(self) -> None:
        # A client starts its reads once the stream says ``open`` and counts on
        # the stream for every change after. The stream subscribed only after it
        # said ``open``, so a change in between reached neither: the client had
        # to read everything again on every open.
        engine = _QueueEngine()
        stream = iter_sse_events(
            cast(DatabaseEngine, engine),
            hears=_DEAF.hears,
            keepalive_sec=0.01,
        )
        async with asyncio.timeout(1):
            assert await anext(stream) == b": open\n\n"
            engine.publish('{"id": "abc-123"}')
            frame = await anext(stream)
            while frame == b"event: ping\ndata: {}\n\n":
                frame = await anext(stream)
        assert frame == b'data: {"id": "abc-123"}\n\n'
        await stream.aclose()

    @pytest.mark.asyncio
    async def test_closing_the_stream_ends_the_subscription(self) -> None:
        # A client that disconnects must not leave its queue on the bus.
        engine = _QueueEngine()
        stream = iter_sse_events(
            cast(DatabaseEngine, engine),
            hears=_DEAF.hears,
            keepalive_sec=0.01,
        )
        async with asyncio.timeout(1):
            await anext(stream)
            await anext(stream)
        assert engine.listening == 1
        await stream.aclose()
        assert engine.listening == 0


@dataclass(frozen=True, slots=True, kw_only=True)
class _Hearing:
    """A viewer who hears of the writes to these sessions' transcripts alone."""

    heard: frozenset[UUID]
    asked: list[tuple[UUID, str]] = field(default_factory=list)

    async def hears(self, session_id: UUID, audience: str) -> bool:
        """Note what was asked, and hear it when the session is one of ``heard``."""
        self.asked.append((session_id, audience))
        return session_id in self.heard


_DEAF: Final = _Hearing(heard=frozenset())
"""A viewer who hears of no transcript's write."""


def _frame(subject: object) -> bytes:
    return f'data: {{"id": "{subject}"}}\n\n'.encode()


class _QueueEngine(FakeEngine):
    """A ``FakeEngine`` whose listen waits on a queue, as a quiet channel does."""

    def __init__(self) -> None:
        super().__init__()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.listening = 0
        self.channels: list[str] = []

    @override
    def listen(self, channel: str) -> AsyncGenerator[str]:
        self.channels.append(channel)
        return self._listen()

    def publish(self, payload: str) -> None:
        """Deliver ``payload`` as the engines' bus does: to a subscribed listener only."""
        if self.listening:
            self.queue.put_nowait(payload)

    async def _listen(self) -> AsyncGenerator[str]:
        self.listening += 1
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.listening -= 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
