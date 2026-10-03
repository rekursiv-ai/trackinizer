"""The server lists, mounts, and runs its deployment's addons."""

from __future__ import annotations

from dataclasses import field
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import asyncio

from configgle import Fig, Makes
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from trackinizer.addons.addon import AddonManifest
from trackinizer.addons.deployment import Deployment
from trackinizer.conftest import make_store
from trackinizer.lib.custom_json import DictCodec, loads
from trackinizer.server.api import addons_routes, app
from trackinizer.server.api.addons_routes import attach_deployment, deployment_of
from trackinizer.server.api.app import _build_app, lifespan
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.config import Config
from trackinizer.server.visuals.catalog import Workspace


if TYPE_CHECKING:
    import pytest

    from trackinizer.addons.addon import ServerContext
    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


class Probe:
    """A test addon: a route, and a server service that reports its context."""

    class Config(Fig["Probe"]):
        pass

    # Spelled ``Probe.Config``: this module also imports the server's ``Config``.
    def __init__(self, config: Probe.Config) -> None:
        del config
        self.service = _ProbeService()

    def manifest(self) -> AddonManifest:
        router = APIRouter()
        router.add_api_route("/ping", _ping)
        return AddonManifest(
            title="Probe",
            description="Reports what the server gave it.",
            server_services=[self.service],
            routers=[router],
        )


class ProbeSite(Makes["Deployment"], Deployment.Config):
    """A deployment running the probe addon."""

    probe: Probe.Config = field(default_factory=Probe.Config)
    """The probe addon."""


def test_the_catalog_is_empty_without_a_deployment(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, _ = route_client
    response = client.get("/api/addons")
    assert response.status_code == 200
    assert DictCodec.coerce(loads(response.content)) == {"addons": []}


def test_the_module_app_always_has_a_visual_catalog() -> None:
    assert _visual_catalog(app.app).catalog().visuals


def test_mounted_routes_are_prefixed_listed_and_need_a_viewer() -> None:
    app = _app_with(_probe_deployment())
    app.state.store, _ = make_store()
    client = TestClient(app)
    assert client.get("/api/addons/probe/ping").status_code == 401
    app.dependency_overrides[current_user] = _viewer
    assert client.get("/api/addons/probe/ping").json() == {"pong": True}
    assert client.get("/ping").status_code == 404
    listed = DictCodec.coerce(loads(client.get("/api/addons").content))
    assert listed["addons"] == [
        {
            "name": "probe",
            "title": "Probe",
            "description": "Reports what the server gave it.",
            "services": [
                {
                    "name": "watch",
                    "description": "Records its context.",
                    "placement": "server",
                },
            ],
            "routes": ["/api/addons/probe/ping"],
        },
    ]


def test_attaching_sets_the_deployments_visual_catalog() -> None:
    cfg = Deployment.Config()
    cfg.visuals.default_visual = "trax.chat"
    deployment = cfg.make()
    app = _app_with(deployment)
    assert _visual_catalog(app) is deployment.visuals
    assert _visual_catalog(app).default_visual == "trax.chat"


def test_a_real_deployment_replaces_the_default_one_of_a_built_app() -> None:
    built = _build_app()
    assert deployment_of(built).manifests == {}
    cfg = ProbeSite()
    cfg.visuals.default_visual = "trax.chat"
    real = cfg.make()
    attach_deployment(built, deployment=real)
    assert deployment_of(built) is real
    assert _visual_catalog(built) is real.visuals
    assert _visual_catalog(built).default_visual == "trax.chat"
    built.state.store, _ = make_store()
    built.dependency_overrides[current_user] = _viewer
    assert TestClient(built).get("/api/addons/probe/ping").status_code == 200


def test_attaching_twice_keeps_the_first_deployment_and_its_routes() -> None:
    first = _probe_deployment()
    app = _app_with(first)
    routes = list(app.routes)
    attach_deployment(app, deployment=_probe_deployment())
    assert app.routes == routes
    assert deployment_of(app) is first
    app.state.store, _ = make_store()
    app.dependency_overrides[current_user] = _viewer
    assert TestClient(app).get("/api/addons/probe/ping").status_code == 200


def test_the_lifespan_runs_server_services_with_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    monkeypatch.setattr(app, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(app, "Store", Mock(return_value=store))
    monkeypatch.setattr(store, "bootstrap", AsyncMock(return_value=None))
    fastapi_app = _app_with(_probe_deployment())
    fastapi_app.state.config = Config()
    service = _running_service(fastapi_app)
    asyncio.run(_serve_until_probed(fastapi_app, service=service))
    assert service.stores == [store]
    assert service.cancelled


class _ProbeService:
    """Records the store it ran with, then waits to be cancelled."""

    name = "watch"
    description = "Records its context."

    def __init__(self) -> None:
        self.stores: list[object] = []
        self.cancelled = False
        self.ran = asyncio.Event()

    async def run(self, context: ServerContext) -> None:
        self.stores.append(context.store)
        self.ran.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _running_service(app: FastAPI) -> _ProbeService:
    """Return the probe's one server service, reached through the deployment."""
    service = deployment_of(app).manifests["probe"].server_services[0]
    assert isinstance(service, _ProbeService)
    return service


def _visual_catalog(app: FastAPI) -> Workspace:
    """Return the visual catalog the visual routes would read from ``app.state``."""
    catalog: object = getattr(app.state, "visual_catalog", None)
    assert isinstance(catalog, Workspace)
    return catalog


def _probe_deployment() -> Deployment:
    return ProbeSite().make()


def _app_with(deployment: Deployment) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(addons_routes.router)
    attach_deployment(app, deployment=deployment)
    return app


async def _serve_until_probed(app: FastAPI, service: _ProbeService) -> None:
    async with lifespan(app):
        await asyncio.wait_for(service.ran.wait(), timeout=5)


async def _viewer() -> AuthIdentity:
    return make_test_identity(role="viewer")


async def _ping() -> dict[str, bool]:
    return {"pong": True}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
