"""The notes on what this server provides reach every signed-in person."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from treekle import json_loads

from trackinizer.conftest import make_store
from trackinizer.server.api import compute_routes
from trackinizer.server.api.app import ROUTERS
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.config import ComputeNote, Config


def test_a_signed_in_viewer_reads_the_notes_in_order() -> None:
    notes = (
        ComputeNote(name="GPU jobs", about="Campaigns run on a shared GPU."),
        ComputeNote(name="Shell", about="Each chat's own box."),
    )
    client = _client(Config(compute=notes), signed_in=True)

    response = client.get("/api/compute")

    assert response.status_code == 200
    assert json_loads(response.content, dict[str, object]) == {
        "notes": [
            {"name": "GPU jobs", "about": "Campaigns run on a shared GPU."},
            {"name": "Shell", "about": "Each chat's own box."},
        ],
    }


def test_a_server_with_no_notes_or_no_config_answers_none() -> None:
    for config in (Config(), None):
        client = _client(config, signed_in=True)

        assert client.get("/api/compute").json() == {"notes": []}


def test_an_anonymous_caller_is_refused() -> None:
    client = _client(
        Config(compute=(ComputeNote(name="a", about="b"),)),
        signed_in=False,
    )

    response = client.get("/api/compute")

    assert response.status_code == 401
    assert "notes" not in response.text


def test_the_app_mounts_the_route() -> None:
    assert compute_routes.router in ROUTERS


def _client(config: Config | None, *, signed_in: bool) -> TestClient:
    app = FastAPI()
    app.include_router(compute_routes.router)
    app.state.store, _ = make_store()
    if config is not None:
        app.state.config = config
    if signed_in:
        app.dependency_overrides[current_user] = _viewer
    return TestClient(app)


async def _viewer() -> AuthIdentity:
    return make_test_identity(role="viewer")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
