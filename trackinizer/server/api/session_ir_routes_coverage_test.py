"""Unit coverage for reaped-session writes and usage pagination."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call
from uuid import uuid4

from fastapi import Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.api import session_ir_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.config import Config
from trackinizer.server.store.session_ir import SessionManifest
from trackinizer.server.visuals.session_usage import SessionUsage
from trackinizer.types.inquiries import AgentSession
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import AppendRecordsRequest


@pytest.mark.asyncio
async def test_ended_append_revives_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    session = AgentSession(
        cli="codex",
        ended=datetime.now(UTC),
        opened_by_api_key_id=identity.api_key_id,
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store, config=Config()),
            ),
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    monkeypatch.setattr(session_ir_routes, "require_session_access", AsyncMock())
    revive = AsyncMock(return_value=True)
    append = AsyncMock(return_value=(0, 0, 0))
    seen = AsyncMock()
    order = MagicMock()
    order.attach_mock(revive, "revive")
    order.attach_mock(append, "append")
    order.attach_mock(seen, "seen")
    monkeypatch.setattr(session_ir_routes, "revive_if_reaped", revive)
    monkeypatch.setattr(store, "append_session_records", append)
    monkeypatch.setattr(store, "record_session_seen", seen)
    response = await session_ir_routes.append_session_records_route(
        session.id,
        request,
        AppendRecordsRequest(),
        identity,
    )
    assert (
        response.part,
        response.written,
        response.skipped,
        response.slash_commands,
    ) == (
        None,
        0,
        0,
        0,
    )
    revive.assert_awaited_once_with(store, session_id=session.id)
    append.assert_awaited_once_with(session.id, [], restart=False, slash_commands=[])
    seen.assert_awaited_once()
    assert [entry[0] for entry in order.mock_calls] == ["revive", "append", "seen"]


@pytest.mark.asyncio
async def test_usage_advances_exclusive_cursor_and_resets_for_each_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    session = AgentSession(cli="codex", opened_by_api_key_id=identity.api_key_id)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store, config=Config()),
            ),
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    monkeypatch.setattr(session_ir_routes, "require_session_access", AsyncMock())
    monkeypatch.setattr(
        session_ir_routes,
        "resolve_asker",
        AsyncMock(return_value=None),
    )
    manifests = [
        SessionManifest(
            part=part,
            name=f"part-{part}",
            metadata={},
            ir_id=uuid4(),
            format="codex",
            records=9,
        )
        for part in (0, 1)
    ]
    monkeypatch.setattr(
        store,
        "read_session_manifests",
        AsyncMock(return_value=manifests),
    )
    rows = [
        SessionRecordRow.of(
            session_id=session.id,
            part=0,
            idx=idx,
            record=UserMessage(content=str(idx)),
        )
        for idx in (3, 5, 8)
    ]
    read = AsyncMock(side_effect=[rows[:2], rows[2:], []])
    monkeypatch.setattr(store, "read_session_records", read)
    monkeypatch.setattr(session_ir_routes, "MAX_LIST_LIMIT", 2)
    expected = SessionUsage(input_tokens=7, output_tokens=3)
    usage = MagicMock(return_value=expected)
    monkeypatch.setattr(session_ir_routes, "session_usage", usage)
    before = datetime.now(UTC).date()
    assert (
        await session_ir_routes.read_session_usage_route(
            session.id,
            request,
            identity,
        )
        is expected
    )
    assert read.await_args_list == [
        call(session.id, part=0, after_idx=-1, limit=2, plaintext_only=True),
        call(session.id, part=0, after_idx=5, limit=2, plaintext_only=True),
        call(session.id, part=1, after_idx=-1, limit=2, plaintext_only=True),
    ]
    usage.assert_called_once_with(rows, formats={0: "codex", 1: "codex"}, today=before)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
