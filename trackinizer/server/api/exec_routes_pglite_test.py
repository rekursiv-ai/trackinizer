"""The exec routes end to end over PGlite: who may run a command, and a host's round trip.

The routes are driven through the whole app with the real sign-in and machine
credentials. The host is played by the test: it holds the wait route, answers a command
and posts the result, as the machine host does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncio
import base64
import uuid

from fastapi import Response
from treekle import from_plain

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import clear_identity_override, patch_state
from trackinizer.server.auth import create_api_key
from trackinizer.server.config import Assistant, Config
from trackinizer.server.execs import ExecHub
from trackinizer.server.session import SESSION_COOKIE_NAME, set_session_cookie
from trackinizer.wire import wire_exec, wire_terminals
from trackinizer.wire.wire_exec import (
    EXEC_PATH,
    MACHINE_EXEC_RESULT_PATH,
    MACHINE_EXEC_WAIT_PATH,
    ExecResult,
    ExecWait,
)
from trackinizer.wire.wire_machine_host import ENROLL_PATH, JOIN_PATH, REVOKE_PATH


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_SESSION_KEY = "session-signing-key"
_ORIGIN = "https://trax.test"
_HOST = "trax.test"
_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ASSISTANT = "kb@x.y"


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Machine:
    name: str
    id: uuid.UUID
    credential: str

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}


class _World:
    """A served app with users, the assistant, and a fresh exec hub."""

    def __init__(self, client: httpx2.AsyncClient, store: Store) -> None:
        self.client = client
        self.store = store

    async def person(
        self,
        email: str,
        *,
        role: str = "writer",
        status: str = "active",
    ) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'n', $3, $4)",
                user_id,
                email,
                role,
                status,
            )
        return user_id

    def cookie(self, user_id: uuid.UUID) -> str:
        signed = Response()
        set_session_cookie(
            signed,
            user_id=str(user_id),
            secret=_SESSION_KEY,
            max_age_seconds=600,
        )
        value = signed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        return f"{SESSION_COOKIE_NAME}={value}"

    async def key(self, user_id: uuid.UUID) -> dict[str, str]:
        async with self.store.engine.acquire() as conn:
            _, secret, _, _ = await create_api_key(
                conn,
                user_id=user_id,
                name="k",
                ceiling="writer",
            )
        return {"Authorization": f"Bearer {secret}"}

    async def assistant(self) -> dict[str, str]:
        return await self.key(await self.person(_ASSISTANT))

    def browser(self, cookie: str) -> dict[str, str]:
        return {"Cookie": cookie, "Host": _HOST, "Origin": _ORIGIN}

    async def call(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None = None,
    ) -> httpx2.Response:
        return await self.client.request(method, path, json=body, headers=headers)

    async def join(
        self,
        cookie: str,
        name: str = "gx10",
        *,
        confirm: bool = True,
        facts: dict[str, object] | None = None,
    ) -> _Machine:
        """Self-enroll ``name`` with ``cookie``'s user, join it, and confirm it."""
        enrolled = await self.call(
            "POST",
            ENROLL_PATH,
            headers={"Cookie": cookie},
            body={"name": name},
        )
        assert enrolled.status_code == 201
        joined = await self.call(
            "POST",
            JOIN_PATH,
            headers={},
            body={
                "name": name,
                "token": from_plain(enrolled.json(), dict[str, str])["token"],
                "instance": str(_INSTANCE),
                "host_version": "t",
                "facts": {"exec": True} if facts is None else facts,
            },
        )
        assert joined.status_code == 201
        body = from_plain(joined.json(), dict[str, str])
        if confirm:
            confirmed = await self.call(
                "POST",
                wire_terminals.TERMINAL_CONFIRM_PATH.format(name=name),
                headers=self.browser(cookie),
            )
            assert confirmed.status_code == 204, confirmed.text
        return _Machine(
            name=name,
            id=uuid.UUID(body["machine_id"]),
            credential=body["credential"],
        )

    async def run(
        self,
        headers: Mapping[str, str],
        *,
        asker: str | None = "ada@x.y",
        machine: str = "gx10",
        key: uuid.UUID | None = None,
        **fields: object,
    ) -> httpx2.Response:
        """Ask for a command to run as the caller of the exec route would."""
        path = EXEC_PATH.format(name=machine)
        return await self.call(
            "POST",
            path if asker is None else f"{path}?asker={asker}",
            headers=headers
            if key is None
            else {**headers, "Idempotency-Key": str(key)},
            body={"command": "ls", "cwd": "~", "timeout_sec": 5} | fields,
        )

    async def wait(
        self,
        machine: _Machine,
        *,
        instance: uuid.UUID = _INSTANCE,
        wait_sec: float = 0,
        key: uuid.UUID | None = None,
    ) -> httpx2.Response:
        """Hold the wait route as the host does."""
        path = MACHINE_EXEC_WAIT_PATH.format(machine_id=machine.id)
        return await self.call(
            "GET",
            f"{path}?instance={instance}&wait_sec={wait_sec}",
            headers=machine.headers()
            | ({} if key is None else {"Idempotency-Key": str(key)}),
        )

    async def report(
        self,
        machine: _Machine,
        exec_id: uuid.UUID,
        result: ExecResult,
    ) -> httpx2.Response:
        """Post a command's result as the host does."""
        return await self.call(
            "POST",
            MACHINE_EXEC_RESULT_PATH.format(machine_id=machine.id, exec_id=exec_id),
            headers=machine.headers(),
            body=result.model_dump(mode="json"),
        )

    async def serve(
        self,
        machine: _Machine,
        answer: Callable[[ExecWait], ExecResult],
    ) -> ExecWait:
        """Be the host for one command: wait for it, run ``answer`` on it, report."""
        waited = await self.wait(machine, wait_sec=5)
        assert waited.status_code == 200, waited.text
        offer = ExecWait.model_validate(waited.json())
        reported = await self.report(machine, offer.exec_id, answer(offer))
        assert reported.status_code == 204, reported.text
        return offer


