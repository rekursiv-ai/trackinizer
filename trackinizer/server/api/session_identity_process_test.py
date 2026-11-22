"""Process keys read launch results only from their account's descendant sessions."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import query, session_relay_routes
from trackinizer.server.api.conftest import live_session
from trackinizer.server.auth import AuthIdentity, ProcessKey, request_user
from trackinizer.server.config import Config
from trackinizer.server.route_iter import iter_route_leaves
from trackinizer.server.session_relay import SessionRelay
from trackinizer.types.inquiries import Issue


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/api/sessions/{session_id}/lines", "/api/inquiries/{target_id}"],
)
@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("child", 200),
        ("case", 200),
        ("own", 403),
        ("cycle", 403),
        ("sibling", 403),
        ("foreign", 403),
        ("issue", 403),
        ("missing", 403),
        ("person", 200),
        ("unbound", 403),
    ],
)
async def test_launch_reads_only_descendant_sessions(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    case: str,
    status: int,
) -> None:
    store, engine = make_store()
    served = FastAPI()
    served.include_router(query.router)
    served.include_router(session_relay_routes.router)
    served.state.store, served.state.engine = store, engine
    served.state.config = Config()
    served.state.session_relay = SessionRelay()
    own = UUID(int=2)
    target = own if case in {"own", "cycle"} else UUID(int=3)
    session = replace(
        live_session(),
        id=target,
        account="bob@x.y"
        if case == "foreign"
        else "ADA@X.Y"
        if case == "case"
        else "ada@x.y",
    )
    row = (
        None
        if case == "missing"
        else Issue(id=target, account="ada@x.y")
        if case == "issue"
        else session
    )
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=row))
    engine.conn.fetchval.return_value = case != "sibling"
    engine.conn.fetchrow = AsyncMock(
        return_value=None
        if case in {"issue", "missing"}
        else {"account": session.account},
    )
    key_id = UUID(int=4)
    identity = AuthIdentity(
        user_id=UUID(int=5),
        api_key_id=key_id,
        email="ada@x.y",
        role="writer",
        process=None
        if case == "person"
        else ProcessKey(
            process_id=UUID(int=6),
            session_id=None if case == "unbound" else own,
            host_id=UUID(int=7),
        ),
    )
    store.remember_bearer_identity("trax_process", identity)
    store.should_bump_api_key_last_used(key_id)
    route = dict(iter_route_leaves(served))[path]
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "app": served,
            "route": route,
            "headers": [(b"authorization", b"Bearer trax_process")],
            "path_params": {
                "session_id" if "sessions" in path else "target_id": str(target),
            },
        },
    )
    actual = 200
    try:
        caller = await request_user(request)
        if "sessions" in path:
            await session_relay_routes.session_lines_route(target, request, caller)
        else:
            result = await query.get_inquiry_route(target, request, caller)
            assert result["id"] == str(target)
    except HTTPException as error:
        actual = error.status_code
    assert actual == status


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
