"""Tests for the shared API dependencies, chiefly :func:`tag_kind`.

``tag_kind`` sits on every inquiry read path, so its output IS the wire
contract. The serialization tests below pin that shape against
``jsonable_encoder``, the reference implementation the route used before the
dataclass fast path replaced it: the encoder is slow (measured 5.3ms of a
9ms 50-row listing) but definitionally correct, so it stays here as the
oracle even though it no longer runs in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncio
import dataclasses
import decimal
import logging
import types
import typing

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request

import pytest

from trackinizer.conftest import make_conn, make_store
from trackinizer.server.api import _deps
from trackinizer.server.api._deps import (
    Later,
    auth_is_disabled,
    get_assistant,
    get_chat_orgs,
    get_default_remote_agent_service,
    get_host_changes,
    get_later,
    get_relay,
    get_remote_agent_services,
    get_replays,
    get_secrets,
    get_session_relay,
    get_staff,
    get_store,
    get_terminal_relay,
    get_tool_line_bytes,
    get_tool_relay,
    host_changes,
    host_closed,
    host_idle_sec,
    hosts_changed,
    tag_kind,
    tag_row,
    terminal_relay,
    tool_relay,
)
from trackinizer.server.api.tool_access import ToolAccess
from trackinizer.server.config import Assistant, Config, RemoteAgentService
from trackinizer.server.session_relay import SessionRelay
from trackinizer.server.terminals import Terminal, TerminalRelay
from trackinizer.server.tool_lines import ResultIds
from trackinizer.server.tool_relay import (
    ToolConnection,
    ToolConnectionBusyError,
    ToolRelay,
)
from trackinizer.types import inquiries
from trackinizer.types.cost import Cost
from trackinizer.types.inquiries import (
    AgentSession,
    Artifact,
    Belief,
    CodeChange,
    Experiment,
    Inquiry,
    InquiryEdge,
    Issue,
    Paper,
    WebResult,
    WebSearch,
)


if typing.TYPE_CHECKING:
    from trackinizer.mock_test_support import MockConnection
    from trackinizer.server.api.tool_access import Check
    from trackinizer.server.tool_relay import ToolSocket


_INQUIRIES_NS: dict[str, object] = dict(vars(inquiries))


def _resolve(annotation: object) -> object:
    """Turn a source-text or aliased annotation into the type it names."""
    if isinstance(annotation, str):
        # ``from __future__ import annotations`` leaves every ``field.type``
        # as source text; evaluate it in the defining module's namespace,
        # which is what ``get_type_hints`` does for the top-level classes.
        annotation = eval(annotation, _INQUIRIES_NS)  # noqa: S307 -- module's own annotations.  # pyright: ignore[reportAny] -- eval's stub is Any, but this evaluates the module's own annotations.
    if isinstance(annotation, typing.TypeAliasType):
        # PEP 695 aliases (``type Actor = str``, ``type Status = Literal[...]``)
        # are values rather than types; unwrap to whatever they name.
        return _resolve(cast(object, annotation.__value__))
    return annotation


# Derived from the annotation rather than hand-written per kind. A field added to any
# Inquiry is populated -- and so serialized, and so compared against the oracle -- with
# no edit here, PROVIDED its type already has an arm below. A type with no arm raises
# rather than silently substituting something else, so the failure names the missing
# case instead of hiding it; extending this function is then the deliberate act it
# should be.
def _type_args(annotation: object) -> tuple[object, ...]:
    """Return runtime annotation arguments with the untyped stub narrowed."""
    return typing.get_args(annotation)


def _sample(annotation: object) -> object:
    """One non-``None`` value satisfying an annotation."""
    annotation = _resolve(annotation)
    origin = typing.get_origin(annotation)
    if origin is types.UnionType:
        # Reached only via a nested annotation (a tuple of optionals, say);
        # ``_samples`` is what expands a union at the field level.
        return _sample(
            next(a for a in _type_args(annotation) if a is not type(None)),
        )
    if origin is tuple:
        return (_sample(_type_args(annotation)[0]),)
    if origin is dict:
        # ``Experiment.config`` -- the one JSONB field, whose leaves are
        # caller-supplied and so the only place an unhandled type can enter.
        #
        # The nested value must REQUIRE conversion. A dict of JSON-native
        # leaves is returned unchanged whether or not the serializer recurses
        # into dicts at all, so it cannot tell a working dict arm from a
        # missing one -- verified by deleting the arm and watching this test
        # still pass. A datetime leaf makes the branch observable.
        return {
            "nested": {
                "at": datetime(2026, 8, 19, 10, 12, 23, 55_030, tzinfo=UTC),
                "n": 1,
                "s": "x",
                "none": None,
            },
        }
    if origin is typing.Literal:
        return _type_args(annotation)[0]
    if annotation is datetime:
        return datetime(2026, 8, 19, 10, 12, 23, 55_030, tzinfo=UTC)
    if annotation is UUID:
        return uuid4()
    if annotation is decimal.Decimal:
        # No Inquiry field is Decimal-typed today; the arm exists so a union
        # carrying one is sampled rather than raising, which is how the
        # oracle would first see it.
        return decimal.Decimal("12.50")
    if annotation is Cost:
        return Cost(agent_usd=0.5, resource_usd=0.25)
    if annotation is bool:
        return True
    if annotation is int:
        return 7
    if annotation is float:
        return 1.5
    if annotation is str:
        return "sample"
    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        return annotation(
            **{
                field.name: _sample(field.type)
                for field in dataclasses.fields(annotation)
            },
        )
    raise AssertionError(f"no sample value for annotation {annotation!r}")


# The kwargs are built from the annotations, so their static type is ``object`` and no
# checker can match them to each field. The construction is verified at RUNTIME instead,
# by ``test_every_field_is_populated``.
def _populated[T: Inquiry](subclass: type[T]) -> T:
    """One instance of ``subclass`` with every field set to a real value."""
    hints = cast(dict[str, object], typing.get_type_hints(subclass, _INQUIRIES_NS))
    return subclass(
        **{  # pyright: ignore[reportArgumentType] -- Dynamic dataclass fields are validated by the runtime oracle.  # ty: ignore[invalid-argument-type] -- Dynamic dataclass fields are validated by the runtime oracle.  # pyrefly: ignore[bad-argument-type] -- Dynamic dataclass fields are validated by the runtime oracle.
            field.name: _sample(hints[field.name])
            for field in dataclasses.fields(subclass)
        },
    )


# Named explicitly rather than walked from ``__subclasses__``: the sub-kinds
# nest under Artifact, and a test session that imports the module twice sees
# every class listed twice.
_KINDS: tuple[type[Inquiry], ...] = (
    Issue,
    Artifact,
    Experiment,
    Paper,
    Belief,
    CodeChange,
    WebResult,
    WebSearch,
    AgentSession,
)


def _issue(
    *,
    subject_id: UUID | None = None,
    produces: tuple[InquiryEdge, ...] = (),
) -> Issue:
    """Build one Issue carrying every scalar type the serializer must handle."""
    return Issue(
        id=uuid4() if subject_id is None else subject_id,
        seq=42,
        account="agent@example.com",
        status="active",
        title="a title",
        created=datetime(2026, 8, 19, 10, 12, 23, 55_030, tzinfo=UTC),
        modified=datetime(2026, 8, 19, 10, 12, 35, 995_201, tzinfo=UTC),
        produces=produces,
    )


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


class TestGetAssistant:
    def test_the_assistant_is_the_configs(self) -> None:
        app = FastAPI()
        kb = Assistant(actor="scout", email="kb@example.com")
        app.state.config = Config(assistant=kb)
        assert get_assistant(_request(app)) == kb

    def test_none_without_a_config_or_an_assistant(self) -> None:
        app = FastAPI()
        assert get_assistant(_request(app)) is None
        app.state.config = "not a config"
        assert get_assistant(_request(app)) is None
        app.state.config = Config()
        assert get_assistant(_request(app)) is None


class TestGetChatOrgs:
    def test_a_server_is_individual_unless_its_config_says_otherwise(
        self,
    ) -> None:
        app = FastAPI()
        assert get_chat_orgs(_request(app)) == "individual"
        app.state.config = "not a config"
        assert get_chat_orgs(_request(app)) == "individual"
        app.state.config = Config()
        assert get_chat_orgs(_request(app)) == "individual"
        app.state.config = Config(chat_orgs="single")
        assert get_chat_orgs(_request(app)) == "single"


def test_get_relay_makes_one_relay_and_keeps_it() -> None:
    app = FastAPI()

    relay = get_relay(_request(app))

    assert get_relay(_request(app)) is relay


def test_get_session_relay_makes_one_relay_keeps_it_and_replaces_a_stranger() -> None:
    app = FastAPI()

    relay = get_session_relay(_request(app))
    kept = get_session_relay(_request(app))
    app.state.session_relay = object()
    replaced = get_session_relay(_request(app))

    assert type(relay) is SessionRelay
    assert kept is relay
    assert type(replaced) is SessionRelay
    assert replaced is not relay


def test_get_secrets_is_the_apps_backend_or_none() -> None:
    app = FastAPI()
    app.state.secrets = None
    assert get_secrets(_request(app)) is None


def test_get_store_is_the_apps() -> None:
    app = FastAPI()
    store, _engine = make_store(make_conn())
    app.state.store = store

    assert get_store(_request(app)) is store


def test_get_replays_makes_one_and_replaces_a_stranger() -> None:
    app = FastAPI()
    app.state.replays = "not replays"

    replays = get_replays(_request(app))

    assert get_replays(_request(app)) is replays


class TestTerminalHub:
    def test_one_relay_per_app_with_the_configs_stun(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store(make_conn())
        app.state.config = Config(terminal_stun=("stun:stun.example.com:3478",))

        relay = get_terminal_relay(_request(app))

        assert relay.ice_servers == ("stun:stun.example.com:3478",)
        assert terminal_relay(app) is relay

    def test_no_stun_without_a_config(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store(make_conn())

        assert terminal_relay(app).ice_servers == ()

    @pytest.mark.asyncio
    async def test_liveness_asks_the_apps_store(self) -> None:
        conn = make_conn()
        conn.fetchval.return_value = False
        app = FastAPI()
        app.state.store, _engine = make_store(conn)
        term = Terminal(
            id=uuid4(),
            user="ada@example.com",
            host_id=uuid4(),
            host="gx10",
            rows=24,
            cols=80,
            opened_at=0.0,
        )

        assert await terminal_relay(app)._still_alive(term) is False

        assert conn.fetchval.await_args is not None
        assert conn.fetchval.await_args.args[1] == term.host_id


def test_auth_is_disabled_only_when_the_config_says_so() -> None:
    app = FastAPI()
    assert auth_is_disabled(_request(app)) is False
    app.state.config = Config()
    assert auth_is_disabled(_request(app)) is False
    app.state.config = Config(auth_disabled=True)
    assert auth_is_disabled(_request(app)) is True


def test_the_tool_line_cap_is_the_configs_or_its_default() -> None:
    """An agent's ``Write`` of 8 MiB fits the default, as JSON."""
    app = FastAPI()
    assert get_tool_line_bytes(_request(app)) == 16 << 20
    app.state.config = Config(tool_line_bytes=1024)
    assert get_tool_line_bytes(_request(app)) == 1024


