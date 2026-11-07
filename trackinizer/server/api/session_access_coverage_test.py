"""Unit coverage for session write guards and transactional policy checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

import pytest

from trackinizer.conftest import (
    RecordingPolicy,
    executed_sql,
    make_store,
    set_field_row,
)
from trackinizer.server.access_policy import AccessDeniedError, Principal, Resource
from trackinizer.server.api import session_access
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.private import chat_sessions
from trackinizer.types.inquiries import AgentSession


@pytest.mark.parametrize("key", [None, UUID(int=1), UUID(int=2)])
def test_viewer_write_requires_matching_nonempty_opening_key(key: UUID | None) -> None:
    identity = make_test_identity(role="viewer", api_key_id=key)
    session = AgentSession(opened_by_api_key_id=UUID(int=1))
    if key == session.opened_by_api_key_id:
        assert session_access.require_session_write_access(identity, session) is None
    else:
        with pytest.raises(HTTPException) as refused:
            session_access.require_session_write_access(identity, session)
        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "Own session key required",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, "owner@example.com"])
@pytest.mark.parametrize("deny", [False, True])
async def test_row_write_asks_policy_inside_transaction(
    monkeypatch: pytest.MonkeyPatch,
    account: str | None,
    deny: bool,
) -> None:
    store, engine = make_store()
    policy = RecordingPolicy(deny_writes=deny)
    monkeypatch.setattr(store, "access_policy", policy)
    engine.conn.fetchval.return_value = account
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store),
            ),
            "state": {},
        },
    )
    identity, target_id = make_test_identity(), uuid4()
    if deny and account is not None:
        with pytest.raises(AccessDeniedError, match="write grant"):
            await session_access.require_row_write(request, identity, target_id)
        assert executed_sql(engine.conn) == ["BEGIN", "ROLLBACK"]
    else:
        await session_access.require_row_write(request, identity, target_id)
        assert executed_sql(engine.conn) == ["BEGIN", "COMMIT"]
    engine.conn.fetchval.assert_awaited_once_with(
        "SELECT account FROM inquiries WHERE id = $1",
        target_id,
    )
    assert policy.of("check") == (
        []
        if account is None
        else [
            (
                Principal(
                    email=identity.email,
                    role=identity.role,
                    api_key_id=identity.api_key_id,
                    groups=policy.groups_of,
                ),
                Resource(kind="inquiry", id=str(target_id), account=account),
                "write",
            ),
        ]
    )
    if account is not None:
        assert policy.sent == [["BEGIN"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "Issue", "AgentSession"])
async def test_session_row_guard_only_checks_sessions(
    monkeypatch: pytest.MonkeyPatch,
    kind: str | None,
) -> None:
    store, engine = make_store()
    identity, target_id = make_test_identity(), uuid4()
    chat_id = f"chat:{uuid4()}"
    set_field_row(
        engine.conn,
        None
        if kind is None
        else {
            "kind": kind,
            "account": identity.email,
            "cli_session_id": chat_id,
            "opener": identity.api_key_id,
            "audience": "open",
        },
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store),
            ),
        },
    )
    writer = AsyncMock()
    monkeypatch.setattr(chat_sessions, "require_row_writer", writer)
    await session_access.require_session_row_write(request, identity, target_id)
    if kind == "AgentSession":
        writer.assert_awaited_once_with(
            request,
            identity,
            session_id=target_id,
            cli_session_id=chat_id,
            opener=identity.api_key_id,
        )
    else:
        writer.assert_not_awaited()
    assert engine.conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_session_row_guard_refuses_hidden_audience_before_chat_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    set_field_row(
        engine.conn,
        {
            "kind": "AgentSession",
            "account": "other@example.com",
            "cli_session_id": "vendor-id",
            "opener": uuid4(),
            "audience": "staff",
        },
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store),
            ),
        },
    )
    writer = AsyncMock()
    monkeypatch.setattr(chat_sessions, "require_row_writer", writer)
    with pytest.raises(HTTPException) as refused:
        await session_access.require_session_row_write(
            request,
            make_test_identity(),
            uuid4(),
        )
    assert refused.value.status_code == 403
    assert refused.value.detail == {"code": "session_audience", "audience": "staff"}
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_row_purge_does_not_require_ownership() -> None:
    store, engine = make_store()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(store=store),
            ),
        },
    )
    assert (
        await session_access.require_row_purge(
            request,
            make_test_identity(role="viewer"),
            uuid4(),
        )
        is None
    )
    assert engine.conn.fetchrow.await_count == 1
    assert executed_sql(engine.conn) == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
