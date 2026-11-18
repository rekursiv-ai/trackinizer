"""The exec routes' answers to the hub's refusals, over a stand-in hub and no database.

The routes end to end, with the real sign-in and machine credentials, are in
``exec_routes_pglite_test.py``. These tests pin what each hub error becomes, and which
routes the schema lists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI
from starlette.testclient import TestClient
from treekle import from_plain

import pytest

from trackinizer.server.api import exec_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.auth import current_user
from trackinizer.server.execs import (
    Exec,
    ExecBusyError,
    ExecHub,
    ExecLimitError,
    ExecLostError,
    ExecRevokedError,
    ExecStaleError,
    Submission,
    always_current,
)
from trackinizer.wire.wire_exec import (
    EXEC_PATH,
    MACHINE_EXEC_RESULT_PATH,
    MACHINE_EXEC_WAIT_PATH,
    ExecResult,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping


_MACHINE = UUID(int=1)
_INSTANCE = UUID(int=2)
_BODY = {"command": "ls", "cwd": "~", "timeout_sec": 5}
_IDENTITY = make_test_identity()


class _Hub(ExecHub):
    """A hub whose every call ends in the error the test chose."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    @override
    async def run(self, submission: Submission) -> ExecResult:
        del submission
        raise self._error

    @override
    async def wait(
        self,
        machine_id: UUID,
        *,
        wait_sec: float,
        is_current: Callable[[], Awaitable[bool]] = always_current,
    ) -> Exec | None:
        del machine_id, wait_sec, is_current
        raise self._error


class _Holding(ExecHub):
    """A hub that notes how long each wait was asked to hold, and has no command."""

    def __init__(self) -> None:
        super().__init__()
        self.held: list[float] = []

    @override
    async def wait(
        self,
        machine_id: UUID,
        *,
        wait_sec: float,
        is_current: Callable[[], Awaitable[bool]] = always_current,
    ) -> Exec | None:
        del machine_id, is_current
        self.held.append(wait_sec)


def _app(monkeypatch: pytest.MonkeyPatch, error: Exception) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ChangeIdMiddleware)
    app.include_router(exec_routes.router)
    app.dependency_overrides[current_user] = lambda: _IDENTITY
    app.state.execs = _Hub(error)
    monkeypatch.setattr(exec_routes, "_exec_owner", AsyncMock(return_value="ada@x.y"))
    monkeypatch.setattr(exec_routes, "_exec_machine", AsyncMock(return_value=_MACHINE))
    monkeypatch.setattr(exec_routes, "current_machine", AsyncMock())
    monkeypatch.setattr(exec_routes, "_require_instance", AsyncMock())
    return app


def _detail(response_json: object) -> Mapping[str, str]:
    return from_plain(response_json, dict[str, dict[str, str]])["detail"]


@pytest.mark.parametrize(
    ("error", "status", "code", "said"),
    [
        (ExecBusyError(), 409, "exec_busy", "gx10"),
        (ExecLimitError(), 429, "exec_limit", "wait a minute"),
        (ExecRevokedError(), 410, "exec_machine_revoked", "gx10 was revoked"),
        (ExecLostError(taken=False), 504, "exec_lost", "did not take"),
        (ExecLostError(taken=True), 504, "exec_lost", "did not report"),
    ],
)
def test_each_refusal_of_the_hub_is_one_coded_answer(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
    said: str,
) -> None:
    with TestClient(_app(monkeypatch, error)) as client:
        response = client.post(
            EXEC_PATH.format(name="gx10"),
            params={"asker": "ada@x.y"},
            json=_BODY,
        )

    assert response.status_code == status
    assert _detail(response.json())["code"] == code
    assert said in _detail(response.json())["message"]


def test_a_host_whose_machine_is_revoked_while_it_waits_gets_the_revoked_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(_app(monkeypatch, ExecRevokedError())) as client:
        response = client.get(
            MACHINE_EXEC_WAIT_PATH.format(machine_id=_MACHINE),
            params={"instance": str(_INSTANCE), "wait_sec": 1},
        )

    assert (response.status_code, response.json()) == (
        410,
        {"detail": "machine_revoked"},
    )


def test_a_host_the_machine_was_taken_from_while_it_waited_gets_a_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(_app(monkeypatch, ExecStaleError())) as client:
        response = client.get(
            MACHINE_EXEC_WAIT_PATH.format(machine_id=_MACHINE),
            params={"instance": str(_INSTANCE), "wait_sec": 1},
        )

    assert (response.status_code, response.json()) == (
        403,
        {"detail": "stale host instance"},
    )


@pytest.mark.parametrize(("asked", "held"), [(1_000, 25), (3, 3)])
def test_a_host_cannot_hold_its_wait_past_25_seconds(
    monkeypatch: pytest.MonkeyPatch,
    asked: float,
    held: float,
) -> None:
    app = _app(monkeypatch, ExecRevokedError())
    app.state.execs = hub = _Holding()

    with TestClient(app) as client:
        response = client.get(
            MACHINE_EXEC_WAIT_PATH.format(machine_id=_MACHINE),
            params={"instance": str(_INSTANCE), "wait_sec": asked},
        )

    assert (response.status_code, response.json()) == (200, None)
    assert hub.held == [held]


def test_only_the_assistants_route_is_in_the_schema_and_not_the_hosts_legs() -> None:
    app = FastAPI()
    app.include_router(exec_routes.router)

    paths = app.openapi()["paths"]

    assert list(paths) == [EXEC_PATH]
    assert MACHINE_EXEC_WAIT_PATH not in paths
    assert MACHINE_EXEC_RESULT_PATH not in paths


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
