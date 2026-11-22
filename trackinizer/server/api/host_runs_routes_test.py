"""Host-run routes against a mocked connection and a session relay of the test's own.

``host_runs_routes_pglite_test.py`` runs the same routes against a real database;
these pin what each route asks of the connection and the relay, and that a refusal
stops nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, override
from unittest.mock import call

import asyncio
import uuid

import pytest

from trackinizer.conftest import (
    ExchangeEngine,
    RecordingPolicy,
    Says,
    set_field_row,
)
from trackinizer.server.access_policy import Principal
from trackinizer.server.api._deps import Later
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.host_runs_routes import named_runs
from trackinizer.server.session_relay import SessionRelay
from trackinizer.wire.wire_hosts import (
    HOST_DISCONNECT_PATH,
    HOST_LOGOUT_PATH,
    HOST_RUN_STOP_PATH,
    HOST_RUNS_PATH,
    RESERVED_NAMES,
    HostLogout,
    HostRun,
    HostRuns,
)
from trackinizer.wire.wire_session_relay import AgentProcess, ProcessSpec


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.access_policy import ResourceKind
    from trackinizer.server.store.core import Store


_HOST_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_OTHER_HOST_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_FIRST = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_SECOND = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_ELSEWHERE = uuid.UUID("00000000-0000-0000-0000-00000000000c")
_PAST = uuid.UUID("00000000-0000-0000-0000-00000000000d")
_CHAT = uuid.UUID("00000000-0000-0000-0000-0000000000c4")
_SEEN = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_HOST_SQL = "SELECT id, owner FROM hosts WHERE name = $1"


class _Later(Later):
    """The app's delayed work, kept for the test to read instead of run."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[tuple[float, Callable[[], Awaitable[None]]]] = []

    @override
    def run(
        self,
        delay_sec: float,
        work: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None]:
        self.asked.append((delay_sec, work))
        return asyncio.ensure_future(_nothing())


async def _nothing() -> None:
    """Do nothing, in place of work the test only reads."""


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> SessionRelay:
    started = SessionRelay()
    patch_state(monkeypatch, "session_relay", started)
    return started


@pytest.fixture
def later(monkeypatch: pytest.MonkeyPatch) -> _Later:
    kept = _Later()
    patch_state(monkeypatch, "later", kept)
    return kept


def test_listing_shows_the_hosts_live_processes_by_session_then_its_past_runs(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL.upper())
    _live_runs(relay)
    engine.conn.fetch.side_effect = [
        [_past_row(_SECOND), _past_row(_PAST)],
        [
            _session_row(_SECOND, seq=7, chat=f"chat:{_CHAT}"),
            _session_row(_PAST, seq=5),
        ],
    ]

    response = client.get(HOST_RUNS_PATH.format(name="box"), params={"past": 1})

    assert response.status_code == 200, response.text
    runs = HostRuns.model_validate_json(response.text)
    assert runs.runs == [
        HostRun(session_id=_FIRST, cli="sagent"),
        HostRun(session_id=_SECOND, cli="sagent", seq=7, conversation_id=_CHAT),
    ]
    assert runs.past == [
        HostRun(
            session_id=_PAST,
            cli="claude",
            run=None,
            cwd="~/p",
            last_seen=_SEEN,
            seq=5,
        ),
    ]
    assert engine.conn.fetchrow.call_args_list == [call(_HOST_SQL, "box")]
    past, sessions = engine.conn.fetch.call_args_list
    assert past.args[1:] == ("box", TEST_USER_EMAIL.upper(), 1 + 2)
    assert sessions.args[1:] == ([_FIRST, _SECOND, _PAST], TEST_USER_EMAIL.upper())


@pytest.mark.asyncio
async def test_named_runs_reads_the_owners_sessions_once_and_names_each_found() -> None:
    chat, alone, theirs = [
        HostRun(session_id=uuid.uuid4(), cli="claude") for _ in range(3)
    ]
    engine = ExchangeEngine(
        Says(
            method="fetch",
            sql="SELECT id, seq, agentsession_cli_session_id AS chat FROM inquiries "
            "WHERE id = ANY($1::uuid[]) AND kind = 'AgentSession' "
            "AND lower(account) = lower($2)",
            args=([chat.session_id, alone.session_id, theirs.session_id], "Ada@x.io"),
            # As the driver may hand an id over: text.
            answer=[
                {"id": str(alone.session_id), "seq": 4, "chat": "campaign:issue-3"},
                {"id": str(chat.session_id), "seq": 3, "chat": f"chat:{_CHAT}"},
            ],
        ),
    )

    named = await named_runs(
        engine.conn,
        [chat, alone, theirs],
        owner="Ada@x.io",
        policy=None,
        principal=None,
    )

    assert engine.done
    assert named == [
        chat.model_copy(update={"seq": 3, "conversation_id": _CHAT}),
        alone.model_copy(update={"seq": 4}),
        theirs,
    ]
    assert (
        await named_runs(engine.conn, [], owner="Ada@x.io", policy=None, principal=None)
        == []
    )