def test_get_staff_is_the_configs_patterns_or_nobody() -> None:
    app = FastAPI()
    assert get_staff(_request(app)) == ()
    app.state.config = Config(staff=("*@example.com",))
    assert get_staff(_request(app)) == ("*@example.com",)


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False])
async def test_a_terminal_is_alive_while_its_host_and_owner_are(
    *,
    live: bool,
) -> None:
    conn = make_conn()
    conn.fetchval.return_value = live
    store, _engine = make_store(conn)
    term = Terminal(
        id=uuid4(),
        user="ada@example.com",
        host_id=uuid4(),
        host="gx10",
        rows=24,
        cols=80,
        opened_at=0.0,
    )

    assert await _deps._alive(term, store=store) is live

    assert conn.fetchval.await_args is not None
    sql, host_id = conn.fetchval.await_args.args
    assert host_id == term.host_id
    assert isinstance(sql, str)
    assert "c.revoked_at IS NULL" in sql
    assert "u.status = 'active'" in sql


def test_replays_are_made_once_and_kept() -> None:
    app = FastAPI()

    replays = get_replays(_request(app))

    assert get_replays(_request(app)) is replays
    app.state.replays = "not replays"
    assert get_replays(_request(app)) is not replays


def test_host_changes_are_made_once_and_a_write_bumps_its_owner() -> None:
    app = FastAPI()
    changes = host_changes(app)
    assert host_changes(app) is changes
    assert get_host_changes(_request(app)) is changes
    before = changes.version("ada@example.com")

    hosts_changed(_request(app), owner="ada@example.com")
    hosts_changed(_request(app), owner=None)

    assert changes.version("ada@example.com") == before + 1