@pytest.fixture
def world(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> _World:
    client, store = pglite_route_client
    clear_identity_override()
    patch_state(
        monkeypatch,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email=_ASSISTANT),
        ),
    )
    patch_state(monkeypatch, "execs", ExecHub())
    monkeypatch.delattr(app.state, "replays", raising=False)
    return _World(client, store)


class _Time:
    """A clock the test moves, and a sleep that ends only once it has moved far enough."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.timers: list[tuple[float, asyncio.Event]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        event = asyncio.Event()
        self.timers.append((self.now + seconds, event))
        await event.wait()

    def has_timer(self, seconds: float) -> bool:
        """Say whether something is sleeping until ``seconds`` from now."""
        return any(due == self.now + seconds for due, _ in self.timers)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for due, event in self.timers:
            if due <= self.now:
                event.set()


def _timed_hub(monkeypatch: pytest.MonkeyPatch) -> _Time:
    """Serve with a hub whose every deadline waits for the test to move the clock."""
    time = _Time()
    patch_state(monkeypatch, "execs", ExecHub(clock=time, sleep=time.sleep))
    return time


def _hub() -> ExecHub:
    """Return the hub the app is serving with."""
    hub: object = app.state.execs
    assert isinstance(hub, ExecHub)
    return hub


async def _until(condition: Callable[[], bool]) -> None:
    """Let the app run until ``condition`` holds, failing after ten seconds."""
    for _ in range(2_000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the app did not reach the state the test waits for")


def _result(*, exit_code: int = 0, stdout: bytes = b"") -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout_b64=base64.b64encode(stdout).decode(),
        stderr_b64="",
        cwd="/home/ada",
        truncated=False,
        duration_ms=7,
    )


def _code(response: httpx2.Response) -> str:
    """Return the ``code`` of a refusal's ``detail: {code, message}``."""
    detail = from_plain(response.json(), dict[str, dict[str, str]])["detail"]
    assert set(detail) == {"code", "message"}
    return detail["code"]


def _echo(offer: ExecWait) -> ExecResult:
    return _result(stdout=offer.command.encode())


