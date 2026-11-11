"""The session relay's routes: declared with their bodies, and unbuilt until the relay is."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypedDict
from uuid import UUID

from fastapi import FastAPI
from treekle import from_plain

import pytest

from trackinizer.server.api import session_relay_routes
from trackinizer.server.api.app import ROUTERS
from trackinizer.wire.audience import SESSION_CONTENT_ROUTES
from trackinizer.wire.wire_session_relay import (
    SESSION_INPUTS_PATH,
    SESSION_LINES_PATH,
    SESSION_PROCESS_PATH,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


class _Components(TypedDict):
    schemas: dict[str, object]


class _Schema(TypedDict):
    components: _Components


_SESSION = UUID(int=1)
_ROUTES: Final[tuple[tuple[str, str, dict[str, object] | None], ...]] = (
    ("GET", SESSION_LINES_PATH, None),
    ("POST", SESSION_INPUTS_PATH, {"kind": "Halt", "payload": {}}),
    ("PUT", SESSION_PROCESS_PATH, {"host": "lab-box"}),
    ("PUT", SESSION_PROCESS_PATH, {}),
    ("PUT", SESSION_PROCESS_PATH, {"host": "lab-box", "login_host": "laptop"}),
    ("GET", SESSION_PROCESS_PATH, None),
    ("DELETE", SESSION_PROCESS_PATH, None),
)


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_each_route_says_it_is_not_built_yet(
    route_client: tuple[TestClient, Store, FakeEngine],
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    client, _, _ = route_client

    response = client.request(method, path.format(session_id=_SESSION), json=body)

    assert response.status_code == 501
    assert response.json() == {
        "detail": {
            "code": "not_built",
            "message": "The session relay is not built yet.",
        },
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", SESSION_INPUTS_PATH, {"kind": "Quit", "payload": {}}),
        ("PUT", SESSION_PROCESS_PATH, {"host": "lab-box", "cwd": "relative"}),
        ("PUT", SESSION_PROCESS_PATH, {"provider": "rai"}),
    ],
    ids=[
        "an input no person may send",
        "a folder that is not absolute",
        "a field this wire does not have",
    ],
)
def test_a_body_the_wire_refuses_is_refused_before_the_route_runs(
    route_client: tuple[TestClient, Store, FakeEngine],
    method: str,
    path: str,
    body: dict[str, object],
) -> None:
    client, _, _ = route_client

    response = client.request(method, path.format(session_id=_SESSION), json=body)

    assert response.status_code == 422


def test_the_app_serves_the_routes_and_registers_them_as_session_content() -> None:
    assert session_relay_routes.router in ROUTERS
    assert {(method, path) for method, path, _ in _ROUTES} <= SESSION_CONTENT_ROUTES


def test_the_schema_names_every_frame_a_viewer_reads() -> None:
    # A fresh app: the module-level one is shared, and caches the schema it builds.
    served = FastAPI()
    served.include_router(session_relay_routes.router)

    schema = from_plain(served.openapi(), _Schema)

    assert {
        "Opened",
        "EventLine",
        "StateLine",
        "SettingsLine",
        "Setting",
        "RecordLine",
        "ProcessLine",
        "AgentState",
        "AgentProcess",
        "NewInput",
        "InputSent",
        "ProcessSpec",
    } <= set(schema["components"]["schemas"])


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
