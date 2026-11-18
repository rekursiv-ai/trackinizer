"""One ask's whole path, from the agent that asks to the person who answers in Chat.

Each test runs a chat's headless run (:class:`~trackinizer.trax.run.headless.
HeadlessRun`) on a scripted driver against the whole app over PGlite. The run's records
reach the chat's session in a file of its own, as its sink sends them, and the states it
reports reach ``POST .../run-state``. The person answers through ``POST /api/chats``,
and the run drains the answer and hands it on as ``trax run`` does
(``session._deliver_headless``). The assistant's card is answered the same way and read
as the assistant reads its drain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, cast

import asyncio
import tempfile
import threading
import uuid

from treekle import PlainTree, from_plain, json_loads

import httpx2
import pytest

from trackinizer.lib.agent.types.sessions import SystemMessage
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    TEST_USER_EMAIL,
    USER_AGENT,
    act_as_assistant,
    act_as_user_agent,
    browser,
    conversation_of,
    drain,
    open_workspace,
    seed_accounts,
    seed_science_chat,
    seed_session,
    send_chat,
)
from trackinizer.server.asks import open_ask_ids, release_gone_runs
from trackinizer.server.asks_test_support import write_records
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run import session
from trackinizer.trax.run.asks import (
    Answer,
    Ask,
    Option,
    Resolved,
    StateChanged,
    answer_from_payload,
    opened_record,
)
from trackinizer.trax.run.headless import HeadlessRun, HostState, request
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id
from trackinizer.wire.wire_sessions import (
    RUN_ENDING_REFUSALS,
    InboundDrainItem,
    session_run_state_path,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from trackinizer.trax.run.asks import Driver


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_WAIT: Final = 5.0

_COMMAND: Final = Ask(
    id="0a1b2c3d-0",
    provider="codex",
    kind="command",
    title="Run a command",
    tool="shell",
    tool_call_id="exec-1",
    command="touch ../outside/x",
    cwd="/work",
    options=(
        Option(id="accept", label="Yes", kind="allow_once"),
        Option(id="decline", label="No", kind="deny"),
        Option(id="cancel", label="No, and stop", kind="deny_stop"),
    ),
)

_PLAN: Final = Ask(
    id="req-plan-1",
    provider="claude",
    kind="plan",
    title="Claude asks to approve its plan",
    tool="ExitPlanMode",
    tool_call_id="toolu_1",
    plan="1. Read the tests.\n2. Fix the failing one.",
    options=(
        Option(id="allow_once", label="Approve plan", kind="allow_once"),
        Option(id="deny", label="Keep planning", kind="deny"),
    ),
)


@pytest.fixture
def served(pglite_route_client: _Client, monkeypatch: pytest.MonkeyPatch) -> _Served:
    """Serve with the assistant configured and a queue whose leases age by hand."""
    clock = _Clock()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(
        app.state,
        "inbound",
        InboundQueue(_clock=clock.now),
        raising=False,
    )
    client, store = pglite_route_client
    return _Served(client=client, store=store, clock=clock)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Served:
    """The app's client and store, and the clock of its queue's leases."""

    client: httpx2.AsyncClient
    store: Store
    clock: _Clock


@dataclass(slots=True, kw_only=True)
class _Clock:
    """A monotonic clock a test moves by hand."""

    at: float = 1_000.0

    def now(self) -> float:
        return self.at


class _Driver:
    """A CLI that asks what it is pushed, and ends an ask with the answer it gets."""

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self.resumed: list[str] = []
        self.answers: list[Answer] = []
        self.lines: list[str] = []
        self._open: dict[str, Ask] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue = asyncio.Queue[Ask | Resolved | StateChanged | None]()

    def ask(self, ask: Ask) -> None:
        """Open ``ask`` from the test's thread, as the CLI asks it mid-turn."""
        loop = self._loop
        assert loop is not None
        loop.call_soon_threadsafe(self._opened, ask)

    def _opened(self, ask: Ask) -> None:
        self._open[ask.id] = ask
        self._queue.put_nowait(ask)
        self._queue.put_nowait(StateChanged(state="requires_action"))

    async def start(self, *, cwd: Path, resume: str, model: str, effort: str) -> str:
        del cwd, model, effort
        self._loop = asyncio.get_running_loop()
        self.resumed.append(resume)
        return resume or self.session_id

    async def send_line(self, text: str) -> None:
        self.lines.append(text)
        await self._queue.put(StateChanged(state="running"))

    async def command(self, verb: str, args: str) -> None:
        del verb, args

    async def answer(self, answer: Answer) -> None:
        ask = self._open.pop(answer.ask_id, None)
        if ask is None:
            return
        self.answers.append(answer)
        await self._queue.put(ask.resolve(answer.option, by="chat", who=answer.who))
        await self._queue.put(StateChanged(state="running"))

    async def interrupt(self) -> None:
        return

    async def settings(self, *, model: str, effort: str) -> None:
        del model, effort

    async def stop(self) -> None:
        for ask in self._open.values():
            await self._queue.put(ask.resolve("", by="released"))
        self._open.clear()
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[Ask | Resolved | StateChanged]:
        while (event := await self._queue.get()) is not None:
            yield event


