"""Machine-run routes against a mocked connection and the app's real inbound queue.

``machine_runs_routes_pglite_test.py`` runs the same routes against a real
database; these pin what each route asks of the connection and the queue, and
that a refusal stops nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast, override

import uuid

import pytest

from trackinizer.conftest import (
    ExchangeEngine,
    RecordingPolicy,
    Says,
    set_field_row,
)
from trackinizer.server.access_policy import Principal
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.machine_runs_routes import named_runs
from trackinizer.wire.wire_machines import (
    MACHINE_DISCONNECT_PATH,
    MACHINE_LOGOUT_PATH,
    MACHINE_RUN_STOP_PATH,
    MACHINE_RUNS_PATH,
    RESERVED_NAMES,
    MachineLogout,
    MachineRun,
    MachineRuns,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.access_policy import ResourceKind
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


_MACHINE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_CLAUDE = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_CODEX = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_THEIRS = uuid.UUID("00000000-0000-0000-0000-00000000000c")
_PAST = uuid.UUID("00000000-0000-0000-0000-00000000000d")
_CHAT = uuid.UUID("00000000-0000-0000-0000-0000000000c4")
_SEEN = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class _Hub:
    """The app's event streams, counting nudges."""

    def __init__(self) -> None:
        self.nudges = 0

    def nudge(self) -> None:
        self.nudges += 1


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> _Hub:
    streams = _Hub()
    patch_state(monkeypatch, "hub", streams)
    return streams


def test_listing_shows_the_owners_live_runs_sorted_then_the_past_ones(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL.upper())
    _live_runs()
    engine.conn.fetch.side_effect = [
        [_past_row(_CLAUDE), _past_row(_PAST)],
        [
            _session_row(_CLAUDE, seq=7, chat=f"chat:{_CHAT}"),
            _session_row(_PAST, seq=5),
        ],
    ]

    response = client.get(MACHINE_RUNS_PATH.format(name="box"), params={"past": 1})

    assert response.status_code == 200, response.text
    runs = MachineRuns.model_validate_json(response.text)
    assert [(run.cli, run.session_id) for run in runs.runs] == [
        ("claude", _CLAUDE),
        ("codex", _CODEX),
    ]
    assert runs.runs == [
        MachineRun(
            session_id=_CLAUDE,
            cli="claude",
            run="chat-b",
            cwd="~/w",
            state="idle",
            seq=7,
            conversation_id=_CHAT,
        ),
        MachineRun(session_id=_CODEX, cli="codex"),
    ]
    assert runs.past == [
        MachineRun(
            session_id=_PAST,
            cli="claude",
            run=None,
            cwd="~/p",
            last_seen=_SEEN,
            seq=5,
        ),
    ]
    past, sessions = engine.conn.fetch.call_args_list
    assert past.args[1:] == ("box", TEST_USER_EMAIL.upper(), 1 + 2)
    assert sessions.args[1:] == ([_CLAUDE, _CODEX, _PAST], TEST_USER_EMAIL.upper())


