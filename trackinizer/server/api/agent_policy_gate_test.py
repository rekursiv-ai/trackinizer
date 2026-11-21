"""The agent policy at a remote agent's start, at a line to it, and at its uploads."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast, override
from unittest.mock import call
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

import pytest

from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.server.agent_policy import Halt, Refusal
from trackinizer.server.api import agent_policy_gate
from trackinizer.server.api.agent_policy_gate import (
    after_stored,
    refuse_line,
    refuse_start,
    remote_agent_hosts,
)
from trackinizer.server.auth import AuthIdentity, ProcessKey
from trackinizer.server.config import Config, RemoteAgentService
from trackinizer.server.session_relay import InputRefusedError, SessionRelay
from trackinizer.wire.wire_session_ir import RecordBody
from trackinizer.wire.wire_session_relay import (
    AgentProcess,
    EventLine,
    NewInput,
    ProcessSpec,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.agent_policy import AgentWork
    from trackinizer.wire.wire_sessions import MessageKind


_AGENTS: Final = "agents@service.invalid"
_SERVICE: Final = RemoteAgentService(name="remote-agents", account=_AGENTS)


@dataclass(slots=True, kw_only=True)
class _Policy:
    """An agent policy that keeps what it is asked and shown, and answers as set."""

    refused: dict[str, Refusal] = field(default_factory=dict[str, Refusal])
    halt: Halt | None = None
    engine: object = None
    """The server's database, which each ask must be given."""

    asked: list[tuple[str, UUID, str]] = field(
        default_factory=list[tuple[str, UUID, str]],
    )
    shown: list[tuple[UUID, int, list[int], list[UUID]]] = field(
        default_factory=list[tuple[UUID, int, list[int], list[UUID]]],
    )

    def serves(self, asker: str) -> bool:
        del asker
        return False

    async def refusal(
        self,
        engine: DatabaseEngine,
        *,
        asker: str,
        session_id: UUID,
        work: AgentWork,
    ) -> Refusal | None:
        self._given(engine)
        self.asked.append((asker, session_id, work))
        return self.refused.get(asker)

    async def stored(
        self,
        engine: DatabaseEngine,
        *,
        session_id: UUID,
        part: int,
        records: Sequence[RecordBody],
        live: Sequence[UUID],
    ) -> Halt | None:
        self._given(engine)
        self.shown.append(
            (session_id, part, [each.idx for each in records], list(live)),
        )
        return self.halt

    def _given(self, engine: object) -> None:
        if engine is not self.engine:
            raise AssertionError(f"asked with {engine!r}, not the server's database")