class TestTheTerminalHub:
    def test_it_is_made_once_and_kept(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store()

        relay = terminal_relay(app)

        assert terminal_relay(app) is relay
        assert get_terminal_relay(_request(app)) is relay

    def test_it_hands_out_the_configured_stun_servers_or_none(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store()
        assert terminal_relay(app).ice_servers == ()

        other = FastAPI()
        other.state.store, _engine = make_store()
        other.state.config = Config(terminal_stun=("stun:stun.example.org:3478",))
        assert terminal_relay(other).ice_servers == ("stun:stun.example.org:3478",)


async def _pass_through(
    connection: ToolConnection,
    frame: bytes,
) -> tuple[bytes, bytes]:
    """Let every frame through to the host, and answer the agent nothing."""
    del connection
    return frame, b""


def _pipe(*, instance: UUID | None = None) -> ToolConnection:
    return ToolConnection(
        id=uuid4(),
        host_id=uuid4(),
        agent=cast("ToolSocket", AsyncMock()),
        asker="ada@example.com",
        api_key_id=uuid4(),
        inbound=_pass_through,
        opened=0.0,
        instance=instance,
        results=ResultIds(line_bytes=1 << 10),
    )


_KB = Assistant(actor="scout", email="kb@example.com")


def _app_with_assistant(
    monkeypatch: pytest.MonkeyPatch,
    failed: Check | None = None,
    *,
    conn: MockConnection | None = None,
) -> tuple[FastAPI, AsyncMock]:
    """Return an app on ``conn`` whose rule fails ``failed``, and the rule as asked."""
    app = FastAPI()
    app.state.store, _engine = make_store(conn)
    app.state.config = Config(assistant=_KB)
    rule = AsyncMock(return_value=ToolAccess(host_id=uuid4(), failed=failed))
    monkeypatch.setattr(_deps, "tool_access", rule)
    return app, rule


@pytest.mark.asyncio
async def test_the_connection_relay_is_made_once_and_asks_if_a_connection_still_qualifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rule = _app_with_assistant(monkeypatch)

    relay = tool_relay(app)

    assert tool_relay(app) is relay
    assert get_tool_relay(_request(app)) is relay
    assert await relay._alive(_pipe()) is True
    rule.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_connection_relay_holds_the_configs_connections_and_dial_back() -> (
    None
):
    app = FastAPI()
    app.state.store, _engine = make_store()
    app.state.config = Config(
        tool_connections_per_host=1,
        tool_dial_back_sec=0.01,
        tool_line_bytes=4_096,
    )
    relay = tool_relay(app)
    host_id = uuid4()
    connection = _join(relay, host_id)

    with pytest.raises(ToolConnectionBusyError) as busy:
        _ = _join(relay, host_id)
    never = asyncio.Future[bytes | None]()
    async with asyncio.timeout(5):
        await relay.serve_agent(connection, lambda: never)

    assert busy.value.limit == 1
    assert connection.ended.result()[1] == "the daemon did not dial back in time"
    assert connection.results._line_bytes == 4_096


@pytest.mark.asyncio
async def test_the_connection_relay_holds_the_default_connections_without_a_config() -> (
    None
):
    app = FastAPI()
    app.state.store, _engine = make_store()
    relay = tool_relay(app)
    host_id = uuid4()
    for _ in range(Config().tool_connections_per_host):
        _ = _join(relay, host_id)

    with pytest.raises(ToolConnectionBusyError):
        _ = _join(relay, host_id)


@pytest.mark.asyncio
async def test_a_hosts_idle_time_is_the_shorter_of_its_connections_and_its_terminals() -> (
    None
):
    app = FastAPI()
    now = [100.0]
    connections = app.state.tool_relay = ToolRelay(
        alive=AsyncMock(),
        connections_per_host=1,
        dial_back_sec=1,
        line_bytes=1 << 10,
        clock=lambda: now[0],
    )
    app.state.terminals = TerminalRelay(alive=AsyncMock(), clock=lambda: now[0])
    host_id = uuid4()
    now[0] += 40
    assert host_idle_sec(app, host_id) == 40

    connection = _join(connections, host_id)
    connections.called(connection, "c1")
    now[0] += 3_600
    assert host_idle_sec(app, host_id) == 0
    connections.acked(connection, "c1")
    now[0] += 9

    assert host_idle_sec(app, host_id) == 9
    assert host_idle_sec(app, uuid4()) == 3_649


@pytest.mark.asyncio
async def test_a_host_closed_ends_its_connections_terminals_and_agent_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    closed: list[tuple[str, UUID]] = []

    def connections_closed(relay: ToolRelay, host_id: UUID) -> None:
        del relay
        closed.append(("tool connections", host_id))

    async def terminals_closed(relay: TerminalRelay, host_id: UUID) -> None:
        del relay
        closed.append(("terminals", host_id))

    async def processes_stopped(relay: SessionRelay, host_id: UUID) -> None:
        del relay
        closed.append(("agent processes", host_id))

    monkeypatch.setattr(ToolRelay, "close_host", connections_closed)
    monkeypatch.setattr(TerminalRelay, "close_host", terminals_closed)
    monkeypatch.setattr(SessionRelay, "stop_host", processes_stopped)
    app.state.tool_relay = ToolRelay(
        alive=AsyncMock(),
        connections_per_host=1,
        dial_back_sec=1,
        line_bytes=1 << 10,
    )
    app.state.terminals = TerminalRelay(alive=AsyncMock())
    host_id = uuid4()

    await host_closed(app, host_id)

    assert closed == [
        ("tool connections", host_id),
        ("terminals", host_id),
        ("agent processes", host_id),
    ]


@pytest.mark.asyncio
async def test_later_runs_its_work_after_the_delay_on_its_sleep_and_logs_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    asked: list[float] = []
    go = asyncio.Event()

    async def sleep(seconds: float) -> None:
        asked.append(seconds)
        _ = await go.wait()

    later = Later(sleep=sleep)
    ran: list[str] = []

    async def work() -> None:
        ran.append("work")

    async def fails() -> None:
        raise RuntimeError("database gone")

    tasks = [later.run(60.0, work), later.run(60.0, fails)]
    await asyncio.sleep(0)
    assert (asked, ran) == ([60.0, 60.0], [])
    # Held while it waits, so none is collected before it runs; let go once done.
    assert later._waiting == set(tasks)

    go.set()
    _ = await asyncio.gather(*tasks)

    assert later._waiting == set()
    assert ran == ["work"]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "delayed work failed",
    ]


def test_the_remote_agent_services_and_the_default_are_the_configs_or_none() -> None:
    app = FastAPI()
    request = _request(app)
    assert (
        get_remote_agent_services(request),
        get_default_remote_agent_service(request),
    ) == ((), "")
    service = RemoteAgentService(name="remote-agents", account="a@service.invalid")
    app.state.config = Config(
        remote_agent_services=(service,),
        default_remote_agent_service="remote-agents",
    )

    assert (
        get_remote_agent_services(request),
        get_default_remote_agent_service(request),
    ) == ((service,), "remote-agents")


def test_the_app_holds_one_later_made_on_first_use() -> None:
    app = FastAPI()

    later = get_later(_request(app))

    assert get_later(_request(app)) is later
    assert later.sleep is asyncio.sleep


def _join(relay: ToolRelay, host_id: UUID) -> ToolConnection:
    """Queue an agent that nothing will serve, to fill a slot."""
    return relay.join(
        host_id,
        agent=cast("ToolSocket", AsyncMock()),
        asker="ada@example.com",
        api_key_id=uuid4(),
        inbound=_pass_through,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [None, "asker"])
async def test_the_sweep_asks_the_browser_form_of_a_connection_a_browser_opened(
    monkeypatch: pytest.MonkeyPatch,
    failed: Check | None,
) -> None:
    app, rule = _app_with_assistant(monkeypatch, failed)
    connection = dataclasses.replace(_pipe(instance=uuid4()), api_key_id=None)

    assert await _deps._connection_alive(connection, app=app) is (failed is None)

    assert rule.await_args is not None
    assert rule.await_args.kwargs["api_key_id"] == "browser"
    assert rule.await_args.kwargs["asker"] == connection.asker


@pytest.mark.asyncio
async def test_without_an_assistant_the_sweep_asks_the_rule_of_the_keys_own_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rule = _app_with_assistant(monkeypatch)
    app.state.config = Config()

    assert await _deps._connection_alive(_pipe(), app=app) is True

    assert rule.await_args is not None
    assert rule.await_args.kwargs["assistant"] is None


@pytest.mark.asyncio
async def test_the_sweep_asks_the_rule_of_a_connections_key_asker_host_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    app, rule = _app_with_assistant(monkeypatch, conn=conn)
    connection = _pipe(instance=uuid4())

    assert await _deps._connection_alive(connection, app=app) is True

    assert rule.await_args is not None
    assert rule.await_args.args == (conn,)
    assert rule.await_args.kwargs == {
        "assistant": _KB,
        "api_key_id": connection.api_key_id,
        "asker": connection.asker,
        "host": connection.host_id,
        "instance": connection.instance,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [None, "key", "instance"])
async def test_a_connection_no_longer_allowed_warns_once_naming_the_check_it_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed: Check | None,
) -> None:
    app, _ = _app_with_assistant(monkeypatch, failed)
    connection = _pipe()

    assert await _deps._connection_alive(connection, app=app) is (failed is None)

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned == (
        []
        if failed is None
        else [
            (
                f"tool connection no longer allowed: connection={connection.id} "
                f"host_id={connection.host_id} check={failed}"
            ),
        ]
    )


