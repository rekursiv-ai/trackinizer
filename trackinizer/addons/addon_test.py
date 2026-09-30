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


def test_describe_refuses_a_websocket_route_even_when_nested() -> None:
    inner = APIRouter()
    inner.add_api_websocket_route("/live", _socket)
    top = APIRouter()
    top.include_router(inner, prefix="/sub")
    manifest = AddonManifest(title="T", description="D.", routers=[top])
    with pytest.raises(TypeError, match=r"WebSocket route '/sub/live'"):
        manifest.describe("demo")


async def _ping() -> dict[str, bool]:
    return {"pong": True}


async def _socket() -> None:
    return None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