class _Relay(SessionRelay):
    """The relay, keeping each input it was sent, refusing those for ``full``, and
    reporting each of ``dropped`` too, as a process a dropped socket detached.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[UUID, str, object, str, str]] = []
        self.full: set[UUID] = set()
        self.dropped: list[AgentProcess] = []

    @override
    def processes(self, host_id: UUID) -> list[AgentProcess]:
        return [*super().processes(host_id), *self.dropped]

    @override
    def send_input(
        self,
        session_id: UUID,
        new: NewInput,
        *,
        author: str,
        recipient: str = "",
        booked: MessageKind | None = None,
    ) -> UUID:
        if session_id in self.full:
            raise InputRefusedError("full")
        self.sent.append((session_id, new.kind, new.payload, author, recipient))
        return super().send_input(
            session_id,
            new,
            author=author,
            recipient=recipient,
            booked=booked,
        )


@dataclass(slots=True, kw_only=True)
class _Site:
    """An app's state: the policy, the relay, and the services' hosts.

    The hosts' lookup is faked here; it runs on a scripted connection and on PGlite
    further down.
    """

    engine: FakeEngine = field(default_factory=FakeEngine)
    policy: _Policy | None = field(default_factory=_Policy)
    relay: _Relay = field(default_factory=_Relay)
    service: UUID = field(default_factory=uuid4)
    laptop: UUID = field(default_factory=uuid4)

    def request(self) -> Request:
        app = FastAPI()
        app.state.engine = self.engine
        app.state.config = Config(remote_agent_services=(_SERVICE,))
        app.state.session_relay = self.relay
        app.state.agent_policy = self.policy
        return Request({"type": "http", "app": app, "headers": []})

    async def remote_agent_hosts(
        self,
        conn: object,
        services: Sequence[RemoteAgentService],
    ) -> frozenset[UUID]:
        if conn is not self.engine.conn:
            raise AssertionError(f"read on {conn!r}, not the request's connection")
        return frozenset({self.service}) if services == (_SERVICE,) else frozenset()

    def started(self, session_id: UUID, *, host: UUID) -> UUID:
        process_id = uuid4()
        _ = self.relay.start(
            session_id,
            ProcessSpec(),
            process_id=process_id,
            host_id=host,
            host="remote-agents",
            token=str(uuid4()),
            remote=True,
        )
        return process_id


def _site(monkeypatch: pytest.MonkeyPatch) -> _Site:
    site = _Site()
    assert site.policy is not None
    site.policy.engine = site.engine
    monkeypatch.setattr(
        agent_policy_gate,
        "remote_agent_hosts",
        site.remote_agent_hosts,
    )
    return site


def _person(email: str, *, process: ProcessKey | None = None) -> AuthIdentity:
    return AuthIdentity(
        user_id=uuid4(),
        api_key_id=None if process is None else uuid4(),
        email=email,
        role="writer",
        process=process,
    )


def _key(session_id: UUID, *, host: UUID) -> ProcessKey:
    return ProcessKey(process_id=uuid4(), session_id=session_id, host_id=host)


def _said(idx: int, kind: str) -> RecordBody:
    return RecordBody.model_validate(
        {"idx": idx, "kind": kind, "payload": {"py/object": kind}},
    )


def _dropped(session_id: UUID) -> AgentProcess:
    return AgentProcess(
        session_id=session_id,
        process_id=uuid4(),
        host="remote-agents",
        state="detached",
        reason="disconnected",
        since=datetime(2026, 11, 14, tzinfo=UTC),
    )


async def _always() -> bool:
    return True


_REFUSED: Final = Refusal(status=402, code="no_way", message="Not now.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refuse", "work"),
    [(refuse_start, "start"), (refuse_line, "line")],
    ids=["a start", "a line"],
)
async def test_work_the_policy_refuses_is_answered_as_it_says_to_whoever_asks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refuse: Callable[..., Awaitable[None]],
    work: str,
) -> None:
    site = _site(monkeypatch)
    assert site.policy is not None
    site.policy.refused["Bob@X.y"] = _REFUSED
    session_id = uuid4()
    _ = site.started(session_id, host=site.service)

    with pytest.raises(HTTPException) as refused:
        await refuse(site.request(), _person("Bob@X.y"), session_id=session_id)
    await refuse(site.request(), _person("ada@x.y"), session_id=session_id)

    assert refused.value.status_code == 402
    assert refused.value.detail == {"code": "no_way", "message": "Not now."}
    assert site.policy.asked == [
        ("Bob@X.y", session_id, work),
        ("ada@x.y", session_id, work),
    ]


@pytest.mark.asyncio
async def test_with_no_policy_nothing_is_asked_refused_or_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _site(monkeypatch)
    session_id = uuid4()
    _ = site.started(session_id, host=site.service)
    unset = replace(site, policy=None)

    await refuse_start(unset.request(), _person("ada@x.y"), session_id=session_id)
    await refuse_line(unset.request(), _person("ada@x.y"), session_id=session_id)
    await after_stored(
        unset.request(),
        _person("ada@x.y", process=_key(session_id, host=site.service)),
        session_id=session_id,
        part=0,
        records=[_said(0, "TokenUsage")],
    )

    assert site.policy is not None
    assert (site.policy.asked, site.policy.shown) == ([], [])


@pytest.mark.asyncio
async def test_a_line_is_asked_about_only_for_an_agent_on_a_remote_agent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent on a person's own host, or none at all, is not the policy's."""
    site = _site(monkeypatch)
    assert site.policy is not None
    site.policy.refused["ada@x.y"] = _REFUSED
    own, idle = uuid4(), uuid4()
    _ = site.started(own, host=site.laptop)

    await refuse_line(site.request(), _person("ada@x.y"), session_id=own)
    await refuse_line(site.request(), _person("ada@x.y"), session_id=idle)

    assert site.policy.asked == []