class _Tui:
    """The CLI's own terminal a takeover opens: it listens, then runs until ended."""

    def __init__(self, *, listening: Callable[[], None]) -> None:
        self._listening = listening
        self.ended = threading.Event()

    def run(self) -> int:
        self._listening()
        _ = self.ended.wait(_WAIT)
        return 0

    def terminate(self) -> None:
        self.ended.set()

    def submit(self, text: str) -> None:
        del text


@dataclass(frozen=True, slots=True, kw_only=True)
class _Chat:
    """A station on a CLI, and the chat's session its run attaches to."""

    station: uuid.UUID
    session: uuid.UUID


@dataclass(slots=True, kw_only=True)
class _Run:
    """A chat's headless run in a thread, wired to the server as ``trax run`` wires it.

    ``upload`` sends what the run wrote since the last upload, records and states;
    ``deliver`` drains the chat's queue as the run and hands it each message.
    """

    cli: str
    chat: _Chat
    session_id: str = "cli-session-1"
    resume: str = ""
    control: Path | None = None
    instance: str = field(default_factory=lambda: uuid.uuid4().hex)
    driver: _Driver = field(init=False)
    run: HeadlessRun = field(init=False)
    records: list[SystemMessage] = field(default_factory=list[SystemMessage])
    states: list[HostState] = field(default_factory=list[HostState])
    tuis: list[_Tui] = field(default_factory=list[_Tui])
    started: threading.Event = field(default_factory=threading.Event)
    _sent: int = 0
    _reported: int = 0
    _thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        self.driver = _Driver(session_id=self.session_id)
        self.run = HeadlessRun(
            cli=self.cli,
            cwd=Path("/work"),
            resume=self.resume,
            model="",
            effort="",
            make_driver=lambda: cast("Driver", self.driver),
            make_tui=self._tui,
            record=self.records.append,
            on_session=lambda _: None,
            on_state=self.states.append,
            control=self.control,
        )
        self._thread = threading.Thread(
            target=lambda: self.run.run(on_started=self.started.set),
            daemon=True,
        )

    def __enter__(self) -> Self:
        self._thread.start()
        assert self.started.wait(_WAIT), "the run did not start"
        return self

    def __exit__(self, *_: object) -> None:
        self.run.terminate()
        self._thread.join(_WAIT)
        assert not self._thread.is_alive(), "the run did not end"

    def ended(self) -> bool:
        """Wait for the run to end; say whether it did."""
        self._thread.join(_WAIT)
        return not self._thread.is_alive()

    def _tui(self, *, session_id: str, listening: Callable[[], None]) -> _Tui:
        assert session_id == self.session_id
        tui = _Tui(listening=listening)
        self.tuis.append(tui)
        return tui

    async def upload(
        self,
        client: httpx2.AsyncClient,
        store: Store,
        *,
        records: int,
    ) -> None:
        """Send the run's records up to the ``records``th, and every state it reported."""
        for _ in range(int(_WAIT / 0.005)):
            if len(self.records) >= records:
                break
            await asyncio.sleep(0.005)
        assert len(self.records) >= records, self.records
        if records > self._sent:
            await write_records(
                store,
                self.chat.session,
                self.records[self._sent : records],
                name="run.jsonl",
            )
            self._sent = records
        act_as_user_agent("writer")
        for state in self.states[self._reported :]:
            reported = await client.post(
                session_run_state_path(self.chat.session),
                json={"state": state, "instance": self.instance},
            )
            assert reported.status_code == 204, reported.text
        self._reported = len(self.states)

    async def drain(self, client: httpx2.AsyncClient) -> httpx2.Response:
        """Drain the chat's queue as this run: its CLI and its own id."""
        act_as_user_agent("writer")
        return await drain(
            client,
            session_id=self.chat.session,
            cli=self.cli,
            instance=self.instance,
        )

    async def deliver(self, client: httpx2.AsyncClient) -> list[InboundDrainItem]:
        """Drain the chat's queue as this run, and hand the run each message."""
        drained = await self.drain(client)
        assert drained.status_code == 200, drained.text
        items = _items(drained)
        for item in items:
            await asyncio.to_thread(
                session._deliver_headless,
                self.run,
                item,
                inbound=None,
                record=None,
            )
        return items


