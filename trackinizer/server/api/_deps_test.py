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
    auth_is_disabled,
    get_assistant,
    get_chat_orgs,
    get_inbound,
    get_machine_changes,
    get_pipe_hub,
    get_relay,
    get_replays,
    get_secrets,
    get_staff,
    get_store,
    get_terminal_hub,
    get_tool_line_bytes,
    machine_changes,
    machine_closed,
    machine_idle_sec,
    machines_changed,
    nudge_streams,
    pipe_hub,
    tag_kind,
    tag_row,
    terminal_hub,
)
from trackinizer.server.api.tool_access import ToolAccess
from trackinizer.server.config import Assistant, Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.terminals import Terminal, TerminalHub
from trackinizer.server.tool_lines import ResultIds
from trackinizer.server.tool_pipes import Pipe, PipeBusyError, PipeHub
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
    from trackinizer.server.api.tool_access import Check
    from trackinizer.server.tool_pipes import PipeSocket


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


class _CountedStreams:
    def __init__(self) -> None:
        self.nudges = 0

    def nudge(self) -> None:
        self.nudges += 1


def test_nudge_streams_wakes_the_hub_and_skips_a_missing_one() -> None:
    app = FastAPI()
    nudge_streams(_request(app))
    app.state.hub = streams = _CountedStreams()

    nudge_streams(_request(app))

    assert streams.nudges == 1


def test_get_relay_makes_one_relay_and_keeps_it() -> None:
    app = FastAPI()

    relay = get_relay(_request(app))

    assert get_relay(_request(app)) is relay


def test_get_secrets_is_the_apps_backend_or_none() -> None:
    app = FastAPI()
    app.state.secrets = None
    assert get_secrets(_request(app)) is None


def test_get_store_and_get_inbound_are_the_apps() -> None:
    app = FastAPI()
    store, _engine = make_store(make_conn())
    inbound = InboundQueue()
    app.state.store = store
    app.state.inbound = inbound

    assert get_store(_request(app)) is store
    assert get_inbound(_request(app)) is inbound


def test_get_replays_makes_one_and_replaces_a_stranger() -> None:
    app = FastAPI()
    app.state.replays = "not replays"

    replays = get_replays(_request(app))

    assert get_replays(_request(app)) is replays


