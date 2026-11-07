"""The exec routes' answers to the hub's refusals, over a stand-in hub and no database.

The routes end to end, with the real sign-in and machine credentials, are in
``exec_routes_pglite_test.py``. These tests pin what each hub error becomes, and which
routes the schema lists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast, override
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient
from treekle import from_plain

import pytest

from trackinizer.conftest import FakeEngine, set_field_row
from trackinizer.server.api import exec_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.api.session_identity import Asker
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
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_exec import (
    EXEC_PATH,
    MACHINE_EXEC_RESULT_PATH,
    MACHINE_EXEC_WAIT_PATH,
    ExecRequest,
    ExecResult,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping


class _Refusal(TypedDict):
    code: str
    message: str


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

    paths = cast(dict[str, object], app.openapi()["paths"])

    assert list(paths) == [EXEC_PATH]
    assert MACHINE_EXEC_WAIT_PATH not in paths
    assert MACHINE_EXEC_RESULT_PATH not in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_exec_owner_refuses_auth_disabled_and_browser_credentials(
    monkeypatch: pytest.MonkeyPatch,
    disabled: bool,
) -> None:
    request, _engine = _unit_exec_request()
    monkeypatch.setattr(exec_routes, "auth_is_disabled", Mock(return_value=disabled))
    resolve = AsyncMock()
    monkeypatch.setattr(exec_routes, "resolve_asker", resolve)
    identity = make_test_identity(api_key_id=UUID(int=3) if disabled else None)
    with pytest.raises(HTTPException) as refused:
        await exec_routes._exec_owner(request, identity, asker="ada@example.com")
    assert refused.value.status_code == 403
    assert from_plain(refused.value.detail, _Refusal)["code"] == "exec_forbidden"
    resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "viewer", "writer", "admin"])
async def test_exec_owner_requires_an_attested_writer_or_admin(
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    request, _engine = _unit_exec_request()
    asker = (
        None
        if role is None
        else Asker(
            email="ada@example.com",
            role="viewer"
            if role == "viewer"
            else "admin"
            if role == "admin"
            else "writer",
            read_audiences=frozenset(),
        )
    )
    resolve = AsyncMock(return_value=asker)
    monkeypatch.setattr(exec_routes, "resolve_asker", resolve)
    if role in {"writer", "admin"}:
        assert (
            await exec_routes._exec_owner(
                request,
                _IDENTITY,
                asker="ada@example.com",
            )
            == "ada@example.com"
        )
    else:
        with pytest.raises(HTTPException) as refused:
            await exec_routes._exec_owner(request, _IDENTITY, asker="ada@example.com")
        assert refused.value.status_code == 403
        assert from_plain(refused.value.detail, _Refusal)["code"] == "exec_forbidden"
    resolve.assert_awaited_once_with(request, _IDENTITY, email="ada@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["missing", "offline", "unconfirmed", "no-exec", "ready"],
)
async def test_exec_machine_checks_availability_before_returning_its_id(
    state: str,
) -> None:
    request, engine = _unit_exec_request()
    set_field_row(
        engine.conn,
        None
        if state == "missing"
        else {
            "id": _MACHINE,
            "online": state != "offline",
            "confirmed": state != "unconfirmed",
            "facts": {"exec": state != "no-exec"},
        },
    )
    if state == "ready":
        assert (
            await exec_routes._exec_machine(
                request,
                owner="ada@example.com",
                name="gx10",
            )
            == _MACHINE
        )
    elif state == "missing":
        with pytest.raises(NotFoundError, match="no machine gx10 of yours"):
            await exec_routes._exec_machine(
                request,
                owner="ada@example.com",
                name="gx10",
            )
    else:
        with pytest.raises(HTTPException) as refused:
            await exec_routes._exec_machine(
                request,
                owner="ada@example.com",
                name="gx10",
            )
        assert refused.value.status_code == 409
        detail = from_plain(refused.value.detail, _Refusal)
        assert detail["code"] == "exec_not_enabled"
        expected = {
            "offline": "offline",
            "unconfirmed": "confirm it first",
            "no-exec": "--no-exec",
        }
        assert expected[state] in detail["message"]
    assert engine.conn.fetchrow.await_args is not None
    assert engine.conn.fetchrow.await_args.args[1:3] == ("gx10", "ada@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [None, False, True])
async def test_exec_instance_requires_a_true_database_answer(
    current: bool | None,
) -> None:
    request, engine = _unit_exec_request()
    engine.conn.fetchval.return_value = current
    assert await exec_routes._is_current(request, _MACHINE, instance=_INSTANCE) is (
        current is True
    )
    if current is True:
        assert (
            await exec_routes._require_instance(request, _MACHINE, instance=_INSTANCE)
            is None
        )
    else:
        with pytest.raises(HTTPException) as refused:
            await exec_routes._require_instance(request, _MACHINE, instance=_INSTANCE)
        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "stale host instance",
        )
    engine.conn.fetchval.assert_awaited_with(
        "SELECT host_instance = $2 FROM machines WHERE id = $1",
        _MACHINE,
        _INSTANCE,
    )


@pytest.mark.asyncio
async def test_exec_route_returns_the_result_of_one_hub_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _engine = _unit_exec_request()
    hub = ExecHub()
    result = _unit_exec_result()
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(hub, "run", run)
    monkeypatch.setattr(exec_routes, "get_exec_hub", Mock(return_value=hub))
    monkeypatch.setattr(
        exec_routes,
        "_exec_owner",
        AsyncMock(return_value="ada@example.com"),
    )
    monkeypatch.setattr(exec_routes, "_exec_machine", AsyncMock(return_value=_MACHINE))
    body = ExecRequest(command="pwd", cwd="~", timeout_sec=1)
    assert await exec_routes.exec_route("gx10", body, request, _IDENTITY) is result
    run.assert_awaited_once_with(
        Submission(
            user="ada@example.com",
            machine_id=_MACHINE,
            machine="gx10",
            request=body,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disabled", "accepted"),
    [(True, False), (False, False), (False, True)],
)
async def test_exec_result_authenticates_host_and_requires_a_waiting_command(
    monkeypatch: pytest.MonkeyPatch,
    disabled: bool,
    accepted: bool,
) -> None:
    request, _engine = _unit_exec_request()
    hub = ExecHub()
    deliver = Mock(return_value=accepted)
    authenticate = AsyncMock()
    monkeypatch.setattr(hub, "deliver", deliver)
    monkeypatch.setattr(exec_routes, "get_exec_hub", Mock(return_value=hub))
    monkeypatch.setattr(exec_routes, "current_machine", authenticate)
    monkeypatch.setattr(exec_routes, "auth_is_disabled", Mock(return_value=disabled))
    result = _unit_exec_result()
    if disabled:
        with pytest.raises(HTTPException) as refused:
            await exec_routes.exec_result_route(_MACHINE, _INSTANCE, result, request)
        assert refused.value.status_code == 403
        authenticate.assert_not_awaited()
        deliver.assert_not_called()
    else:
        if accepted:
            assert (
                await exec_routes.exec_result_route(
                    _MACHINE,
                    _INSTANCE,
                    result,
                    request,
                )
                is None
            )
        else:
            with pytest.raises(NotFoundError, match="no such command waiting"):
                await exec_routes.exec_result_route(
                    _MACHINE,
                    _INSTANCE,
                    result,
                    request,
                )
        authenticate.assert_awaited_once_with(request, _MACHINE)
        deliver.assert_called_once_with(_MACHINE, _INSTANCE, result)


@pytest.mark.asyncio
async def test_exec_wait_refuses_disabled_auth_before_authenticating_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _engine = _unit_exec_request()
    authenticate = AsyncMock()
    monkeypatch.setattr(exec_routes, "auth_is_disabled", Mock(return_value=True))
    monkeypatch.setattr(exec_routes, "current_machine", authenticate)
    with pytest.raises(HTTPException) as refused:
        await exec_routes.exec_wait_route(_MACHINE, request, _INSTANCE, 0)
    assert refused.value.status_code == 403
    assert from_plain(refused.value.detail, _Refusal)["code"] == "exec_forbidden"
    authenticate.assert_not_awaited()


@pytest.mark.asyncio
async def test_next_command_freezes_all_host_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = ExecHub()
    body = ExecRequest(
        command="pwd",
        cwd="~/project",
        timeout_sec=2,
        stdin_b64="eA==",
        env={"TEST": "1"},
    )
    command = Exec(
        id=_INSTANCE,
        user="ada@example.com",
        machine_id=_MACHINE,
        machine="gx10",
        request=body,
    )
    wait = AsyncMock(return_value=command)
    monkeypatch.setattr(hub, "wait", wait)
    result = await exec_routes._next_command(
        hub,
        machine_id=_MACHINE,
        wait_sec=0,
        is_current=always_current,
    )
    assert result is not None
    assert result.exec_id == command.id
    assert result.asker == command.user
    assert result.model_dump(exclude={"exec_id", "asker"}) == body.model_dump()
    wait.assert_awaited_once_with(_MACHINE, wait_sec=0, is_current=always_current)


def _unit_exec_request() -> tuple[Request, FakeEngine]:
    engine = FakeEngine()
    app = FastAPI()
    app.state.engine = engine
    return Request({"type": "http", "app": app}), engine


def _unit_exec_result() -> ExecResult:
    return ExecResult(
        exit_code=0,
        stdout_b64="eA==",
        stderr_b64="",
        cwd="~",
        truncated=False,
        duration_ms=1,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