def _items(drained: httpx2.Response) -> list[InboundDrainItem]:
    """Return the messages a drain handed out, as ``trax run`` reads them."""
    messages = json_loads(drained.content, dict[str, list[dict[str, object]]])
    return [InboundDrainItem.model_validate(each) for each in messages["messages"]]


async def _cli_chat(client: httpx2.AsyncClient, store: Store, *, cli: str) -> _Chat:
    """Put the person's station on ``cli``, with a chat whose run's session is open."""
    await seed_accounts(store)
    station = await open_workspace(client)
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET state = state || jsonb_build_object("
            "'provider', $2::text), station = station || jsonb_build_object("
            "'conversation_id', $3::text) WHERE id = $1",
            station,
            cli,
            str(conversation),
        )
    chat = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli=cli,
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    return _Chat(station=station, session=chat)


async def _post(
    client: httpx2.AsyncClient,
    chat: _Chat,
    *,
    text: str,
    kind: str = "line",
    payload: dict[str, object] | None = None,
) -> httpx2.Response:
    """Post to the station's chat from the person's browser."""
    browser()
    return await client.post(
        "/api/chats",
        json={
            "workspace_id": str(chat.station),
            "text": text,
            "kind": kind,
            "payload": payload,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


async def _partner_state(client: httpx2.AsyncClient, chat: _Chat) -> object:
    """Return the run state the person's station shows for its chat."""
    browser()
    response = await client.get(f"/api/workspaces/{chat.station}")
    assert response.status_code == 200, response.text
    partner = from_plain(
        json_loads(response.content, dict[str, PlainTree])["partner"],
        dict[str, PlainTree],
    )
    return partner["state"]


async def _open(store: Store, chat: _Chat) -> list[str]:
    async with store.engine.acquire() as conn:
        return await open_ask_ids(conn, session_id=chat.session)


def _ends(records: list[SystemMessage]) -> list[dict[str, PlainTree]]:
    """Return how the run's records end its asks, in order."""
    return [
        from_plain(record.extra["$interaction_resolved"], dict[str, PlainTree])
        for record in records
        if "$interaction_resolved" in record.extra
    ]


@pytest.mark.parametrize(
    ("cli", "ask", "option", "decision"),
    [("codex", _COMMAND, "accept", "allow"), ("claude", _PLAN, "deny", "deny")],
    ids=["a codex command", "a claude plan"],
)
async def test_an_ask_is_a_card_answered_in_chat_by_option_and_the_card_closes(
    served: _Served,
    cli: str,
    ask: Ask,
    option: str,
    decision: str,
) -> None:
    """The run's ask is the chat's card; the option the person takes reaches the CLI."""
    client, store = served.client, served.store
    chat = await _cli_chat(client, store, cli=cli)
    with _Run(cli=cli, chat=chat) as run:
        assert (await run.deliver(client)) == []
        run.driver.ask(ask)
        await run.upload(client, store, records=1)

        assert await _open(store, chat) == [ask.id]
        assert await _partner_state(client, chat) == "requires_action"
        answered = await _post(
            client,
            chat,
            text="Yes",
            kind="answer",
            payload={"prompt_id": ask.id, "option": option},
        )
        assert answered.status_code == 200, answered.text
        [item] = await run.deliver(client)
        await run.upload(client, store, records=2)

    assert item.kind == "answer"
    [heard] = run.driver.answers
    assert (heard.ask_id, heard.option, heard.who) == (ask.id, option, TEST_USER_EMAIL)
    assert _ends(run.records) == [
        {
            "id": ask.id,
            "option": option,
            "by": "chat",
            "who": TEST_USER_EMAIL,
            "decision": decision,
        },
    ]
    assert await _open(store, chat) == []
    assert await _partner_state(client, chat) == "running"


async def test_the_assistants_question_is_answered_by_the_person_it_asked(
    served: _Served,
) -> None:
    """The pick reaches the assistant's drain as that person's, with their canvas."""
    client, store = served.client, served.store
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    station = await open_workspace(client)
    browser()
    conversation = conversation_of(
        await send_chat(client, workspace_id=station, text="open a terminal"),
    )
    chat = await seed_science_chat(store, conversation_id=conversation)
    question = Ask(
        id="kb-1",
        provider="assistant",
        kind="question",
        title="Which machine?",
        tool="AskUserQuestion",
        input={"asker": TEST_USER_EMAIL},
        options=(
            Option(id="1", label="gx10", kind="allow_once"),
            Option(id="2", label="laptop", kind="allow_once"),
        ),
    )
    await write_records(
        store,
        chat,
        [opened_record(question, at=datetime.now(UTC))],
        name="cards.jsonl",
    )
    browser()
    answered = await client.post(
        "/api/chats",
        json={
            "workspace_id": str(station),
            "conversation_id": str(conversation),
            "text": "laptop",
            "kind": "answer",
            "payload": {"prompt_id": "kb-1", "option": "2"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    act_as_assistant()
    drained = await drain(client, session_id=chat)

    assert answered.status_code == 200, answered.text
    [item] = _items(drained)
    assert (item.kind, item.text, item.source) == ("answer", "laptop", TEST_USER_EMAIL)
    assert item.context is not None
    assert item.context.workspace_id == station
    heard = answer_from_payload(from_plain(item.payload, dict[str, PlainTree]))
    assert (heard.ask_id, heard.option) == ("kb-1", "2")
    assert question.chosen(heard.option) == question.options[1]


async def test_a_run_that_dies_with_a_card_open_has_it_released(
    served: _Served,
) -> None:
    """Nobody can answer a dead run's card, so the server ends it once the lease ends."""
    client, store = served.client, served.store
    chat = await _cli_chat(client, store, cli="codex")
    run = _Run(cli="codex", chat=chat)
    with run:
        assert (await run.deliver(client)) == []
        run.driver.ask(_COMMAND)
        await run.upload(client, store, records=1)
    # The run is gone without a word: what its driver released on the way out never
    # reached the server, and no drain renews its lease.
    inbound = cast("InboundQueue", app.state.inbound)
    assert await release_gone_runs(store, inbound=inbound, at=datetime.now(UTC)) == 0
    served.clock.at += inbound.poller_ttl_sec + 1

    released = await release_gone_runs(store, inbound=inbound, at=datetime.now(UTC))

    assert released == 1
    assert await _open(store, chat) == []


async def test_a_takeover_refuses_chat_lines_and_a_reattach_resumes_the_session(
    served: _Served,
) -> None:
    """The terminal holds the CLI until the chat is reattached on the same session."""
    client, store = served.client, served.store
    chat = await _cli_chat(client, store, cli="claude")
    control = socket_address(Path(tempfile.mkdtemp()) / "c.sock")
    with _Run(cli="claude", chat=chat, control=control) as old:
        assert (await old.deliver(client)) == []
        taken = await asyncio.to_thread(request, control, "takeover")
        await old.upload(client, store, records=0)
        refused = await _post(client, chat, text="still there?")
        # The reattach route turns the old run away (``InboundQueue.retire_run``)
        # once it has checked the machine; the route itself is tested with a host.
        cast("InboundQueue", app.state.inbound).retire_run(chat.session)
        turned_away = await old.drain(client)
        code = from_plain(
            json_loads(turned_away.content, dict[str, PlainTree])["detail"],
            dict[str, PlainTree],
        )["code"]
        assert code in RUN_ENDING_REFUSALS
        old.run.terminate()
        assert old.ended()
    with _Run(cli="claude", chat=chat, resume=old.session_id) as new:
        assert (await new.deliver(client)) == []
        said = await _post(client, chat, text="back in Chat")
        [line] = await new.deliver(client)

    assert (taken.ok, taken.state, taken.session) == (
        True,
        "taken_over",
        "cli-session-1",
    )
    assert old.states == ["taken_over"]
    assert refused.status_code == 409
    assert json_loads(refused.content, dict[str, PlainTree])["detail"] == {
        "code": "chat_taken_over",
        "message": "taken over in a terminal; reattach from Chat or Console",
    }
    assert (turned_away.status_code, code) == (409, "chat_run_live")
    assert old.tuis[0].ended.is_set()
    assert new.driver.resumed == ["cli-session-1"]
    assert new.run.session_id == "cli-session-1"
    assert said.status_code == 200, said.text
    assert line.text == "back in Chat"
    [typed] = new.driver.lines
    assert "back in Chat" in typed


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
