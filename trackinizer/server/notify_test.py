"""Tests for transactional notify plumbing."""

from __future__ import annotations

from typing import Any, cast

import json

import pytest

from trackinizer.conftest import (
    FakeEngine,
    executed_sql,
    make_conn,
    new_uuid,
)
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.notify import (
    NOTIFICATION_BUFFER,
    Notification,
    _publish_notifications,
    iter_sse_events,
    notify_after_commit,
    tx,
)


class TestTx:
    @pytest.mark.asyncio
    async def test_code_changes_on_success(self) -> None:
        conn = make_conn()
        async with tx(cast(Any, conn)):
            pass
        assert executed_sql(conn) == ["BEGIN", "COMMIT"]

    @pytest.mark.asyncio
    async def test_rollback_on_exception(self) -> None:
        conn = make_conn()
        with pytest.raises(ValueError, match="boom"):
            async with tx(cast(Any, conn)):
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
                    )
                )
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await fail_after_buffering()
        assert engine.notify_calls == []

    @pytest.mark.asyncio
    async def test_publish_notifications_swallows_engine_errors(self) -> None:
        engine = FakeEngine()
        engine.notify_calls.clear()

        async def boom(channel: str, payload: str) -> None:
            del channel, payload
            raise RuntimeError("network down")

        # Test mock attribute patch: replace the bound method with a
        # function whose signature drops ``self``. The checker can't
        # narrow this case (and shouldn't -- in production this would
        # be a bug).
        engine.notify = boom  # ty: ignore[invalid-assignment]
        # Must not raise -- the transaction has committed; notify failures
        # are best-effort post-commit fanout.
        await _publish_notifications(
            [
                Notification(
                    engine=cast(DatabaseEngine, engine),
                    subject_id=new_uuid(),
                )
            ]
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
            ]
        )
        published = {json.loads(payload)["id"] for _, payload in engine.notify_calls}
        assert published == {str(a), str(b)}
        assert len(engine.notify_calls) == 2, (
            "expected one NOTIFY per distinct subject_id, not one per buffered entry"
        )


class TestSseEvents:
    @pytest.mark.asyncio
    async def test_malformed_payload_is_logged_not_silently_dropped(
        self, caplog: pytest.LogCaptureFixture
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
            frames = [frame async for frame in iter_sse_events(cast(Any, engine))]
        # Only the well-formed payload yields a frame.
        assert frames == [b'data: {"id": "abc-123"}\n\n']
        # Both malformed payloads were logged (one per drop).
        drops = [r for r in caplog.records if "malformed" in r.message.lower()]
        assert len(drops) == 2


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