@pytest.mark.asyncio
async def test_a_remote_agents_upload_is_shown_with_the_live_sessions_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person's own key, and a process on a person's own host, show nothing."""
    site = _site(monkeypatch)
    assert site.policy is not None
    session_id, other, own, gone = uuid4(), uuid4(), uuid4(), uuid4()
    for each in (session_id, other):
        _ = site.started(each, host=site.service)
    _ = site.started(own, host=site.laptop)
    site.relay.dropped.append(_dropped(gone))
    remote = _key(session_id, host=site.service)
    laptop = _key(session_id, host=site.laptop)
    request = site.request()

    batches: list[tuple[AuthIdentity, list[RecordBody]]] = [
        (_person("ada@x.y", process=remote), [_said(3, "UserMessage"), _said(4, "X")]),
        (_person("ada@x.y", process=remote), []),
        (_person("ada@x.y", process=laptop), [_said(3, "X")]),
        (_person("ada@x.y"), [_said(3, "X")]),
    ]
    for identity, records in batches:
        await after_stored(
            request,
            identity,
            session_id=session_id,
            part=1,
            records=records,
        )

    assert site.policy.shown == [(session_id, 1, [3, 4], [session_id, other])]
    assert site.relay.sent == []


@pytest.mark.asyncio
async def test_each_live_session_the_policy_names_is_halted_and_told_its_notice(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One it does not name, one on its owner's own host, and one gone are left alone."""
    site = _site(monkeypatch)
    assert site.policy is not None
    bobs, adas, full, also, own, gone = (uuid4() for _ in range(6))
    # One it does not name first, and the full one before one halted: the loop must
    # pass each, not stop at it.
    for session_id in (bobs, adas, full, also):
        _ = site.started(session_id, host=site.service)
    _ = site.started(own, host=site.laptop)
    site.relay.full.add(full)
    site.relay.dropped.append(_dropped(gone))
    site.policy.halt = Halt(
        sessions=frozenset({adas, full, also, own, gone}),
        notice="Stopped, for a reason.",
    )
    viewers = {each: site.relay.lines(each, allowed=_always) for each in (adas, also)}
    for viewer in viewers.values():
        _ = await anext(viewer)

    await after_stored(
        site.request(),
        _person("ada@x.y", process=_key(adas, host=site.service)),
        session_id=adas,
        part=0,
        records=[_said(0, "TokenUsage")],
    )

    # The halts first: a viewer of a chat that was not told would wait for its line.
    assert site.relay.sent == [
        (each, "ModelHalt", {"py/object": "ModelHalt"}, "server", "all")
        for each in (adas, also)
    ]
    assert [record.getMessage() for record in caplog.records] == [
        f"could not halt session {full} now",
    ]
    for viewer in viewers.values():
        assert await anext(viewer) == EventLine(
            kind="CommandOutput",
            payload={
                "py/object": "CommandOutput",
                "text": "Stopped, for a reason.",
                "tone": "error",
                "command": "",
            },
        )


@pytest.mark.asyncio
async def test_the_services_hosts_are_read_by_name_and_account_together() -> None:
    conn = make_conn()
    host = UUID(int=9)
    conn.fetch.return_value = [{"id": host}]
    services = (
        RemoteAgentService(name="remote-agents", account="agents@service.invalid"),
        RemoteAgentService(name="elsewhere", account="else@service.invalid"),
    )

    hosts = await remote_agent_hosts(cast("Conn", conn), services)

    assert hosts == frozenset({host})
    assert conn.fetch.await_args_list == [
        call(
            "SELECT h.id FROM hosts AS h "
            "JOIN unnest($1::text[], $2::text[]) AS s(name, owner) "
            "ON h.name = s.name AND h.owner = s.owner",
            ["remote-agents", "elsewhere"],
            ["agents@service.invalid", "else@service.invalid"],
        ),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_service_is_the_host_its_own_account_enrolled_under_its_name(
    pglite_rollback_conn: Conn,
) -> None:
    conn = pglite_rollback_conn
    hosts = {}
    for name, owner in (
        ("remote-agents", "agents@service.invalid"),
        ("elsewhere", "ada@x.y"),
        ("laptop", "agents@service.invalid"),
    ):
        hosts[name, owner] = await conn.fetchval(
            "INSERT INTO hosts (name, updated_by, owner) VALUES ($1, $2, $2) "
            "RETURNING id",
            name,
            owner,
        )
    services = (
        RemoteAgentService(name="remote-agents", account="agents@service.invalid"),
        RemoteAgentService(name="elsewhere", account="agents@service.invalid"),
    )

    assert await remote_agent_hosts(conn, services) == {
        hosts["remote-agents", "agents@service.invalid"],
    }
    assert await remote_agent_hosts(conn, ()) == frozenset()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