@pytest.mark.asyncio
async def test_named_runs_names_only_a_session_the_callers_policy_reads() -> None:
    run = HostRun(session_id=uuid.uuid4(), cli="claude")
    engine = ExchangeEngine(
        Says(
            method="fetch",
            sql="SELECT id, seq, agentsession_cli_session_id AS chat FROM inquiries "
            "WHERE id = ANY($1::uuid[]) AND kind = 'AgentSession' "
            "AND lower(account) = lower($2) AND (EXISTS (SELECT 1 FROM inquiries "
            "readable_inquiry WHERE readable_inquiry.id = inquiries.id AND "
            "(readable_inquiry.access_group = $3)))",
            args=([run.session_id], "ada@x.io", "g1"),
            # The policy hides the session from the caller, so the database finds none.
            answer=[],
        ),
    )

    named = await named_runs(
        engine.conn,
        [run],
        owner="ada@x.io",
        policy=_GroupPolicy(),
        principal=Principal(email="ada@x.io", role="writer", groups=frozenset({"g1"})),
    )

    assert engine.done
    assert named == [run]


def test_listing_with_no_runs_reads_no_sessions(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL)
    # Another host's process is no run of this one.
    _start(relay, _ELSEWHERE, host_id=_OTHER_HOST_ID)
    engine.conn.fetch.return_value = []

    response = client.get(HOST_RUNS_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    assert HostRuns.model_validate_json(response.text) == HostRuns(runs=[])
    assert engine.conn.fetch.call_count == 1


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"id": str(_HOST_ID), "owner": None},
        {"id": str(_HOST_ID), "owner": "eve@example.com"},
    ],
    ids=["absent", "nobody's", "theirs"],
)
def test_a_host_that_is_not_the_callers_reads_as_none(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
    later: _Later,
    row: dict[str, object] | None,
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, row)
    _live_runs(relay)
    running = relay.processes(_HOST_ID)

    listed = client.get(HOST_RUNS_PATH.format(name="box"))
    stopped = client.post(_stop_path("box", _FIRST))

    assert (listed.status_code, stopped.status_code) == (404, 404)
    assert stopped.json() == {"detail": "no host box of yours", "code": "not_found"}
    assert engine.conn.fetch.call_count == 0
    assert relay.processes(_HOST_ID) == running
    assert later.asked == []