@pytest.mark.asyncio
async def test_named_runs_reads_the_owners_sessions_once_and_names_each_found() -> None:
    chat, alone, theirs = [
        MachineRun(session_id=uuid.uuid4(), cli="claude") for _ in range(3)
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
    run = MachineRun(session_id=uuid.uuid4(), cli="claude")
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
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL)
    engine.conn.fetch.return_value = []

    response = client.get(MACHINE_RUNS_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    assert MachineRuns.model_validate_json(response.text) == MachineRuns(runs=[])
    assert engine.conn.fetch.call_count == 1


@pytest.mark.parametrize("owner", [None, "eve@example.com"], ids=["absent", "theirs"])
def test_a_machine_that_is_not_the_callers_reads_as_none(
    route_client: tuple[TestClient, Store, FakeEngine],
    owner: str | None,
) -> None:
    client, _, engine = route_client
    _own(engine, owner=owner)
    _live_runs()

    listed = client.get(MACHINE_RUNS_PATH.format(name="box"))
    stopped = client.post(_stop_path("box", _CLAUDE))

    assert (listed.status_code, stopped.status_code) == (404, 404)
    assert engine.conn.fetch.call_count == 0
    assert _CLAUDE in _inbound().runs_on("box", account=TEST_USER_EMAIL)


def test_a_reserved_name_is_refused_unread(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    reserved = min(RESERVED_NAMES)

    response = client.get(MACHINE_RUNS_PATH.format(name=reserved))

    assert response.status_code == 422
    assert engine.conn.fetchval.call_count == 0


def test_stopping_a_live_run_refuses_its_drains_and_nudges_the_streams(
    route_client: tuple[TestClient, Store, FakeEngine],
    hub: _Hub,
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL)
    _live_runs()

    stopped = client.post(_stop_path("box", _CLAUDE))
    again = client.post(_stop_path("box", _CLAUDE))
    theirs = client.post(_stop_path("box", _THEIRS))

    assert (stopped.status_code, stopped.content) == (204, b"")
    assert (again.status_code, theirs.status_code) == (404, 404)
    assert _inbound().stopped(_CLAUDE, instance="b" * 32)
    assert not _inbound().stopped(_THEIRS, instance="c" * 32)
    assert hub.nudges == 1


def test_the_assistants_drain_of_a_session_is_no_run_to_stop(
    route_client: tuple[TestClient, Store, FakeEngine],
    hub: _Hub,
) -> None:
    client, _, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL)
    assistant = uuid.uuid4()
    # The assistant's poller names no CLI, even where it names the machine.
    _inbound().mark_poller(assistant, machine="box", account=TEST_USER_EMAIL)

    response = client.post(_stop_path("box", assistant))

    assert response.status_code == 404
    assert _inbound().has_poller(assistant)
    assert hub.nudges == 0


def test_disconnect_revokes_the_host_and_stops_every_owner_run(
    route_client: tuple[TestClient, Store, FakeEngine],
    hub: _Hub,
) -> None:
    client, _, engine = route_client
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": TEST_USER_EMAIL})
    _live_runs()

    response = client.post(MACHINE_DISCONNECT_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    stopped = MachineRuns.model_validate_json(response.text)
    assert [run.session_id for run in stopped.runs] == [_CLAUDE, _CODEX]
    assert stopped.past == []
    assert _inbound().stopped(_CLAUDE, instance="b" * 32)
    assert _inbound().stopped(_CODEX, instance="a" * 32)
    assert list(_inbound().runs_on("box", account="eve@example.com")) == [_THEIRS]
    assert hub.nudges == 1


@pytest.mark.parametrize("owner", [None, TEST_USER_EMAIL], ids=["no owner", "no runs"])
def test_disconnect_that_stops_nothing_nudges_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    hub: _Hub,
    owner: str | None,
) -> None:
    client, _, engine = route_client
    install_identity(make_test_identity(role="admin"))
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": owner})

    response = client.post(MACHINE_DISCONNECT_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    assert MachineRuns.model_validate_json(response.text).runs == []
    assert hub.nudges == 0


def test_logout_disconnects_and_revokes_the_keys_its_logins_made(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, engine = route_client
    _own(engine, owner=TEST_USER_EMAIL)
    set_field_row(engine.conn, {"id": _MACHINE_ID, "owner": TEST_USER_EMAIL})
    _live_runs()
    keys = [uuid.uuid4(), uuid.uuid4()]
    engine.conn.fetch.return_value = [{"id": key} for key in keys]
    forgotten: list[uuid.UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identity_for_key", forgotten.append)

    response = client.post(MACHINE_LOGOUT_PATH.format(name="box"))

    assert response.status_code == 200, response.text
    logout = MachineLogout.model_validate_json(response.text)
    assert ([run.session_id for run in logout.runs], logout.keys) == (
        [_CLAUDE, _CODEX],
        2,
    )
    assert forgotten == keys
    call = engine.conn.fetch.call_args
    assert call is not None
    sql = call.args[0]
    assert isinstance(sql, str)
    assert sql.startswith("UPDATE api_keys SET revoked_at = clock_timestamp()")
    assert "api_keys.revoked_at IS NULL" in sql
    assert call.args[1:] == ("box", TEST_USER_EMAIL, TEST_USER_ID)


def test_logout_of_a_machine_that_is_not_the_callers_revokes_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    client, _, engine = route_client
    _own(engine, owner="eve@example.com")
    _live_runs()

    response = client.post(MACHINE_LOGOUT_PATH.format(name="box"))

    assert response.status_code == 404
    assert (engine.conn.fetchrow.call_count, engine.conn.fetch.call_count) == (0, 0)


def _own(engine: FakeEngine, *, owner: str | None) -> None:
    """Answer the machine's owner lookup with ``owner``."""
    engine.conn.fetchval.return_value = owner


def _live_runs() -> None:
    """Renew the caller's two runs on ``box``, another person's there, and one elsewhere."""
    inbound = _inbound()
    inbound.mark_poller(
        _CODEX,
        machine="box",
        cli="codex",
        instance="a" * 32,
        account=TEST_USER_EMAIL,
    )
    inbound.mark_poller(
        _CLAUDE,
        run="chat-b",
        machine="box",
        cwd="~/w",
        cli="claude",
        instance="b" * 32,
        account=TEST_USER_EMAIL,
    )
    inbound.mark_poller(
        _THEIRS,
        machine="box",
        cli="claude",
        instance="c" * 32,
        account="eve@example.com",
    )
    inbound.mark_poller(
        uuid.uuid4(),
        machine="gx10",
        cli="claude",
        instance="d" * 32,
        account=TEST_USER_EMAIL,
    )


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


def _inbound() -> InboundQueue:
    return cast("InboundQueue", app.state.inbound)


def _stop_path(name: str, session_id: uuid.UUID) -> str:
    return MACHINE_RUN_STOP_PATH.format(name=name, session_id=session_id)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
