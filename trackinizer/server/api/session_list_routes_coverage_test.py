"""Unit coverage for the own-session listing adapter."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Request

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import session_list_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.session_identity import (
    request_principal,
    session_reader,
)
from trackinizer.server.config import Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals.session_list import SessionList


@pytest.mark.asyncio
async def test_list_forwards_scoped_reader_policy_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    inbound = InboundQueue()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    store=store,
                    engine=engine,
                    inbound=inbound,
                    config=Config(),
                ),
            ),
            "state": {},
        },
    )
    identity = make_test_identity()
    principal = await request_principal(request, identity)
    expected = SessionList(sessions=[], issues=[], more=False)
    listing = AsyncMock(return_value=expected)
    monkeypatch.setattr(session_list_routes, "list_own_sessions", listing)
    result = await session_list_routes.list_sessions_route(
        request,
        identity,
        days=2,
        limit=3,
    )
    assert result is expected
    listing.assert_awaited_once_with(
        engine,
        email=identity.email,
        reader=session_reader(request, identity),
        inbound=inbound,
        days=2,
        limit=3,
        policy=store.access_policy,
        principal=principal,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