class TestAuthIsDisabled:
    def test_only_a_config_that_says_so_disables_auth(self) -> None:
        app = FastAPI()
        assert auth_is_disabled(_request(app)) is False
        app.state.config = "not a config"
        assert auth_is_disabled(_request(app)) is False
        app.state.config = Config()
        assert auth_is_disabled(_request(app)) is False
        app.state.config = Config(auth_disabled=True)
        assert auth_is_disabled(_request(app)) is True


def test_the_staff_are_the_configs_patterns_or_nobody() -> None:
    app = FastAPI()
    assert get_staff(_request(app)) == ()
    app.state.config = "not a config"
    assert get_staff(_request(app)) == ()
    app.state.config = Config(staff=("*@example.com",))
    assert get_staff(_request(app)) == ("*@example.com",)


class TestTagRow:
    def test_a_known_row_is_the_encoder_output_plus_its_kind(self) -> None:
        issue = _issue()
        expected = cast(dict[str, object], jsonable_encoder(issue))
        expected["kind"] = "Issue"
        assert tag_row(issue) == expected

    @pytest.mark.parametrize("subclass", _KINDS)
    def test_named_fields_are_the_whole_rows_keys_in_order(
        self,
        subclass: type[Inquiry],
    ) -> None:
        instance = _populated(subclass)
        whole = tag_row(instance)
        for names in (
            frozenset({"id", "title", "kind"}),
            frozenset({"labels", "created", "judgement"}),
            frozenset(whole),
        ):
            named = tag_row(instance, names)
            assert named == {key: value for key, value in whole.items() if key in names}
            assert list(named) == [key for key in whole if key in names]

    def test_a_field_not_named_is_never_serialized(self) -> None:
        # A list sends 50-1,000 rows; walking an Experiment's config and outcome
        # only to drop them was most of a slim list's time.
        experiment = dataclasses.replace(
            _populated(Experiment),
            config={"unserializable": {"a set"}},
        )
        assert tag_row(experiment, frozenset({"id", "kind"})) == {
            "id": str(experiment.id),
            "kind": "Experiment",
        }
        with pytest.raises(TypeError, match="set"):
            tag_row(experiment, frozenset({"config"}))