class TestTerminalHub:
    def test_one_hub_per_app_with_the_configs_stun(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store(make_conn())
        app.state.config = Config(terminal_stun=("stun:stun.example.com:3478",))

        hub = get_terminal_hub(_request(app))

        assert hub.ice_servers == ("stun:stun.example.com:3478",)
        assert terminal_hub(app) is hub

    def test_no_stun_without_a_config(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store(make_conn())

        assert terminal_hub(app).ice_servers == ()

    @pytest.mark.asyncio
    async def test_liveness_asks_the_apps_store(self) -> None:
        conn = make_conn()
        conn.fetchval.return_value = False
        app = FastAPI()
        app.state.store, _engine = make_store(conn)
        term = Terminal(
            id=uuid4(),
            user="ada@example.com",
            machine_id=uuid4(),
            machine="gx10",
            rows=24,
            cols=80,
            opened_at=0.0,
        )

        assert await terminal_hub(app)._still_alive(term) is False

        assert conn.fetchval.await_args is not None
        assert conn.fetchval.await_args.args[1] == term.machine_id


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
async def test_a_terminal_is_alive_while_its_machine_and_owner_are(
    *,
    live: bool,
) -> None:
    conn = make_conn()
    conn.fetchval.return_value = live
    store, _engine = make_store(conn)
    term = Terminal(
        id=uuid4(),
        user="ada@example.com",
        machine_id=uuid4(),
        machine="gx10",
        rows=24,
        cols=80,
        opened_at=0.0,
    )

    assert await _deps._alive(term, store=store) is live

    assert conn.fetchval.await_args is not None
    sql, machine_id = conn.fetchval.await_args.args
    assert machine_id == term.machine_id
    assert isinstance(sql, str)
    assert "c.revoked_at IS NULL" in sql
    assert "u.status = 'active'" in sql


def test_the_store_and_the_inbound_queue_are_the_apps() -> None:
    app = FastAPI()
    store, _engine = make_store()
    app.state.store = store
    app.state.inbound = inbound = object()

    assert get_store(_request(app)) is store
    assert get_inbound(_request(app)) is inbound


def test_replays_are_made_once_and_kept() -> None:
    app = FastAPI()

    replays = get_replays(_request(app))

    assert get_replays(_request(app)) is replays
    app.state.replays = "not replays"
    assert get_replays(_request(app)) is not replays


def test_machine_changes_are_made_once_and_a_write_bumps_its_owner() -> None:
    app = FastAPI()
    changes = machine_changes(app)
    assert machine_changes(app) is changes
    assert get_machine_changes(_request(app)) is changes
    before = changes.version("ada@example.com")

    machines_changed(_request(app), owner="ada@example.com")
    machines_changed(_request(app), owner=None)

    assert changes.version("ada@example.com") == before + 1


class TestTheTerminalHub:
    def test_it_is_made_once_and_kept(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store()

        hub = terminal_hub(app)

        assert terminal_hub(app) is hub
        assert get_terminal_hub(_request(app)) is hub

    def test_it_hands_out_the_configured_stun_servers_or_none(self) -> None:
        app = FastAPI()
        app.state.store, _engine = make_store()
        assert terminal_hub(app).ice_servers == ()

        other = FastAPI()
        other.state.store, _engine = make_store()
        other.state.config = Config(terminal_stun=("stun:stun.example.org:3478",))
        assert terminal_hub(other).ice_servers == ("stun:stun.example.org:3478",)


async def _pass_through(pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
    """Let every frame through to the host, and answer the agent nothing."""
    del pipe
    return frame, b""


def _pipe(*, instance: UUID | None = None) -> Pipe:
    return Pipe(
        id=uuid4(),
        machine_id=uuid4(),
        agent=cast("PipeSocket", AsyncMock()),
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
) -> tuple[FastAPI, AsyncMock]:
    """Return an app whose rule fails ``failed``, and the rule as asked."""
    app = FastAPI()
    app.state.store, _engine = make_store()
    app.state.config = Config(assistant=_KB)
    rule = AsyncMock(return_value=ToolAccess(machine_id=uuid4(), failed=failed))
    monkeypatch.setattr(_deps, "tool_access", rule)
    return app, rule


@pytest.mark.asyncio
async def test_the_pipe_hub_is_made_once_and_asks_if_a_pipe_still_qualifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rule = _app_with_assistant(monkeypatch)

    hub = pipe_hub(app)

    assert pipe_hub(app) is hub
    assert get_pipe_hub(_request(app)) is hub
    assert await hub._alive(_pipe()) is True
    rule.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_pipe_hub_holds_the_configs_pipes_and_dial_back() -> None:
    app = FastAPI()
    app.state.store, _engine = make_store()
    app.state.config = Config(tool_pipes_per_machine=1, tool_dial_back_sec=0.01)
    hub = pipe_hub(app)
    machine_id = uuid4()
    pipe = _join(hub, machine_id)

    with pytest.raises(PipeBusyError) as busy:
        _ = _join(hub, machine_id)
    never = asyncio.Future[bytes | None]()
    async with asyncio.timeout(5):
        await hub.serve_agent(pipe, lambda: never)

    assert busy.value.limit == 1
    assert pipe.ended.result()[1] == "the host did not dial back in time"


@pytest.mark.asyncio
async def test_the_pipe_hub_holds_the_default_pipes_without_a_config() -> None:
    app = FastAPI()
    app.state.store, _engine = make_store()
    hub = pipe_hub(app)
    machine_id = uuid4()
    for _ in range(Config().tool_pipes_per_machine):
        _ = _join(hub, machine_id)

    with pytest.raises(PipeBusyError):
        _ = _join(hub, machine_id)


@pytest.mark.asyncio
async def test_a_machines_idle_time_is_the_shorter_of_its_pipes_and_its_terminals() -> (
    None
):
    app = FastAPI()
    now = [100.0]
    pipes = app.state.tool_pipes = PipeHub(
        alive=AsyncMock(),
        pipes_per_machine=1,
        dial_back_sec=1,
        line_bytes=1 << 10,
        clock=lambda: now[0],
    )
    app.state.terminals = TerminalHub(alive=AsyncMock(), clock=lambda: now[0])
    machine_id = uuid4()
    now[0] += 40
    assert machine_idle_sec(app, machine_id) == 40

    pipe = _join(pipes, machine_id)
    pipes.called(pipe, "c1")
    now[0] += 3_600
    assert machine_idle_sec(app, machine_id) == 0
    pipes.acked(pipe, "c1")
    now[0] += 9

    assert machine_idle_sec(app, machine_id) == 9
    assert machine_idle_sec(app, uuid4()) == 3_649


@pytest.mark.asyncio
async def test_a_machine_closed_ends_its_pipes_then_its_terminals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    closed: list[tuple[str, UUID]] = []

    def pipes_closed(hub: PipeHub, machine_id: UUID) -> None:
        del hub
        closed.append(("pipes", machine_id))

    async def terminals_closed(hub: TerminalHub, machine_id: UUID) -> None:
        del hub
        closed.append(("terminals", machine_id))

    monkeypatch.setattr(PipeHub, "close_machine", pipes_closed)
    monkeypatch.setattr(TerminalHub, "close_machine", terminals_closed)
    app.state.tool_pipes = PipeHub(
        alive=AsyncMock(),
        pipes_per_machine=1,
        dial_back_sec=1,
        line_bytes=1 << 10,
    )
    app.state.terminals = TerminalHub(alive=AsyncMock())
    machine_id = uuid4()

    await machine_closed(app, machine_id)

    assert closed == [("pipes", machine_id), ("terminals", machine_id)]


def _join(hub: PipeHub, machine_id: UUID) -> Pipe:
    """Queue an agent that nothing will serve, to fill a slot."""
    return hub.join(
        machine_id,
        agent=cast("PipeSocket", AsyncMock()),
        asker="ada@example.com",
        api_key_id=uuid4(),
        inbound=_pass_through,
    )


@pytest.mark.asyncio
async def test_a_pipe_does_not_qualify_without_an_assistant() -> None:
    app = FastAPI()
    conn = make_conn()
    app.state.store, _engine = make_store(conn)
    assert await _deps._pipe_alive(_pipe(), app=app) is False
    app.state.config = Config()
    assert await _deps._pipe_alive(_pipe(), app=app) is False

    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_sweep_asks_the_rule_of_a_pipes_key_asker_machine_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rule = _app_with_assistant(monkeypatch)
    pipe = _pipe(instance=uuid4())

    assert await _deps._pipe_alive(pipe, app=app) is True

    assert rule.await_args is not None
    assert rule.await_args.kwargs == {
        "assistant": _KB,
        "api_key_id": pipe.api_key_id,
        "asker": pipe.asker,
        "machine": pipe.machine_id,
        "instance": pipe.instance,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [None, "key", "instance"])
async def test_a_pipe_no_longer_allowed_warns_once_naming_the_check_it_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed: Check | None,
) -> None:
    app, _ = _app_with_assistant(monkeypatch, failed)
    pipe = _pipe()

    assert await _deps._pipe_alive(pipe, app=app) is (failed is None)

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned == (
        []
        if failed is None
        else [
            (
                f"tool pipe no longer allowed: pipe={pipe.id} "
                f"machine_id={pipe.machine_id} check={failed}"
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


def test_state_dependencies_return_the_apps_store_and_inbound_queue() -> None:
    app = FastAPI()
    store, _engine = make_store()
    inbound = InboundQueue()
    app.state.store = store
    app.state.inbound = inbound
    request = _request(app)
    assert _deps.get_store(request) is store
    assert _deps.get_inbound(request) is inbound


def test_replay_and_pipe_dependencies_replace_invalid_state_then_reuse_it() -> None:
    app = FastAPI()
    request = _request(app)
    app.state.replays = "invalid"
    app.state.tool_pipes = "invalid"
    replays = _deps.get_replays(request)
    pipes = _deps.get_pipe_hub(request)
    assert _deps.get_replays(request) is replays
    assert _deps.get_pipe_hub(request) is pipes
    other = _request(FastAPI())
    assert _deps.get_replays(other) is not replays
    assert _deps.get_pipe_hub(other) is not pipes


@pytest.mark.parametrize("configured", [False, True])
def test_terminal_dependency_retains_one_hub_with_the_configured_stun_servers(
    *,
    configured: bool,
) -> None:
    app = FastAPI()
    app.state.store, _engine = make_store()
    servers = ("stun:relay.example",) if configured else ()
    app.state.config = Config(terminal_stun=servers) if configured else "invalid"
    app.state.terminals = "invalid"
    hub = _deps.terminal_hub(app)
    assert hub.ice_servers == servers
    assert _deps.terminal_hub(app) is hub
    assert _deps.get_terminal_hub(_request(app)) is hub


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
