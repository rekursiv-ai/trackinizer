"""An addon manifest describes its services and every HTTP route it mounts."""

from __future__ import annotations

from fastapi import APIRouter

import pytest

from trackinizer.addons.addon import AddonManifest


def test_describe_lists_routes_of_included_sub_routers() -> None:
    inner = APIRouter()
    inner.add_api_route("/x", _ping)
    middle = APIRouter()
    middle.add_api_route("/deep", _ping)
    middle.include_router(inner, prefix="/inner")
    top = APIRouter()
    top.add_api_route("/top", _ping)
    top.include_router(middle, prefix="/mid")
    description = AddonManifest(title="T", description="D.", routers=[top]).describe(
        "demo",
    )
    assert description.name == "demo"
    assert description.routes == [
        "/api/addons/demo/mid/deep",
        "/api/addons/demo/mid/inner/x",
        "/api/addons/demo/top",
    ]


def test_describe_names_each_service_with_where_it_runs() -> None:
    manifest = AddonManifest(
        title="T",
        description="D.",
        server_services=[_Service("sweep", "Sweeps.")],
        standalone_services=[_Service("chat", "Chats.")],
    )

    assert [
        (each.name, each.description, each.placement)
        for each in manifest.describe("demo").services
    ] == [
        ("sweep", "Sweeps.", "server"),
        ("chat", "Chats.", "standalone"),
    ]


def test_describe_lists_open_routes_beside_the_viewer_ones() -> None:
    open_router = APIRouter()
    open_router.add_api_route("/hook", _ping)
    viewer_router = APIRouter()
    viewer_router.add_api_route("/ping", _ping)
    manifest = AddonManifest(
        title="T",
        description="D.",
        routers=[viewer_router],
        open_routers=[open_router],
    )
    assert manifest.describe("demo").routes == [
        "/api/addons/demo/hook",
        "/api/addons/demo/ping",
    ]


def test_describe_refuses_a_websocket_route_even_when_nested() -> None:
    inner = APIRouter()
    inner.add_api_websocket_route("/live", _socket)
    top = APIRouter()
    top.include_router(inner, prefix="/sub")
    manifest = AddonManifest(title="T", description="D.", routers=[top])
    with pytest.raises(TypeError, match=r"WebSocket route '/sub/live'"):
        manifest.describe("demo")


def test_describe_refuses_a_websocket_route_in_an_open_router() -> None:
    open_router = APIRouter()
    open_router.add_api_websocket_route("/live", _socket)
    manifest = AddonManifest(title="T", description="D.", open_routers=[open_router])
    with pytest.raises(TypeError, match=r"WebSocket route '/live'"):
        manifest.describe("demo")


class _Service:
    """A service that is only described."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    async def run(self, context: object) -> None:
        del context


async def _ping() -> dict[str, bool]:
    return {"pong": True}


async def _socket() -> None:
    return None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