async def test_the_assistant_runs_a_command_on_its_askers_machine_and_gets_the_result(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()

    call = asyncio.ensure_future(world.run(headers, command="uname -a", timeout_sec=9))
    offer = await world.serve(machine, _echo)
    response = await call

    assert response.status_code == 200, response.text
    assert ExecResult.model_validate(response.json()) == _result(stdout=b"uname -a")
    assert (offer.command, offer.cwd, offer.timeout_sec, offer.asker) == (
        "uname -a",
        "~",
        9,
        "ada@x.y",
    )
    assert (offer.stdin_b64, offer.env) == (None, None)


async def test_stdin_and_the_environment_reach_the_host_as_sent(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    stdin = base64.b64encode(b"data\n").decode()

    call = asyncio.ensure_future(
        world.run(await world.assistant(), stdin_b64=stdin, env={"A_B": "c"}),
    )
    offer = await world.serve(machine, _echo)

    assert (await call).status_code == 200
    assert (offer.stdin_b64, offer.env) == (stdin, {"A_B": "c"})


@pytest.mark.parametrize(
    "caller",
    [
        "assistant_without_asker",
        "assistant_for_a_viewer",
        "assistant_for_nobody_known",
        "assistant_for_a_disabled_user",
        "assistants_browser_session",
        "owners_own_key",
        "owners_key_naming_asker",
        "owners_browser",
        "owners_browser_naming_asker",
        "another_admins_key",
        "service_account",
        "auth_disabled",
    ],
)
async def test_nobody_but_the_assistant_naming_a_writer_or_admin_runs_a_command(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    _ = await world.person("vic@x.y", role="viewer")
    _ = await world.person("dee@x.y", status="disabled")
    admin = await world.person("boss@x.y", role="admin")
    service = await world.person("svc@service.invalid")
    assistant_id = await world.person(_ASSISTANT)
    assistant = await world.key(assistant_id)
    ada_key = await world.key(ada)
    if caller == "auth_disabled":
        patch_state(monkeypatch, "config", Config(auth_disabled=True))

    response = await world.run(
        {
            "assistant_without_asker": assistant,
            "assistant_for_a_viewer": assistant,
            "assistant_for_nobody_known": assistant,
            "assistant_for_a_disabled_user": assistant,
            "assistants_browser_session": world.browser(world.cookie(assistant_id)),
            "owners_own_key": ada_key,
            "owners_key_naming_asker": ada_key,
            "owners_browser": world.browser(world.cookie(ada)),
            "owners_browser_naming_asker": world.browser(world.cookie(ada)),
            "another_admins_key": await world.key(admin),
            "service_account": await world.key(service),
            "auth_disabled": {"Host": "evil.example", "Origin": "http://evil.example"},
        }[caller],
        asker={
            "assistant_without_asker": None,
            "assistant_for_a_viewer": "vic@x.y",
            "assistant_for_nobody_known": "ghost@x.y",
            "assistant_for_a_disabled_user": "dee@x.y",
            "owners_own_key": None,
        }.get(caller, "ada@x.y"),
    )

    assert (response.status_code, _code(response)) == (403, "exec_forbidden")
    if caller != "auth_disabled":
        assert (await world.wait(machine)).json() is None


async def test_the_assistant_cannot_reach_a_machine_that_is_not_its_askers(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    _ = await world.person("bob@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()

    foreign = await world.run(headers, asker="bob@x.y")
    unknown = await world.run(headers, machine="nowhere")

    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert (await world.wait(machine)).json() is None


@pytest.mark.parametrize(
    ("state", "said"),
    [
        ("offline", "offline"),
        ("revoked", "offline"),
        ("unconfirmed", "not confirmed"),
        ("no_exec_fact", "runs no commands"),
        ("exec_off", "runs no commands"),
        ("exec_not_a_flag", "runs no commands"),
    ],
)
async def test_a_machine_that_is_offline_unconfirmed_or_not_opted_in_runs_nothing(
    world: _World,
    state: str,
    said: str,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    facts: dict[str, dict[str, object]] = {
        "no_exec_fact": {"cpus": 4},
        "exec_off": {"exec": False},
        "exec_not_a_flag": {"exec": "true"},
    }
    machine = await world.join(
        cookie,
        confirm=state != "unconfirmed",
        facts=facts.get(state, {"exec": True}),
    )
    if state == "offline":
        async with world.store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE machines SET last_heartbeat = now() - interval '1 hour'",
            )
    if state == "revoked":
        revoked = await world.call(
            "POST",
            REVOKE_PATH.format(name="gx10"),
            headers=world.browser(cookie),
        )
        assert revoked.status_code == 204

    response = await world.run(await world.assistant())

    assert (response.status_code, _code(response)) == (409, "exec_not_enabled")
    assert (
        said
        in from_plain(response.json(), dict[str, dict[str, str]])["detail"]["message"]
    )
    waited = await world.wait(machine)
    assert waited.status_code != 200 or waited.json() is None


async def test_a_body_outside_the_limits_is_refused_before_anything_is_queued(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))

    response = await world.run(await world.assistant(), timeout_sec=0)

    assert response.status_code == 422
    assert (await world.wait(machine)).json() is None


async def test_a_retry_under_the_same_key_gets_the_first_result_and_runs_nothing_twice(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()
    key = uuid.uuid4()

    first = asyncio.ensure_future(world.run(headers, key=key, command="date"))
    _ = await world.serve(machine, _echo)
    answered = await first
    retried = await world.run(headers, key=key, command="date")
    other = await world.run(headers, key=key, command="uptime")

    assert (answered.status_code, retried.status_code) == (200, 200)
    assert retried.json() == answered.json()
    assert other.status_code == 409
    assert (await world.wait(machine)).json() is None


async def test_a_retry_while_the_first_try_still_waits_joins_it(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()
    key = uuid.uuid4()

    first = asyncio.ensure_future(world.run(headers, key=key))
    second = asyncio.ensure_future(world.run(headers, key=key))
    _ = await world.serve(machine, _echo)

    assert [(await each).status_code for each in (first, second)] == [200, 200]
    assert (await world.wait(machine)).json() is None


async def test_a_fifth_command_waiting_on_one_machine_is_refused(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()
    calls = [
        asyncio.ensure_future(world.run(headers))
        for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)
    ]
    await _until(lambda: len(_hub()._execs) == len(calls))

    refused = await world.run(headers)
    for _ in calls:
        _ = await world.serve(machine, _echo)

    assert (refused.status_code, _code(refused)) == (409, "exec_busy")
    assert [(await each).status_code for each in calls] == [200] * len(calls)


async def test_revoking_the_machine_ends_a_command_in_flight_and_the_hosts_wait(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    call = asyncio.ensure_future(world.run(await world.assistant()))
    handed = ExecWait.model_validate((await world.wait(machine, wait_sec=5)).json())
    held = asyncio.ensure_future(world.wait(machine, wait_sec=5))
    await _until(lambda: machine.id in _hub()._waiters)

    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="gx10"),
        headers=world.browser(cookie),
    )
    ended = await call
    waited = await held
    late = await world.report(machine, handed.exec_id, _result())

    assert revoked.status_code == 204
    assert (ended.status_code, _code(ended)) == (410, "exec_machine_revoked")
    assert (waited.status_code, late.status_code) == (410, 410)


async def test_a_host_that_joins_again_ends_the_commands_its_old_self_was_handed(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    call = asyncio.ensure_future(world.run(await world.assistant()))
    _ = await world.wait(machine, wait_sec=5)

    _ = await world.join(cookie)
    ended = await call

    assert (ended.status_code, _code(ended)) == (410, "exec_machine_revoked")


async def test_the_host_legs_take_only_the_machines_own_credential_and_instance(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    laptop = await world.join(cookie, name="laptop")
    call = asyncio.ensure_future(world.run(await world.assistant()))
    offer = ExecWait.model_validate((await world.wait(machine, wait_sec=5)).json())

    stale = await world.wait(machine, instance=uuid.uuid4())
    as_person = await world.call(
        "GET",
        f"{MACHINE_EXEC_WAIT_PATH.format(machine_id=machine.id)}?instance={_INSTANCE}",
        headers={"Cookie": cookie},
    )
    unknown = await world.report(machine, uuid.uuid4(), _result())
    others = await world.report(laptop, offer.exec_id, _result())
    answered = await world.report(machine, offer.exec_id, _result())
    twice = await world.report(machine, offer.exec_id, _result())

    assert stale.status_code == 403
    assert as_person.status_code == 401
    assert (unknown.status_code, others.status_code) == (404, 404)
    assert (answered.status_code, twice.status_code) == (204, 204)
    assert (await call).status_code == 200


async def test_a_wait_retried_after_a_lost_response_gets_the_same_command(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    call = asyncio.ensure_future(world.run(await world.assistant()))
    key = uuid.uuid4()

    first = await world.wait(machine, wait_sec=5, key=key)
    again = await world.wait(machine, wait_sec=0, key=key)
    elsewhere = await world.wait(machine, wait_sec=0)

    assert first.status_code == 200
    assert again.json() == first.json()
    assert elsewhere.json() is None
    offer = ExecWait.model_validate(first.json())
    _ = await world.report(machine, offer.exec_id, _result())
    assert (await call).status_code == 200


async def test_the_host_legs_refuse_a_server_without_auth(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    patch_state(monkeypatch, "config", Config(auth_disabled=True))

    waited = await world.wait(machine)
    reported = await world.report(machine, uuid.uuid4(), _result())

    assert (waited.status_code, reported.status_code) == (403, 403)


async def test_a_command_no_host_takes_is_a_504_and_a_retry_under_its_key_runs_it(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    time = _timed_hub(monkeypatch)
    machine = await world.join(world.cookie(await world.person("ada@x.y")))
    headers = await world.assistant()
    key = uuid.uuid4()
    first = asyncio.ensure_future(world.run(headers, key=key))
    await _until(lambda: time.has_timer(wire_exec.OFFER_TTL_SEC))

    time.advance(wire_exec.OFFER_TTL_SEC)
    lost = await first
    retried = asyncio.ensure_future(world.run(headers, key=key))
    _ = await world.serve(machine, _echo)

    assert (lost.status_code, _code(lost)) == (504, "exec_lost")
    assert (await retried).status_code == 200


async def test_a_command_lost_after_a_host_took_it_is_not_run_again_by_a_retry(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    time = _timed_hub(monkeypatch)
    machine = await world.join(world.cookie(await world.person("ada@x.y")))
    headers = await world.assistant()
    key = uuid.uuid4()
    first = asyncio.ensure_future(world.run(headers, key=key, timeout_sec=5))
    taken = await world.wait(machine, wait_sec=5)
    await _until(lambda: time.has_timer(5 + wire_exec.RESULT_GRACE_SEC))

    time.advance(5 + wire_exec.RESULT_GRACE_SEC)
    lost = await first
    retried = await world.run(headers, key=key, timeout_sec=5)

    assert taken.status_code == 200
    assert (lost.status_code, _code(lost)) == (504, "exec_lost")
    assert (retried.status_code, _code(retried)) == (504, "exec_lost")
    assert (await world.wait(machine)).json() is None


async def test_a_user_past_the_rate_gets_a_429_until_the_minute_is_up(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    time = _timed_hub(monkeypatch)
    monkeypatch.setattr(wire_exec, "MAX_EXECS_PER_MIN_PER_USER", 2)
    machine = await world.join(world.cookie(await world.person("ada@x.y")))
    headers = await world.assistant()
    for _ in range(2):
        call = asyncio.ensure_future(world.run(headers))
        _ = await world.serve(machine, _echo)
        assert (await call).status_code == 200

    refused = await world.run(headers)
    time.advance(60)
    call = asyncio.ensure_future(world.run(headers))
    _ = await world.serve(machine, _echo)

    assert (refused.status_code, _code(refused)) == (429, "exec_limit")
    assert (await call).status_code == 200


async def test_a_host_another_has_taken_the_machine_from_is_handed_no_command(
    world: _World,
) -> None:
    machine = await world.join(world.cookie(await world.person("ada@x.y")))
    successor = uuid.uuid4()
    old = asyncio.ensure_future(world.wait(machine, wait_sec=5))
    await _until(lambda: machine.id in _hub()._waiters)
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET host_instance = $2 WHERE id = $1",
            machine.id,
            successor,
        )

    call = asyncio.ensure_future(world.run(await world.assistant()))
    superseded = await old
    handed = await world.wait(machine, instance=successor, wait_sec=5)
    offer = ExecWait.model_validate(handed.json())
    _ = await world.report(machine, offer.exec_id, _result())

    assert superseded.status_code == 403
    assert (await call).status_code == 200


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