class TestTagKind:
    def test_none_passes_through(self) -> None:
        assert tag_kind(None) is None

    def test_adds_kind_discriminator(self) -> None:
        # Clients switch on ``kind``; the column does not carry it, so the
        # serializer synthesizes it from the concrete class.
        payload = tag_kind(_issue())
        assert payload is not None
        assert payload["kind"] == "Issue"

    def test_matches_jsonable_encoder_exactly(self) -> None:
        # The oracle. A faster serializer that changes one field is a
        # protocol break, not an optimization.
        issue = _issue()
        payload = tag_kind(issue)
        expected = cast(dict[str, object], jsonable_encoder(issue))
        expected["kind"] = "Issue"
        assert payload == expected

    def test_datetimes_are_iso_8601(self) -> None:
        # ``str(datetime)`` yields a SPACE separator, which parses in Python
        # and breaks every other language's ISO reader. The distinction is
        # invisible until a consumer outside this repo tries to read it.
        payload = tag_kind(_issue())
        assert payload is not None
        assert payload["created"] == "2026-08-19T10:12:23.055030+00:00"

    def test_uuid_is_a_string(self) -> None:
        subject = uuid4()
        payload = tag_kind(_issue(subject_id=subject))
        assert payload is not None
        assert payload["id"] == str(subject)

    def test_nested_edges_are_serialized(self) -> None:
        # Edge tuples are the deep part of the payload -- a shallow
        # conversion leaves dataclass objects that json.dumps then rejects.
        peer = uuid4()
        issue = _issue(
            produces=(InquiryEdge(id=peer, kind="Experiment", note=None, labels=None),),
        )
        payload = tag_kind(issue)
        assert payload is not None
        assert payload["produces"] == [
            {"id": str(peer), "kind": "Experiment", "note": None, "labels": None},
        ]

    @pytest.mark.parametrize("subclass", _KINDS)
    def test_every_kind_matches_the_encoder(self, subclass: type[Inquiry]) -> None:
        # Each sub-kind adds its own fields; the fast path must not be
        # tuned to whichever kind the author happened to test.
        instance = _populated(subclass)
        payload = tag_kind(instance)
        expected = cast(dict[str, object], jsonable_encoder(instance))
        expected["kind"] = subclass.__name__
        assert payload == expected

    def test_unhandled_type_raises_here_not_downstream(self) -> None:
        # ``_jsonable`` dispatches on a closed set of types and used to fall
        # through to ``return value`` for anything else. A value it does not
        # convert survives into the payload and raises inside ``json.dumps``
        # while FastAPI is writing the response -- a 500 whose traceback names
        # the encoder, not the field. Failing at the seam names the type.
        experiment = _populated(Experiment)
        with pytest.raises(TypeError, match="set"):
            tag_kind(dataclasses.replace(experiment, config={"tags": {"a"}}))

    def test_unhandled_type_names_the_offending_value(self) -> None:
        experiment = _populated(Experiment)
        with pytest.raises(TypeError, match="bytes"):
            tag_kind(dataclasses.replace(experiment, config={"blob": b"\x00"}))

    def test_container_fields_are_non_empty(self) -> None:
        # An empty tuple serializes to ``[]`` whether or not the serializer
        # converts elements, so a fixture that leaves one empty passes the
        # oracle while exercising nothing.
        instance = _populated(Issue)
        assert instance.produces, "edge tuples must carry an element"
        assert instance.labels is not None
        assert instance.labels, "list-valued columns must carry an element"

    @pytest.mark.parametrize("subclass", _KINDS)
    def test_every_field_is_populated(self, subclass: type[Inquiry]) -> None:
        # The guard on the guard. An oracle that compares two all-``None``
        # payloads passes no matter what the serializer does, which is
        # exactly how the ``Decimal`` and ``dict`` arms of ``_jsonable``
        # shipped untested. Deriving the fixture from ``dataclasses.fields``
        # covers a newly added field of an already-sampled type with no edit
        # here; a NEW type raises in ``_sample`` instead, naming itself.
        instance = _populated(subclass)
        unset = [
            field.name
            for field in dataclasses.fields(instance)
            if getattr(instance, field.name) is None
        ]
        assert not unset, f"{subclass.__name__} left unpopulated: {unset}"


