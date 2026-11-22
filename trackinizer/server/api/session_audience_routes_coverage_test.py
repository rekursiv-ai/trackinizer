"""Unit coverage for owner-controlled transcript visibility."""

from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException, Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.access_policy import AccessDeniedError
from trackinizer.server.api import session_audience_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.session_identity import request_principal
from trackinizer.server.config import Config
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.wire_sessions import SessionAudienceChange


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["open", "private"])
@pytest.mark.parametrize(
    "chat",
    [
        False,
    ],
)
async def test_owner_visibility_forwards_audit_and_chat_fanout(
    monkeypatch: pytest.MonkeyPatch,
    audience: Literal["open", "private"],
    chat: bool,
) -> None:
    store, engine = make_store()
    identity = make_test_identity()
    conversation = uuid4()
    session = AgentSession(
        account=identity.email,
        audience="open",
        cli="codex",
        cli_session_id=f"chat:{conversation}" if chat else "vendor-id",
        opened_by_api_key_id=identity.api_key_id,
    )
    sessions = [session.id, uuid4()] if chat else [session.id]
    moved = sessions[:1]
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    store=store,
                    config=Config(),
                ),
            ),
            "state": {},
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    opener = AsyncMock(return_value="another@example.com")
    monkeypatch.setattr(session_audience_routes, "opener_email", opener)
    update = AsyncMock(return_value=moved)
    monkeypatch.setattr(store, "set_session_audience", update)
    principal = await request_principal(request, identity)
    response = await session_audience_routes.set_session_audience_route(
        session.id,
        SessionAudienceChange(audience=audience),
        request,
        identity,
    )
    assert (response.session_id, response.audience, response.sessions) == (
        session.id,
        audience,
        moved,
    )
    update.assert_awaited_once_with(
        sessions,
        audience=audience,
        replacing=frozenset({"open", "private"}) - {audience},
        refusing=frozenset({"staff"}),
        api_key_id=identity.api_key_id,
        actor=identity.email,
        reason="hidden by its owner" if audience == "private" else "shown again",
        principal=principal,
        caller=identity.email,
    )
    opener.assert_awaited_once_with(
        engine.conn,
        api_key_id=session.opened_by_api_key_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audience", "owner", "status", "code"),
    [("staff", True, 409, "session_staff"), ("open", False, 403, "session_not_owner")],
)
async def test_visibility_refuses_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    audience: str,
    owner: bool,
    status: int,
    code: str,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    session = AgentSession(
        account=identity.email if owner else "other@example.com",
        audience=audience,
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
    monkeypatch.setattr(
        session_audience_routes,
        "opener_email",
        AsyncMock(return_value=None),
    )
    update = AsyncMock()
    monkeypatch.setattr(store, "set_session_audience", update)
    with pytest.raises(HTTPException) as refused:
        await session_audience_routes.set_session_audience_route(
            session.id,
            SessionAudienceChange(audience="private"),
            request,
            identity,
        )
    assert refused.value.status_code == status
    assert refused.value.detail == {"code": code}
    update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AccessDeniedError("denied"), 403, "session_not_owner"),
        (ConflictError("raised meanwhile"), 409, "session_staff"),
    ],
)
async def test_visibility_translates_store_refusals(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
) -> None:
    store, _engine = make_store()
    identity = make_test_identity()
    session = AgentSession(account=identity.email, audience="open")
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store, config=Config()),
            ),
            "state": {},
        },
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    monkeypatch.setattr(
        session_audience_routes,
        "opener_email",
        AsyncMock(return_value=None),
    )
    update = AsyncMock(side_effect=error)
    monkeypatch.setattr(store, "set_session_audience", update)
    with pytest.raises(HTTPException) as refused:
        await session_audience_routes.set_session_audience_route(
            session.id,
            SessionAudienceChange(audience="private"),
            request,
            identity,
        )
    assert refused.value.status_code == status
    assert refused.value.detail == {"code": code}
    assert refused.value.__cause__ is error
    assert update.await_count == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