def test_a_reserved_name_is_refused_unread(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    reserved = min(RESERVED_NAMES)

    response = client.get(HOST_RUNS_PATH.format(name=reserved))

    assert response.status_code == 422
    assert engine.conn.fetchrow.call_count == 0


def test_stopping_a_live_process_stops_it_as_its_sessions_own_stop_does(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
    later: _Later,
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL)
    _live_runs(relay)

    stopped = client.post(_stop_path("box", _FIRST))
    again = client.post(_stop_path("box", _FIRST))
    elsewhere = client.post(_stop_path("box", _ELSEWHERE))

    assert (stopped.status_code, stopped.content) == (204, b"")
    assert again.json() == {
        "detail": f"no live session {_FIRST} on box",
        "code": "not_found",
    }
    assert (again.status_code, elsewhere.status_code) == (404, 404)
    detached = _process(relay, _FIRST)
    assert (detached.state, detached.reason) == ("detached", "stopped")
    assert relay.processes(_HOST_ID) == [_process(relay, _SECOND)]
    assert relay.processes(_OTHER_HOST_ID) == [_process(relay, _ELSEWHERE)]
    # Its key's revoke waits the minute the session's own stop gives it.
    assert [delay_sec for delay_sec, _ in later.asked] == [60.0]


def test_disconnect_revokes_the_host_and_lists_the_processes_it_stopped(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, _host_row(owner=TEST_USER_EMAIL))
    _live_runs(relay)

    response = client.post(HOST_DISCONNECT_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    assert HostRuns.model_validate_json(response.text) == HostRuns(
        runs=[
            HostRun(session_id=_FIRST, cli="sagent"),
            HostRun(session_id=_SECOND, cli="sagent"),
        ],
    )
    assert [
        (each.state, each.reason)
        for each in (_process(relay, _FIRST), _process(relay, _SECOND))
    ] == [("detached", "host revoked")] * 2
    assert relay.processes(_OTHER_HOST_ID) == [_process(relay, _ELSEWHERE)]


@pytest.mark.parametrize("running", [False, True], ids=["nothing", "a process"])
def test_an_admin_disconnects_a_host_nobody_owns_and_lists_what_it_stopped(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
    *,
    running: bool,
) -> None:
    client, _, engine = route_client
    install_identity(make_test_identity(role="admin"))
    set_field_row(engine.conn, _host_row(owner=None))
    if running:
        _start(relay, _FIRST, host_id=_HOST_ID, remote=True)

    response = client.post(HOST_DISCONNECT_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    assert HostRuns.model_validate_json(response.text) == HostRuns(
        runs=[HostRun(session_id=_FIRST, cli="sagent")] if running else [],
    )


def test_logout_disconnects_and_revokes_the_keys_its_logins_made(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, engine = route_client
    set_field_row(engine.conn, _host_row(owner=TEST_USER_EMAIL))
    _live_runs(relay)
    keys = [uuid.uuid4(), uuid.uuid4()]
    # The disconnect revokes the host's process keys first, then the logout its logins'.
    engine.conn.fetch.side_effect = [
        [{"id": uuid.uuid4()}],
        [{"id": key} for key in keys],
    ]
    forgotten: list[uuid.UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identity_for_key", forgotten.append)
    hosts: list[uuid.UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identities_for_host", hosts.append)

    response = client.post(HOST_LOGOUT_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    assert HostLogout.model_validate_json(response.text) == HostLogout(
        runs=[
            HostRun(session_id=_FIRST, cli="sagent"),
            HostRun(session_id=_SECOND, cli="sagent"),
        ],
        keys=2,
    )
    assert relay.processes(_HOST_ID) == []
    # The host's process keys leave the cache by their host, its logins' by their ids.
    assert (hosts, forgotten) == ([_HOST_ID], keys)
    revoked = engine.conn.fetch.call_args
    assert revoked is not None
    sql = revoked.args[0]
    assert isinstance(sql, str)
    assert sql.startswith("UPDATE api_keys SET revoked_at = clock_timestamp()")
    assert "api_keys.revoked_at IS NULL" in sql
    assert revoked.args[1:] == ("box", TEST_USER_EMAIL, TEST_USER_ID)


def test_logout_of_a_host_that_is_not_the_callers_revokes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    relay: SessionRelay,
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, _host_row(owner="eve@example.com"))
    _live_runs(relay)
    running = relay.processes(_HOST_ID)

    response = client.post(HOST_LOGOUT_PATH.format(name="box"))

    assert response.status_code == 404
    assert engine.conn.fetchrow.call_args_list == [call(_HOST_SQL, "box")]
    assert engine.conn.fetch.call_count == 0
    assert relay.processes(_HOST_ID) == running


def _own(engine: FakeEngine, *, owner: str) -> None:
    """Answer the host's lookup with ``box``, owned by ``owner``."""
    set_field_row(engine.conn, _host_row(owner=owner))


def _host_row(*, owner: str | None) -> dict[str, object]:
    # The id as text, as a driver may hand it: the relay holds the host by its UUID.
    return {"id": str(_HOST_ID), "owner": owner}


def _live_runs(relay: SessionRelay) -> None:
    """Start two processes on ``box``, the one listed second first, and one elsewhere."""
    for session_id, host_id in (
        (_SECOND, _HOST_ID),
        (_FIRST, _HOST_ID),
        (_ELSEWHERE, _OTHER_HOST_ID),
    ):
        _start(relay, session_id, host_id=host_id)


def _start(
    relay: SessionRelay,
    session_id: uuid.UUID,
    *,
    host_id: uuid.UUID,
    remote: bool = False,
) -> None:
    """Offer a new process of ``session_id`` to the host, as a start does."""
    process_id = uuid.uuid4()
    _ = relay.start(
        session_id,
        ProcessSpec(),
        process_id=process_id,
        host_id=host_id,
        host="box",
        token=f"key-{process_id.hex}",
        remote=remote,
    )


def _process(relay: SessionRelay, session_id: uuid.UUID) -> AgentProcess:
    process = relay.process(session_id)
    assert process is not None
    return process


def _past_row(session_id: uuid.UUID) -> dict[str, object]:
    return {
        "session_id": session_id,
        "run": None,
        "cli": "claude",
        "cwd": "~/p",
        "last_seen": _SEEN,
        "ended": None,
    }


@dataclass(slots=True, kw_only=True)
class _GroupPolicy(RecordingPolicy):
    """A policy that lets a reader read only the rows of their own access group."""

    @override
    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        del kind
        params.extend(sorted(principal.groups))
        return f"{alias}.access_group = ${len(params)}"


def _session_row(
    session_id: uuid.UUID,
    *,
    seq: int,
    chat: str | None = None,
) -> dict[str, object]:
    return {"id": session_id, "seq": seq, "chat": chat}


def _stop_path(name: str, session_id: uuid.UUID) -> str:
    return HOST_RUN_STOP_PATH.format(name=name, session_id=session_id)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
