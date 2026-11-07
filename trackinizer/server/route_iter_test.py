"""Tests for flattening a FastAPI app's lazily included route tree."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, WebSocket
from starlette.routing import Host, Mount

from trackinizer.server.route_iter import (
    iter_routes,
    registered_path_methods,
    registered_paths,
)


async def _ok() -> dict[str, str]:
    return {}


async def _socket(websocket: WebSocket) -> None:
    await websocket.close()


def _app() -> FastAPI:
    """Build an app with routes at the top, one include deep, and two deep."""
    inner = APIRouter()
    _ = inner.get("/leaf")(_ok)
    outer = APIRouter()
    _ = outer.post("/items")(_ok)
    _ = outer.put("/items")(_ok)
    outer.include_router(inner, prefix="/inner")
    app = FastAPI()
    _ = app.get("/health")(_ok)
    app.add_api_websocket_route("/ws", _socket)
    app.router.routes.append(Mount("/static", routes=[]))
    app.include_router(outer, prefix="/api")
    app.include_router(outer, prefix="/api")
    return app


def test_every_leaf_carries_its_full_prefix_and_verbs() -> None:
    routes = list(iter_routes(_app()))

    assert ("/health", frozenset({"GET"})) in routes
    assert ("/ws", frozenset()) in routes
    assert ("/static", frozenset()) in routes
    assert ("/api/inner/leaf", frozenset({"GET"})) in routes
    assert ("/api/items", frozenset({"POST"})) in routes


def test_a_host_route_has_no_path_and_is_skipped() -> None:
    app = FastAPI()
    app.router.routes.append(Host("docs.example.com", app=FastAPI()))

    assert [path for path, _ in iter_routes(app)] == [
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    ]


def test_a_router_included_twice_yields_its_routes_twice() -> None:
    routes = list(iter_routes(_app()))

    assert routes.count(("/api/inner/leaf", frozenset({"GET"}))) == 2


def test_registered_paths_are_the_distinct_templates() -> None:
    paths = registered_paths(_app())

    assert {"/health", "/ws", "/static", "/api/items", "/api/inner/leaf"} <= paths
    assert all(path.startswith("/") for path in paths)


def test_registered_path_methods_pair_each_path_with_each_verb() -> None:
    pairs = registered_path_methods(_app())

    assert {("/api/items", "POST"), ("/api/items", "PUT")} <= pairs
    assert not any(path == "/ws" for path, _ in pairs)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