def test_replay_and_connection_dependencies_replace_invalid_state_then_reuse_it() -> (
    None
):
    app = FastAPI()
    request = _request(app)
    app.state.replays = "invalid"
    app.state.tool_relay = "invalid"
    replays = _deps.get_replays(request)
    connections = _deps.get_tool_relay(request)
    assert _deps.get_replays(request) is replays
    assert _deps.get_tool_relay(request) is connections
    other = _request(FastAPI())
    assert _deps.get_replays(other) is not replays
    assert _deps.get_tool_relay(other) is not connections


@pytest.mark.parametrize("configured", [False, True])
def test_terminal_dependency_retains_one_relay_with_the_configured_stun_servers(
    *,
    configured: bool,
) -> None:
    app = FastAPI()
    app.state.store, _engine = make_store()
    servers = ("stun:relay.example",) if configured else ()
    app.state.config = Config(terminal_stun=servers) if configured else "invalid"
    app.state.terminals = "invalid"
    relay = _deps.terminal_relay(app)
    assert relay.ice_servers == servers
    assert _deps.terminal_relay(app) is relay
    assert _deps.get_terminal_relay(_request(app)) is relay


def test_auth_and_staff_dependencies_require_a_real_config() -> None:
    app = FastAPI()
    request = _request(app)
    assert _deps.auth_is_disabled(request) is False
    assert _deps.get_staff(request) == ()
    app.state.config = "invalid"
    assert _deps.auth_is_disabled(request) is False
    assert _deps.get_staff(request) == ()
    app.state.config = Config()
    assert _deps.auth_is_disabled(request) is False
    assert _deps.get_staff(request) == ()
    staff = ("*@example.com",)
    app.state.config = Config(auth_disabled=True, staff=staff)
    assert _deps.auth_is_disabled(request) is True
    assert _deps.get_staff(request) == staff


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
