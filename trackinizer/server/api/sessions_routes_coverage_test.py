"""Unit coverage for session lifecycle and the send route."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException, Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import sessions_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.wire_sessions import (
    SendMessage,
)


@pytest.mark.asyncio
async def test_bare_multiroom_send_refuses_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    session_id = uuid4()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(store=store)),
            "state": {},
        },
    )
    resolve = AsyncMock(return_value=[(session_id, ("z", "a"))])
    monkeypatch.setattr(store, "resolve_live_sessions", resolve)
    monkeypatch.setattr(sessions_routes, "resolve_asker", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as refused:
        await sessions_routes.send_message_route(
            SendMessage(actor="runner", text="hello"),
            request,
            make_test_identity(),
        )
    assert refused.value.status_code == 409
    assert refused.value.detail == (
        "@runner is in rooms ['a', 'z']; address one explicitly (e.g. @runner:a)"
    )
    resolve.assert_awaited_once_with("runner", room=None)


@pytest.mark.asyncio
async def test_attach_requires_write_access_and_chat_run_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity(role="viewer")
    session = AgentSession(opened_by_api_key_id=identity.api_key_id)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store),
            ),
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    access = AsyncMock()
    monkeypatch.setattr(sessions_routes, "require_session_access", access)
    assert (
        await sessions_routes._require_attach(
            request,
            identity,
            session_id=session.id,
        )
        is session
    )
    access.assert_awaited_once_with(request, identity, session=session, mode="write")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
