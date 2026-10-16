"""Browser route adapters for workstreams."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import uuid

from fastapi import HTTPException
from fastapi.routing import APIRoute

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server.api import workstream_routes
from trackinizer.server.api.app import ROUTERS
from trackinizer.server.api.session_access import Asker
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.workstreams import (
    CreateWorkstream,
    ScratchWorkstreamError,
    UpdateWorkstream,
    WorkstreamOrder,
    WorkstreamSummary,
)


if TYPE_CHECKING:
    from starlette.requests import Request

    from trackinizer.lib.postgres import DatabaseEngine, PGliteEngine


_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ASKER = uuid.UUID("33333333-3333-3333-3333-333333333333")
_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_IDENTITY = AuthIdentity(
    user_id=_USER,
    api_key_id=None,
    email="user@example.com",
    role="writer",
)
_SUMMARY = WorkstreamSummary(
    id=_ID,
    seq=1,
    title="Research",
    scratch=False,
    hidden=False,
    pinned=False,
)


async def _open_scratch_fake(
    engine: DatabaseEngine,
    *,
    store: object,
    user_id: uuid.UUID,
    email: str,
    events: list[object],
    event: object,
    result: uuid.UUID,
) -> uuid.UUID:
    del engine, store, user_id, email
    events.append(event)
    return result


async def _list_workstreams_fake(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    q: str | None = None,
    include_hidden: bool = False,
    events: list[object],
    event: object,
    result: list[WorkstreamSummary],
) -> list[WorkstreamSummary]:
    del engine, user_id, email, q, include_hidden
    events.append(event)
    return result


async def _resolve_asker_fake(
    request: Request,
    *,
    identity: AuthIdentity,
    email: str | None,
    expected_email: str,
    result: Asker,
) -> Asker:
    del request, identity
    assert email == expected_email
    return result


async def _create_workstream_fake(
    engine: DatabaseEngine,
    *,
    store: object,
    user_id: uuid.UUID,
    email: str,
    title: str,
    expected_title: str,
    result: WorkstreamSummary,
) -> WorkstreamSummary:
    del engine, store, user_id, email
    assert title == expected_title
    return result


async def _missing_order_fake(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    ids: list[uuid.UUID],
) -> None:
    del engine, user_id, email, ids


async def _scratch_update_fake(
    engine: DatabaseEngine,
    *,
    store: object,
    user_id: uuid.UUID,
    email: str,
    workstream_id: uuid.UUID,
    title: str | None = None,
    hidden: bool | None = None,
    pinned: bool | None = None,
) -> WorkstreamSummary | None:
    del engine, store, user_id, email, workstream_id, title, hidden, pinned
    raise ScratchWorkstreamError


async def _missing_info_fake(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    workstream_id: uuid.UUID,
) -> None:
    del engine, user_id, email, workstream_id


def _request() -> Request:
    """Return a route request stand-in."""
    return cast("Request", MagicMock())


def _route_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine: DatabaseEngine,
    store: Store,
) -> None:
    """Replace authentication and app-state reads for direct route calls."""

    def require_browser(identity: AuthIdentity) -> None:
        del identity

    def engine_of(request: Request) -> DatabaseEngine:
        del request
        return engine

    def get_store(request: Request) -> Store:
        del request
        return store

    monkeypatch.setattr(workstream_routes, "require_browser", require_browser)
    monkeypatch.setattr(workstream_routes, "engine_of", engine_of)
    monkeypatch.setattr(workstream_routes, "get_store", get_store)


def test_workstream_order_route_precedes_dynamic_id_routes() -> None:
    routes = [
        route
        for route in workstream_routes.router.routes
        if isinstance(route, APIRoute)
    ]
    paths = [route.path for route in routes]
    assert paths.index("/api/workstreams/order") < paths.index(
        "/api/workstreams/{workstream_id}",
    )
    assert workstream_routes.router in ROUTERS


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_opens_scratch_before_returning_the_bar(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    store = Store(pglite_engine, embed=StubEmbedder())
    _route_dependencies(monkeypatch, engine=pglite_engine, store=store)
    order: list[object] = []

    monkeypatch.setattr(
        workstream_routes,
        "open_scratch",
        partial(
            _open_scratch_fake,
            events=order,
            event="scratch",
            result=_ID,
        ),
    )
    monkeypatch.setattr(
        workstream_routes,
        "list_workstreams",
        partial(
            _list_workstreams_fake,
            events=order,
            event="list",
            result=[_SUMMARY],
        ),
    )
    result = await workstream_routes.list_workstreams_route(
        request=_request(),
        identity=_IDENTITY,
        q="Res",
        hidden=True,
    )
    assert result == [_SUMMARY]
    assert order == ["scratch", "list"]


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_workstream_list_resolves_the_asker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    engine.conn.fetchval.return_value = _ASKER
    store = Store(cast("DatabaseEngine", engine), embed=StubEmbedder())
    _route_dependencies(monkeypatch, engine=cast("DatabaseEngine", engine), store=store)

    asker = Asker(
        email="user@example.com",
        role="writer",
        read_audiences=frozenset(),
    )

    monkeypatch.setattr(
        workstream_routes,
        "resolve_asker",
        partial(
            _resolve_asker_fake,
            expected_email="user@example.com",
            result=asker,
        ),
    )
    called: list[object] = []
    monkeypatch.setattr(
        workstream_routes,
        "open_scratch",
        partial(
            _open_scratch_fake,
            events=called,
            event=("scratch", _ASKER, "user@example.com"),
            result=_ID,
        ),
    )
    monkeypatch.setattr(
        workstream_routes,
        "list_workstreams",
        partial(
            _list_workstreams_fake,
            events=called,
            event=("list", _ASKER, "user@example.com", None, False),
            result=[_SUMMARY],
        ),
    )
    _ = await workstream_routes.list_workstreams_route(
        request=_request(),
        identity=_IDENTITY,
        asker="user@example.com",
    )

    assert called == [
        ("scratch", _ASKER, "user@example.com"),
        ("list", _ASKER, "user@example.com", None, False),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_create_route_returns_the_created_issue_summary(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    store = Store(pglite_engine, embed=StubEmbedder())
    _route_dependencies(monkeypatch, engine=pglite_engine, store=store)

    monkeypatch.setattr(
        workstream_routes,
        "create_workstream",
        partial(
            _create_workstream_fake,
            expected_title="Research",
            result=_SUMMARY,
        ),
    )
    result = await workstream_routes.create_workstream_route(
        CreateWorkstream(title="Research"),
        request=_request(),
        identity=_IDENTITY,
    )
    assert result == _SUMMARY


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_order_route_returns_not_found_for_another_users_id(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    store = Store(pglite_engine, embed=StubEmbedder())
    _route_dependencies(monkeypatch, engine=pglite_engine, store=store)

    monkeypatch.setattr(
        workstream_routes,
        "set_workstream_order",
        _missing_order_fake,
    )
    with pytest.raises(HTTPException) as error:
        await workstream_routes.set_workstream_order_route(
            WorkstreamOrder(ids=[_ID]),
            request=_request(),
            identity=_IDENTITY,
        )
    assert error.value.status_code == 404


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_patch_route_maps_scratch_hide_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    store = Store(pglite_engine, embed=StubEmbedder())
    _route_dependencies(monkeypatch, engine=pglite_engine, store=store)

    monkeypatch.setattr(workstream_routes, "update_workstream", _scratch_update_fake)
    with pytest.raises(HTTPException) as error:
        await workstream_routes.update_workstream_route(
            _ID,
            UpdateWorkstream(hidden=True),
            request=_request(),
            identity=_IDENTITY,
        )
    assert error.value.status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_info_route_returns_not_found_for_an_unowned_issue(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    store = Store(pglite_engine, embed=StubEmbedder())
    _route_dependencies(monkeypatch, engine=pglite_engine, store=store)

    monkeypatch.setattr(workstream_routes, "workstream_info", _missing_info_fake)
    with pytest.raises(HTTPException) as error:
        await workstream_routes.workstream_info_route(
            _ID,
            request=_request(),
            identity=_IDENTITY,
        )
    assert error.value.status_code == 404


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
