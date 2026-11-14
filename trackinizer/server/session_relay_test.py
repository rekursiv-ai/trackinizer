"""The session relay, rule by rule: a scripted process socket, scripted viewers, no server."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final, override
from uuid import UUID

import asyncio
import logging

import pydantic
import pytest

from trackinizer.server import session_relay
from trackinizer.server.session_relay import (
    BAD_FRAME,
    Allowed,
    HostRevokedError,
    InputRefusedError,
    NotLiveError,
    SessionRelay,
    process_frame,
)
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_session_ir import RecordBody
from trackinizer.wire.wire_session_relay import (
    MAX_FRAME_BYTES,
    AgentProcess,
    AgentState,
    EventLine,
    Hello,
    Input,
    NewInput,
    Opened,
    ProcessEnded,
    ProcessLine,
    ProcessSpec,
    RecordLine,
    Setting,
    SettingsLine,
    StateLine,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.wire.wire_session_relay import ViewerFrame


pytestmark = pytest.mark.asyncio

_SESSION: Final = UUID(int=1)
_PROCESS: Final = UUID(int=2)
_OTHER: Final = UUID(int=3)
_HOST: Final = UUID(int=4)
_LAPTOP: Final = UUID(int=5)
_ENDED: Final = UUID(int=6)
_SINCE: Final = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_STATE: Final = StateLine(state=AgentState(line="[3s]", busy=True, since=_SINCE))
_SETTINGS: Final = SettingsLine(settings=(Setting(name=("model",), help_short="M"),))
_HALT: Final = NewInput(kind="Halt", payload={"py/object": "Halt"})
_AUTHOR: Final = "ada@x.y"


class _Socket:
    """A process socket the test scripts: what it reads pushed in, what it was sent read out."""

    def __init__(self) -> None:
        self._received: asyncio.Queue[str | bytes | int] = asyncio.Queue()
        self._sent: asyncio.Queue[str] = asyncio.Queue()
        self.closed: asyncio.Future[tuple[int, str]] = (
            asyncio.get_running_loop().create_future()
        )

    def push(self, frame: pydantic.BaseModel | str | bytes | int) -> None:
        """Have the process send ``frame``, or close with it when it is a code."""
        self._received.put_nowait(
            frame.model_dump_json() if isinstance(frame, pydantic.BaseModel) else frame,
        )

    async def receive(self) -> str | bytes | int:
        return await self._received.get()

    async def send_text(self, text: str) -> None:
        if self.closed.done():
            raise ConnectionError
        self._sent.put_nowait(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed.done():
            self.closed.set_result((code, reason))

    async def input(self) -> Input:
        """Return the next input the relay sent the process."""
        return Input.model_validate_json(await asyncio.wait_for(self._sent.get(), 1))

    def sent(self) -> int:
        """Return how many inputs wait unread."""
        return self._sent.qsize()

    async def close_code(self) -> int:
        """Return the code the relay closed this socket with."""
        code, _ = await asyncio.wait_for(asyncio.shield(self.closed), 1)
        return code


async def _always() -> bool:
    """Answer yes: a rule that always holds."""
    return True


async def _here() -> bool:
    """Answer no: a wait's caller that is still there."""
    return False


class _Viewer:
    """One viewer's stream, read a frame at a time."""

    def __init__(
        self,
        relay: SessionRelay,
        session_id: UUID = _SESSION,
        *,
        allowed: Allowed = _always,
    ) -> None:
        self._frames: AsyncIterator[ViewerFrame] = relay.lines(
            session_id,
            allowed=allowed,
        )

    async def next(self) -> ViewerFrame:
        return await asyncio.wait_for(anext(self._frames), 1)

    async def process(self) -> AgentProcess:
        """Return the next frame, which must be a process change."""
        frame = await self.next()
        assert isinstance(frame, ProcessLine), frame
        return frame.process

    async def ended(self) -> bool:
        """Say whether the stream ends next."""
        try:
            _ = await self.next()
        except StopAsyncIteration:
            return True
        return False


def _hello(
    process: UUID = _PROCESS,
    *,
    host: str = "laptop",
    version: str = "1.2",
) -> Hello:
    return Hello(process_id=process, version=version, host=host)


def _serve(
    relay: SessionRelay,
    socket: _Socket,
    *,
    session_id: UUID = _SESSION,
    process: UUID = _PROCESS,
    host: str = "laptop",
    version: str = "1.2",
    host_id: UUID | None = None,
    allowed: Allowed = _always,
    key: UUID | None = None,
) -> asyncio.Future[None]:
    """Run the process socket's route, as the server runs it, in a task of its own."""
    return asyncio.ensure_future(
        relay.serve_process(
            session_id,
            socket,
            _hello(process, host=host, version=version),
            host_id=host_id,
            allowed=allowed,
            key=key,
        ),
    )


def _start(
    relay: SessionRelay,
    *,
    session_id: UUID = _SESSION,
    process: UUID = _PROCESS,
    spec: ProcessSpec | None = None,
    host_id: UUID = _HOST,
    remote: bool = False,
) -> AgentProcess:
    return relay.start(
        session_id,
        ProcessSpec(login_host="laptop") if spec is None else spec,
        process_id=process,
        host_id=host_id,
        host="lab-box",
        token=f"key-{process.hex}",
        remote=remote,
    )


def _event(kind: str = "ModelResponsePartial", text: str = "hi") -> EventLine:
    return EventLine(kind=kind, payload={"text": text})


async def _attached(
    relay: SessionRelay,
    viewer: _Viewer,
    socket: _Socket,
    *,
    allowed: Allowed = _always,
) -> asyncio.Future[None]:
    """Serve ``socket`` and return once the viewer is told it attached."""
    served = _serve(relay, socket, allowed=allowed)
    assert (await viewer.process()).state == "attached"
    return served


async def test_a_started_process_attaches_on_the_hosts_the_relay_holds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = _start(relay)
    viewer = _Viewer(relay)
    _ = await viewer.next()

    _ = _serve(relay, _Socket(), host="somewhere-else", host_id=_LAPTOP)
    process = await viewer.process()

    assert (process.process_id, process.host, process.login_host) == (
        _PROCESS,
        "lab-box",
        "laptop",
    )
    assert (process.state, process.reason, process.version) == ("attached", "", "1.2")
    assert (
        f"process attached: session={_SESSION} process={_PROCESS} host=lab-box"
        in _said(caplog)
    )
    # The host is the start's, not the Hello's: a revoke of the Hello's host stops nothing.
    assert await relay.stop_host(_LAPTOP) == []
    assert relay.process(_SESSION) == process


async def test_a_process_whose_socket_dropped_attaches_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tick(monkeypatch)
    relay = SessionRelay()
    _ = _start(relay)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)

    socket.push(1006)
    await served
    dropped = await viewer.process()
    _ = await _attached(relay, viewer, _Socket())

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.process_id, process.host, process.state) == (
        _PROCESS,
        "lab-box",
        "attached",
    )
    # Attached again from now, with no reason left of its drop.
    assert (dropped.reason, process.reason) == ("disconnected", "")
    assert process.since > dropped.since


async def test_a_process_nobody_started_here_is_one_a_person_started() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    assert await viewer.next() == Opened()

    _ = _serve(relay, _Socket(), process=_OTHER, host="laptop", host_id=_LAPTOP)
    process = await viewer.process()

    assert (process.process_id, process.host, process.login_host, process.state) == (
        _OTHER,
        "laptop",
        "",
        "attached",
    )
    assert process.version == "1.2"
    # It is known by the host the route found for it.
    await relay.stop_host(_LAPTOP)
    assert (await viewer.process()).reason == "host revoked"


async def test_a_hello_from_a_process_that_was_stopped_is_told_it_ended() -> None:
    relay = SessionRelay()
    _ = _start(relay)
    _ = await relay.stop(_SESSION)

    socket = _Socket()
    await _serve(relay, socket)

    assert await socket.close_code() == wire_terminals.CLOSE_ENDED
    process = relay.process(_SESSION)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "stopped")


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        b"{}",
        '{"type": "record", "part": 0, "record": {"idx": 0, "kind": "X"}}',
        _hello().model_dump_json(),
        EventLine(kind="X", payload={"text": "x" * MAX_FRAME_BYTES}).model_dump_json(),
    ],
    ids=[
        "not JSON",
        "binary",
        "a line only the server writes",
        "a second Hello",
        "too long",
    ],
)
async def test_a_frame_the_wire_does_not_allow_closes_the_socket_as_a_bad_one(
    caplog: pytest.LogCaptureFixture,
    frame: str | bytes,
) -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)

    socket.push(frame)
    await served

    assert socket.closed.result() == (
        BAD_FRAME,
        "the server reads one JSON line a frame",
    )
    assert (await viewer.process()).reason == "disconnected"
    assert f"process sent a bad frame: session={_SESSION} bytes={len(frame)}" in [
        record.getMessage() for record in caplog.records
    ]


@pytest.mark.parametrize(
    ("received", "frame"),
    [
        (_STATE.model_dump_json(), _STATE),
        (1000, None),
        (b"{}", None),
        ("x" * (MAX_FRAME_BYTES + 1), None),
        ('{"type": "hello"}', None),
    ],
)
async def test_a_frame_is_read_whole_or_not_at_all(
    received: str | bytes | int,
    frame: StateLine | None,
) -> None:
    assert process_frame(received) == frame


async def test_another_process_is_refused_while_one_starts_or_serves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = _start(relay)
    starting = _Socket()
    await _serve(relay, starting, process=_OTHER)
    assert await starting.close_code() == wire_terminals.CLOSE_TAKEN_OVER
    assert (
        f"process refused: session={_SESSION} process={_OTHER} "
        f"code={wire_terminals.CLOSE_TAKEN_OVER}"
    ) in _said(caplog)

    viewer = _Viewer(relay)
    _ = await viewer.next()
    _ = await _attached(relay, viewer, _Socket())
    attached = _Socket()
    await _serve(relay, attached, process=_OTHER)

    assert await attached.close_code() == wire_terminals.CLOSE_TAKEN_OVER
    process = relay.process(_SESSION)
    assert process is not None
    assert (process.process_id, process.state) == (_PROCESS, "attached")


async def test_what_a_socket_does_while_attached_runs_once_taken_and_ends_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused socket starts none of it; a failure is logged, the socket served on."""
    relay = SessionRelay()
    _ = _start(relay)
    ran: list[str] = []
    ended = asyncio.Event()

    async def attached(name: str, *, fails: bool = False) -> None:
        ran.append(name)
        if fails:
            raise RuntimeError(name)
        try:
            _ = await asyncio.Event().wait()
        finally:
            ended.set()

    refused = _Socket()
    await relay.serve_process(
        _SESSION,
        refused,
        _hello(_OTHER),
        host_id=None,
        allowed=_always,
        attached=partial(attached, "refused"),
    )
    viewer = _Viewer(relay)
    _ = await viewer.next()
    taken = _Socket()
    serving = asyncio.ensure_future(
        relay.serve_process(
            _SESSION,
            taken,
            _hello(),
            host_id=None,
            allowed=_always,
            attached=partial(attached, "taken"),
        ),
    )
    assert (await viewer.process()).state == "attached"
    await asyncio.sleep(0)
    taken.push(1000)
    await asyncio.wait_for(serving, 1)
    failing = _Socket()
    serving = asyncio.ensure_future(
        relay.serve_process(
            _SESSION,
            failing,
            _hello(_OTHER),
            host_id=None,
            allowed=_always,
            attached=partial(attached, "failing", fails=True),
        ),
    )
    assert (await viewer.process()).state == "detached"
    assert (await viewer.process()).state == "attached"
    failing.push(_event().model_dump_json())
    assert isinstance(await viewer.next(), EventLine)
    failing.push(1000)
    await asyncio.wait_for(serving, 1)

    assert await refused.close_code() == wire_terminals.CLOSE_TAKEN_OVER
    assert ran == ["taken", "failing"]
    assert ended.is_set()
    [failed] = [
        record
        for record in caplog.records
        if record.getMessage() == f"work while attached failed: session={_SESSION}"
    ]
    assert isinstance(failed.exc_info, tuple)
    assert repr(failed.exc_info[1]) == repr(RuntimeError("failing"))


async def test_the_same_process_again_replaces_its_old_socket_closing_it_without_a_code() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    old = _Socket()
    first = await _attached(relay, viewer, old)

    new = _Socket()
    _ = _serve(relay, new, version="1.3")
    await first

    assert await old.close_code() == 1000
    assert relay.send_input(_SESSION, _HALT, author="ada@x.y") == _HALT.id
    assert (await new.input()).id == _HALT.id
    process = relay.process(_SESSION)
    assert process is not None
    # Its client may have been upgraded meanwhile: the new socket's Hello says so.
    assert (process.state, process.version) == ("attached", "1.3")


async def test_once_the_process_is_detached_the_next_to_attach_holds_the_session() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    socket.push(1000)
    await served
    _ = await viewer.process()

    _ = _serve(relay, _Socket(), process=_OTHER)
    process = await viewer.process()

    assert (process.process_id, process.state) == (_OTHER, "attached")


async def test_each_line_reaches_every_viewer_as_it_arrives_unread() -> None:
    relay = SessionRelay()
    first, second = _Viewer(relay), _Viewer(relay)
    _ = await first.next(), await second.next()
    socket = _Socket()
    _ = await _attached(relay, first, socket)
    _ = await second.process()
    # A payload of any JSON shape passes as it came: the relay reads none of it.
    event = EventLine(
        kind="Unknown",
        payload={"py/object": "X", "nested": [1, {"a": None}]},
    )

    for line in (event, _STATE, _SETTINGS):
        socket.push(line)
        assert await first.next() == line
        assert await second.next() == line


async def test_the_last_state_and_settings_are_kept_for_the_next_viewer() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    later = StateLine(state=AgentState(line="[4s]", busy=False, since=_SINCE))
    for line in (_STATE, _SETTINGS, later):
        socket.push(line)
        _ = await viewer.next()

    opened = await _Viewer(relay).next()

    assert isinstance(opened, Opened)
    assert (opened.state, opened.settings) == (later.state, _SETTINGS.settings)
    assert opened.process == relay.process(_SESSION)


async def test_stored_records_reach_viewers_and_are_not_kept() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    records = [RecordBody(idx=4, kind="UserMessage"), RecordBody(idx=5, kind="X")]

    relay.publish_records(_SESSION, part=2, records=records)

    assert [await viewer.next(), await viewer.next()] == [
        RecordLine(part=2, record=record) for record in records
    ]
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    socket.push(_event("ModelCallStarted"))
    _ = await viewer.next()
    late = _Viewer(relay)
    assert isinstance(await late.next(), Opened)
    assert await late.next() == _event("ModelCallStarted")


async def test_a_session_is_watched_while_a_viewer_follows_it() -> None:
    relay = SessionRelay()
    assert not relay.watched(_SESSION)
    _ = _start(relay)
    assert not relay.watched(_SESSION)

    viewer = _Viewer(relay)
    _ = await viewer.next()

    assert relay.watched(_SESSION)


async def test_records_for_a_session_nobody_watches_go_nowhere() -> None:
    relay = SessionRelay()

    relay.publish_records(_SESSION, part=0, records=[RecordBody(idx=0, kind="X")])

    assert relay.process(_SESSION) is None


async def test_a_late_viewer_gets_the_reply_since_its_model_call_then_live_lines() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    sent = [
        _event("ModelResponsePartial", "before any call"),
        _event("ModelCallStarted", "first"),
        _event("ModelResponsePartial", "of the first call"),
        _event("ModelCallStarted", "second"),
        _event("ModelResponsePartial", "a"),
        _STATE,
        _event("ModelResponsePartial", "b"),
    ]
    for line in sent:
        socket.push(line)
        _ = await viewer.next()

    late = _Viewer(relay)
    opened = await late.next()
    kept = [await late.next() for _ in range(3)]
    socket.push(_event("ModelResponsePartial", "c"))

    assert isinstance(opened, Opened)
    assert opened.state == _STATE.state
    assert kept == [sent[3], sent[4], sent[6]]
    assert await late.next() == _event("ModelResponsePartial", "c")


async def test_every_connect_starts_again_with_no_cursor() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    for line in (_event("ModelCallStarted"), _event()):
        socket.push(line)
        _ = await viewer.next()

    for _ in range(2):
        again = _Viewer(relay)
        assert isinstance(await again.next(), Opened)
        assert [await again.next(), await again.next()] == [
            _event("ModelCallStarted"),
            _event(),
        ]


async def test_past_its_budget_the_reply_is_kept_no_more_until_the_next_call() -> None:
    call = _event("ModelCallStarted")
    budget = len(call.model_dump_json().encode()) + len(
        _event().model_dump_json().encode(),
    )
    relay = SessionRelay(kept_bytes=budget)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)

    async def kept_after(*lines: EventLine) -> list[ViewerFrame]:
        for line in lines:
            socket.push(line)
            _ = await viewer.next()
        late = _Viewer(relay)
        _ = await late.next()
        socket.push(_STATE)
        _ = await viewer.next()
        return [frame async for frame in _until_state(late)]

    assert await kept_after(call, _event()) == [call, _event()]
    assert await kept_after(_event()) == []
    assert await kept_after(_event()) == []
    assert await kept_after(call) == [call]


@pytest.mark.parametrize("end", ["ModelResponseComplete", "ModelResponseCancelled"])
async def test_a_finished_reply_is_kept_no_more_until_the_next_model_call(
    end: str,
) -> None:
    """It is a record once complete, stored or about to be: replayed, it shows twice."""
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    call = _event("ModelCallStarted")
    for line in (call, _event(), _event(end), _event("ToolCallStarted")):
        socket.push(line)
        _ = await viewer.next()

    finished = _Viewer(relay)
    _ = await finished.next()
    socket.push(_STATE)
    _ = await viewer.next()
    replayed = [frame async for frame in _until_state(finished)]
    socket.push(call)
    _ = await viewer.next()
    again = _Viewer(relay)
    _ = await again.next()

    assert replayed == []
    assert await again.next() == call


async def _until_state(viewer: _Viewer) -> AsyncIterator[ViewerFrame]:
    """Yield a viewer's frames up to the next state line, which marks the end."""
    while not isinstance(frame := await viewer.next(), StateLine):
        yield frame


async def test_a_viewer_too_far_behind_is_closed_and_others_go_on() -> None:
    relay = SessionRelay(viewer_lag=2)
    slow, quick = _Viewer(relay), _Viewer(relay)
    _ = await slow.next(), await quick.next()
    records = [RecordBody(idx=index, kind="X") for index in range(3)]

    for record in records[:2]:
        relay.publish_records(_SESSION, part=0, records=[record])
        _ = await quick.next()
    relay.publish_records(_SESSION, part=0, records=records[2:] * 2)

    assert await slow.ended()
    assert [await quick.next(), await quick.next()] == [
        RecordLine(part=0, record=records[2]),
    ] * 2


async def test_a_viewer_just_within_its_lag_is_kept() -> None:
    relay = SessionRelay(viewer_lag=2)
    viewer = _Viewer(relay)
    _ = await viewer.next()

    relay.publish_records(
        _SESSION,
        part=0,
        records=[RecordBody(idx=index, kind="X") for index in range(2)],
    )

    assert [(await viewer.next()).type for _ in range(2)] == ["record", "record"]


async def test_a_viewer_one_line_past_its_lag_is_closed_and_reads_none_of_them() -> (
    None
):
    relay = SessionRelay(viewer_lag=2)
    viewer = _Viewer(relay)
    _ = await viewer.next()

    relay.publish_records(
        _SESSION,
        part=0,
        records=[RecordBody(idx=index, kind="X") for index in range(3)],
    )

    assert await viewer.ended()


def _sized_line(size: int) -> EventLine:
    """Return an event line that is ``size`` bytes as the process sends it."""
    pad = size - len(_event(text="").model_dump_json().encode())
    return _event(text="x" * pad)


async def test_a_viewer_whose_unread_lines_pass_their_bytes_is_closed() -> None:
    """Lines, not their bytes, were the only bound: 1024 frames of 2 MiB each."""
    relay = SessionRelay(viewer_bytes=1_000)
    slow, reading = _Viewer(relay), _Viewer(relay)
    _ = await slow.next(), await reading.next()
    socket = _Socket()
    _ = _serve(relay, socket)
    _ = await slow.process(), await reading.process()

    for _ in range(2):
        socket.push(_sized_line(450))
        assert isinstance(await reading.next(), EventLine)
    # A record counts as it is read: what the viewer is sent.
    relay.publish_records(_SESSION, part=0, records=[RecordBody(idx=0, kind="X")])
    assert isinstance(await reading.next(), RecordLine)

    # Two lines and the record pass 1,000 bytes: it is closed, reading none of them.
    assert await slow.ended()


async def test_a_viewer_that_reads_its_lines_has_their_bytes_back() -> None:
    relay = SessionRelay(viewer_bytes=1_000)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = _serve(relay, socket)
    _ = await viewer.process()

    for _ in range(3):
        socket.push(_sized_line(450))
        assert isinstance(await viewer.next(), EventLine)
    # All it read is its own again: a line of the whole budget, unread, still fits.
    socket.push(_sized_line(1_000))
    assert await viewer.next() == _sized_line(1_000)
    socket.push(_STATE)

    assert await viewer.next() == _STATE


async def test_an_input_reaches_the_process_with_its_author_once() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    new = NewInput(kind="UserMessage", payload={"content": "hi"})

    assert relay.send_input(_SESSION, new, author="ada@x.y") == new.id
    sent = await socket.input()

    assert (sent.id, sent.author, sent.kind, sent.payload) == (
        new.id,
        "ada@x.y",
        "UserMessage",
        {"content": "hi"},
    )
    assert socket.sent() == 0
    assert (sent.recipient, sent.booked) == ("", None)


async def test_an_input_names_the_agent_it_is_for_and_how_it_was_booked() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    new = NewInput(kind="UserMessage", payload={"content": "status?"})

    _ = relay.send_input(
        _SESSION,
        new,
        author="worker#415",
        recipient="lead",
        booked="ask",
    )
    sent = await socket.input()

    assert (sent.author, sent.recipient, sent.booked) == ("worker#415", "lead", "ask")


async def test_the_key_a_process_holds_its_socket_with_is_told_while_attached() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    key = UUID(int=70)
    socket = _Socket()
    served = _serve(relay, socket, key=key)
    _ = await viewer.process()
    keyless = _serve(relay, _Socket(), session_id=_OTHER, process=_ENDED)
    await asyncio.sleep(0)

    other = relay.process(_OTHER)
    assert other is not None
    assert other.state == "attached"
    attached = (relay.socket_key(_SESSION), relay.socket_key(_OTHER))
    socket.push(1000)
    await served
    _ = keyless.cancel()

    assert attached == (key, None)
    assert (relay.socket_key(_SESSION), relay.socket_key(_LAPTOP)) == (None, None)


async def test_inputs_are_held_while_the_process_starts_then_sent_in_order() -> None:
    relay = SessionRelay(held_inputs=2)
    _ = _start(relay)
    held = [NewInput(kind="UserMessage", payload={"n": n}) for n in range(2)]
    for new in held:
        _ = relay.send_input(_SESSION, new, author="ada@x.y")

    with pytest.raises(
        InputRefusedError,
        match=r"^the agent is starting and holds 2 inputs already; send this once it "
        r"has started$",
    ):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    socket = _Socket()
    _ = _serve(relay, socket)

    assert [(await socket.input()).id for _ in held] == [new.id for new in held]


async def test_by_default_a_start_holds_256_inputs_and_a_viewer_lags_1024_lines() -> (
    None
):
    relay = SessionRelay()
    _ = _start(relay)
    for _ in range(256):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    with pytest.raises(InputRefusedError, match="holds 256 inputs already"):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    viewer = _Viewer(relay)
    _ = await viewer.next()

    records = [RecordBody(idx=index, kind="X") for index in range(1024)]
    relay.publish_records(_SESSION, part=0, records=records)
    kept = [await viewer.next() for _ in range(1024)]
    relay.publish_records(_SESSION, part=0, records=[*records, records[0]])

    assert len(kept) == 1024
    assert await viewer.ended()


def _sized(size: int) -> NewInput:
    """Return a line whose input, as the relay sends it, is ``size`` bytes."""
    empty = Input(
        id=_HALT.id,
        author="ada@x.y",
        kind="UserMessage",
        payload={"content": ""},
    )
    pad = size - len(empty.model_dump_json().encode())
    return NewInput(id=_HALT.id, kind="UserMessage", payload={"content": "x" * pad})


async def test_an_input_larger_than_a_frame_is_refused_whole() -> None:
    """The process reads no frame past it: sent, it would end the process's socket."""
    relay = SessionRelay()
    _ = _start(relay)

    with pytest.raises(
        InputRefusedError,
        match=rf"^an input is at most {MAX_FRAME_BYTES} bytes; this one is "
        rf"{MAX_FRAME_BYTES + 1}$",
    ):
        _ = relay.send_input(_SESSION, _sized(MAX_FRAME_BYTES + 1), author="ada@x.y")

    assert relay.send_input(_SESSION, _sized(MAX_FRAME_BYTES), author="ada@x.y")


async def test_inputs_held_while_the_process_starts_stop_at_their_bytes() -> None:
    relay = SessionRelay(input_bytes=1_000)
    _ = _start(relay)
    _ = relay.send_input(_SESSION, _sized(600), author="ada@x.y")

    with pytest.raises(
        InputRefusedError,
        match=r"^the agent holds 600 bytes of inputs it has not read, of the 1000 it "
        r"may; send this once it has read them$",
    ):
        _ = relay.send_input(_SESSION, _sized(401), author="ada@x.y")
    _ = relay.send_input(_SESSION, _sized(400), author="ada@x.y")
    socket = _Unread()
    _ = _serve(relay, socket)
    await asyncio.sleep(0)

    # Once it attaches they wait on its socket, unread, and still count.
    with pytest.raises(InputRefusedError, match=r"^the agent holds 1000 bytes "):
        _ = relay.send_input(_SESSION, _sized(1), author="ada@x.y")
    socket.reading.set()
    assert [len((await socket.input()).model_dump_json()) for _ in range(2)] == [
        600,
        400,
    ]


class _Unread(_Socket):
    """A process socket whose peer reads nothing until the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.reading = asyncio.Event()

    @override
    async def send_text(self, text: str) -> None:
        _ = await self.reading.wait()
        await super().send_text(text)


async def test_inputs_a_process_has_not_read_stop_at_their_bytes_until_it_reads() -> (
    None
):
    relay = SessionRelay(input_bytes=1_000)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Unread()
    _ = await _attached(relay, viewer, socket)
    for _ in range(2):
        _ = relay.send_input(_SESSION, _sized(500), author="ada@x.y")
    await asyncio.sleep(0)

    with pytest.raises(InputRefusedError, match=r"^the agent holds 1000 bytes "):
        _ = relay.send_input(_SESSION, _sized(1), author="ada@x.y")
    socket.reading.set()
    _ = [await socket.input() for _ in range(2)]

    assert relay.send_input(_SESSION, _sized(1_000), author="ada@x.y")
    assert len((await socket.input()).model_dump_json()) == 1_000


async def test_an_input_nobody_serves_is_refused() -> None:
    relay = SessionRelay()
    with pytest.raises(
        NotLiveError,
        match=rf"^no agent process serves session {_SESSION}$",
    ):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")

    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    socket.push(1006)
    await served

    with pytest.raises(NotLiveError):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")


async def test_an_input_too_large_for_one_frame_is_refused_and_one_that_fits_goes() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket)
    fits = _sized(MAX_FRAME_BYTES)

    with pytest.raises(
        InputRefusedError,
        match=r"^an input is at most 2097152 bytes; this one is 2097153$",
    ):
        _ = relay.send_input(_SESSION, _sized(MAX_FRAME_BYTES + 1), author=_AUTHOR)
    assert relay.send_input(_SESSION, fits, author=_AUTHOR) == fits.id

    assert (await socket.input()).id == fits.id
    assert socket.sent() == 0


async def test_an_input_too_large_for_one_frame_is_not_held_for_a_starting_process() -> (
    None
):
    relay = SessionRelay()
    _ = _start(relay)

    with pytest.raises(InputRefusedError, match=r"^an input is at most "):
        _ = relay.send_input(_SESSION, _sized(3 << 20), author=_AUTHOR)
    socket = _Socket()
    _ = _serve(relay, socket)
    await asyncio.sleep(0)

    assert socket.sent() == 0


async def test_an_input_sent_on_a_socket_that_was_replaced_is_not_sent_again() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    old = _Socket()
    first = await _attached(relay, viewer, old)
    _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    assert (await old.input()).id == _HALT.id

    new = _Socket()
    _ = _serve(relay, new)
    await first

    assert new.sent() == 0


async def test_a_start_wakes_its_hosts_wait_and_is_handed_out_once() -> None:
    relay = SessionRelay()
    waiting = asyncio.ensure_future(relay.wait(_HOST, wait_sec=5, dropped=_here))
    await asyncio.sleep(0)

    process = _start(relay, spec=ProcessSpec(cwd="/w", args=("--model", "opus")))
    serve = await asyncio.wait_for(waiting, 1)

    assert (process.state, process.host, process.process_id) == (
        "starting",
        "lab-box",
        _PROCESS,
    )
    assert serve is not None
    assert (serve.session_id, serve.process_id, serve.token) == (
        _SESSION,
        _PROCESS,
        f"key-{_PROCESS.hex}",
    )
    assert serve.spec == ProcessSpec(host="lab-box", cwd="/w", args=("--model", "opus"))
    assert serve.remote is False
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None


async def test_a_start_for_remote_agents_tells_its_daemon_so() -> None:
    """A daemon that confines nothing then refuses it."""
    relay = SessionRelay()
    _ = _start(relay, remote=True)

    serve = await relay.wait(_HOST, wait_sec=0, dropped=_here)

    assert serve is not None
    assert serve.remote is True


async def test_a_start_is_offered_to_its_own_host_alone() -> None:
    relay = SessionRelay()
    _ = _start(relay)

    assert await relay.wait(_LAPTOP, wait_sec=0, dropped=_here) is None
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is not None


async def test_a_start_while_one_starts_or_serves_changes_nothing() -> None:
    relay = SessionRelay()
    first = _start(relay)

    assert _start(relay, process=_OTHER) == first
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is not None
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None


async def test_a_start_once_the_process_is_detached_starts_another() -> None:
    relay = SessionRelay()
    _ = _start(relay)
    stopped = await relay.stop(_SESSION)

    again = _start(relay, process=_OTHER)

    assert stopped is not None
    assert stopped.state == "detached"
    assert (again.process_id, again.state) == (_OTHER, "starting")


async def test_starts_on_one_host_are_handed_out_in_turn_and_a_stop_takes_its_own_back() -> (
    None
):
    relay = SessionRelay()
    waiting = asyncio.ensure_future(relay.wait(_HOST, wait_sec=1, dropped=_here))
    await asyncio.sleep(0)
    sessions = [UUID(int=10 + n) for n in range(3)]
    for number, session in enumerate(sessions):
        _ = relay.start(
            session,
            ProcessSpec(),
            process_id=UUID(int=20 + number),
            host_id=_HOST,
            host="lab-box",
            token=f"key-{number}",
            remote=False,
        )

    first = await asyncio.wait_for(waiting, 1)
    _ = await relay.stop(sessions[2])

    assert first is not None
    assert first.session_id == sessions[0]
    second = await relay.wait(_HOST, wait_sec=0, dropped=_here)
    assert second is not None
    assert second.session_id == sessions[1]
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None


async def test_of_two_waits_on_one_host_one_takes_the_start() -> None:
    relay = SessionRelay()
    waits = [
        asyncio.ensure_future(relay.wait(_HOST, wait_sec=sec, dropped=_here))
        for sec in (1, 0.01)
    ]
    await asyncio.sleep(0)

    _ = _start(relay)
    taken = await asyncio.wait_for(asyncio.gather(*waits), 1)

    assert [serve is not None for serve in taken] == [True, False]


class _Asked:
    """Whether a wait's caller has gone, answered after a turn of the loop.

    The turn lets every other wait woken by the same start ask too, as a request's
    disconnect check, which reads the socket, lets them.
    """

    def __init__(self, *, gone: bool) -> None:
        self.gone = gone
        self.asked = 0

    async def __call__(self) -> bool:
        self.asked += 1
        await asyncio.sleep(0)
        return self.gone


async def test_a_wait_whose_caller_dropped_takes_no_start_and_the_next_wait_gets_it() -> (
    None
):
    """A daemon killed while its wait is held leaves that wait held on the server."""
    relay = SessionRelay()
    dropped = _Asked(gone=True)
    held = asyncio.ensure_future(relay.wait(_HOST, wait_sec=5, dropped=dropped))
    await asyncio.sleep(0)

    _ = _start(relay)
    left = await asyncio.wait_for(held, 1)
    serve = await relay.wait(_HOST, wait_sec=0, dropped=_here)

    assert (left, dropped.asked) == (None, 1)
    assert serve is not None
    assert serve.process_id == _PROCESS


async def test_a_wait_whose_caller_drops_with_no_start_queued_just_ends() -> None:
    relay = SessionRelay()
    dropped = _Asked(gone=True)

    assert await relay.wait(_HOST, wait_sec=0, dropped=dropped) is None
    # Asked only with a start to take: it takes nothing, and leaves nothing held.
    assert dropped.asked == 0
    assert relay._waiters == {}


async def test_two_live_waits_and_one_start_hand_it_out_once() -> None:
    """Each asks after the start woke both; the one that answers second finds none."""
    relay = SessionRelay()
    first, second = _Asked(gone=False), _Asked(gone=False)
    waits = [
        asyncio.ensure_future(relay.wait(_HOST, wait_sec=5, dropped=asked))
        for asked in (first, second)
    ]
    await asyncio.sleep(0)

    _ = _start(relay)
    done, held = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    _ = _start(relay, session_id=_ENDED, process=_OTHER)
    taken = [await asyncio.wait_for(wait, 1) for wait in (*done, *held)]

    assert (first.asked, second.asked) == (1, 2)
    assert [serve.process_id if serve else None for serve in taken] == [
        _PROCESS,
        _OTHER,
    ]


async def _taken(relay: SessionRelay, *, process: UUID = _PROCESS) -> AgentProcess:
    """Start ``process`` and have its host's daemon take the start."""
    started = _start(relay, process=process)
    serve = await relay.wait(_HOST, wait_sec=0, dropped=_here)
    assert serve is not None
    return started


async def test_a_start_taken_and_never_answered_is_detached_as_not_started(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its daemon died, or its answer was lost, between the take and the start."""
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    _ = await _taken(relay)
    _ = await viewer.process()

    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is True

    told = await viewer.process()
    assert (told.process_id, told.state, told.reason, told.failed) == (
        _PROCESS,
        "detached",
        "not started",
        True,
    )
    assert _warned(caplog) == [_failed_said("not started")]
    # Never offered again: run twice, each would cut the other's socket.
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None
    with pytest.raises(NotLiveError):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")


async def test_a_start_still_queued_is_given_up_on_as_one_its_host_did_not_take(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its host's daemon is away: it holds no start, no input and no offer for good."""
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    started = _start(relay)
    _ = await viewer.process()
    _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")

    # A give-up of a start taken leaves one still queued.
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is False
    assert relay.process(_SESSION) == started
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=False) is True

    told = await viewer.process()
    assert (told.process_id, told.state, told.reason, told.failed) == (
        _PROCESS,
        "detached",
        "its host did not take it",
        True,
    )
    assert _warned(caplog) == [_failed_said("its host did not take it")]
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None
    with pytest.raises(NotLiveError):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    # Its daemon's late wait finds nothing to start.
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=False) is False


async def test_a_start_its_daemon_took_is_not_given_up_on_as_still_queued() -> None:
    """It has had its time to attach since the take, not since the start."""
    relay = SessionRelay()
    started = await _taken(relay)

    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=False) is False

    assert relay.process(_SESSION) == started


async def test_a_start_whose_hello_came_first_stays_attached() -> None:
    relay = SessionRelay()
    _ = await _taken(relay)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    _ = _serve(relay, socket)
    attached = await viewer.process()

    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is False

    assert relay.process(_SESSION) == attached
    assert attached.state == "attached"
    assert not socket.closed.done()


async def test_a_start_a_person_stopped_first_is_left_as_the_stop_left_it() -> None:
    relay = SessionRelay()
    _ = await _taken(relay)
    stopped = await relay.stop(_SESSION)
    viewer = _Viewer(relay)
    _ = await viewer.next()

    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is False

    assert relay.process(_SESSION) == stopped
    assert stopped is not None
    assert stopped.reason == "stopped"
    # No second detach reaches a viewer: the next line is the record after it.
    relay.publish_records(_SESSION, part=0, records=[RecordBody(idx=0, kind="X")])
    assert isinstance(await viewer.next(), RecordLine)


async def test_a_start_a_newer_start_replaced_is_left_alone() -> None:
    relay = SessionRelay()
    _ = await _taken(relay)
    _ = await relay.stop(_SESSION)
    newer = _start(relay, process=_OTHER)

    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is False

    assert relay.process(_SESSION) == newer
    assert newer.state == "starting"


async def test_a_daemons_report_after_the_give_up_says_why_and_changes_nothing_else() -> (
    None
):
    relay = SessionRelay()
    _ = await _taken(relay)
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is True
    given_up = relay.process(_SESSION)

    relay.ended(
        ProcessEnded(process_id=_PROCESS, exit_code=None, reason="no folder"),
        host_id=_HOST,
    )

    process = relay.process(_SESSION)
    assert given_up is not None
    assert process == given_up.model_copy(update={"reason": "no folder"})


async def test_a_late_hello_after_the_give_up_is_refused_and_a_new_start_runs() -> None:
    relay = SessionRelay()
    _ = await _taken(relay)
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is True

    late = _Socket()
    await _serve(relay, late)
    again = _start(relay, process=_OTHER)

    assert await late.close_code() == wire_terminals.CLOSE_ENDED
    assert (again.process_id, again.state) == (_OTHER, "starting")
    serve = await relay.wait(_HOST, wait_sec=0, dropped=_here)
    assert serve is not None
    assert (serve.process_id, serve.token) == (_OTHER, f"key-{_OTHER.hex}")


async def test_a_give_up_of_a_session_the_relay_holds_nothing_of_changes_nothing() -> (
    None
):
    relay = SessionRelay()

    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is False
    # One a viewer watches, which has had no process.
    viewer = _Viewer(relay)
    _ = await viewer.next()
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is False
    assert relay.process(_SESSION) is None


async def test_a_wait_with_nothing_offered_answers_none_in_its_time() -> None:
    relay = SessionRelay()

    assert await relay.wait(_HOST, wait_sec=0.001, dropped=_here) is None


async def test_a_stop_closes_the_socket_as_ended() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)

    stopped = await relay.stop(_SESSION)
    await served

    assert stopped is not None
    assert (stopped.state, stopped.reason) == ("detached", "stopped")
    assert await socket.close_code() == wire_terminals.CLOSE_ENDED
    assert await viewer.process() == stopped


class _SlowToClose(_Socket):
    """A socket whose close waits on ``released``, as a peer slow to answer one does."""

    def __init__(self) -> None:
        super().__init__()
        self.released = asyncio.Event()

    @override
    async def close(self, code: int = 1000, reason: str = "") -> None:
        _ = await self.released.wait()
        await super().close(code, reason)


async def test_a_stop_names_the_process_it_stopped_though_another_started_meanwhile() -> (
    None
):
    """The caller revokes the key of the process the stop returns."""
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _SlowToClose()
    served = await _attached(relay, viewer, socket)
    first = relay.process(_SESSION)
    assert first is not None

    stopping = asyncio.ensure_future(relay.stop(_SESSION))
    assert (await viewer.process()).state == "detached"
    second = _start(relay, process=_OTHER)
    socket.released.set()
    stopped = await stopping
    await served

    assert stopped is not None
    assert (stopped.process_id, stopped.state) == (first.process_id, "detached")
    assert relay.process(_SESSION) == second


async def test_serving_names_each_process_starting_or_attached_on_any_host() -> None:
    relay = SessionRelay()
    _ = _start(relay)
    _ = _start(relay, session_id=_ENDED, process=_OTHER, host_id=_LAPTOP)
    assert relay.live_process_ids() == {_PROCESS, _OTHER}

    _ = await relay.stop(_ENDED)

    assert relay.live_process_ids() == {_PROCESS}


async def test_a_stop_forgets_a_start_no_daemon_took_and_drops_held_inputs() -> None:
    relay = SessionRelay()
    _ = _start(relay)
    _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")

    stopped = await relay.stop(_SESSION)

    assert stopped is not None
    assert stopped.reason == "stopped"
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None
    with pytest.raises(NotLiveError):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")


async def test_a_processs_output_is_kept_apart_from_the_reason_every_viewer_reads() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    _ = await _taken(relay)
    _ = await viewer.process()
    assert relay.output(_SESSION) == ""

    relay.ended(
        ProcessEnded(
            process_id=_PROCESS,
            exit_code=3,
            reason="exited with code 3",
            output="Traceback: secret",
        ),
        host_id=_HOST,
    )
    told = await viewer.process()
    # A report after the detach says its reason and output again, as the last one.
    relay.ended(
        ProcessEnded(process_id=_PROCESS, exit_code=3, reason="again", output="later"),
        host_id=_HOST,
    )
    again = await viewer.process()

    assert (told.reason, told.output) == ("exited with code 3", "")
    assert (again.reason, again.output) == ("again", "")
    assert relay.output(_SESSION) == "later"
    assert relay.output(_OTHER) == ""
    # The next process's start forgets the last one's output.
    _ = _start(relay, process=_OTHER)
    assert relay.output(_SESSION) == ""


async def test_a_new_process_attaching_forgets_the_last_ones_output() -> None:
    relay = SessionRelay()
    _ = await _taken(relay)
    relay.ended(
        ProcessEnded(process_id=_PROCESS, exit_code=3, reason="r", output="o"),
        host_id=_HOST,
    )

    _ = _serve(relay, _Socket(), process=_OTHER)
    await asyncio.sleep(0)

    assert relay.output(_SESSION) == ""


async def test_a_stop_of_nothing_or_of_an_ended_process_changes_nothing() -> None:
    relay = SessionRelay()
    assert await relay.stop(_SESSION) is None
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    socket.push(1000)
    await served
    exited = await viewer.process()

    assert await relay.stop(_SESSION) == exited


async def test_a_stop_as_a_bad_frame_closes_the_socket_closes_it_once_as_stopped() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)

    socket.push("not json")
    await asyncio.sleep(0)
    stopped = await relay.stop(_SESSION)
    await served

    assert stopped is not None
    assert stopped.reason == "stopped"
    assert await socket.close_code() == wire_terminals.CLOSE_ENDED


async def test_a_stop_of_a_process_whose_socket_dropped_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tick(monkeypatch)
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    attached = relay.process(_SESSION)
    socket.push(1006)
    await served
    dropped = await viewer.process()

    stopped = await relay.stop(_SESSION)

    assert stopped is not None
    assert stopped.reason == "stopped"
    assert await viewer.process() == stopped
    # Detached from its drop, and still so from then: the stop changes only why.
    assert attached is not None
    assert attached.since < dropped.since == stopped.since


async def test_an_end_from_the_host_it_was_offered_to_detaches_it_with_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another session, held first, is passed over to find it."""
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = _start(relay, session_id=_ENDED, process=_OTHER)
    _ = _start(relay)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)

    relay.ended(
        ProcessEnded(
            process_id=_PROCESS,
            exit_code=1,
            reason="no login",
            output="run the login first\n",
        ),
        host_id=_HOST,
    )
    await served

    process = await viewer.process()
    assert (process.state, process.reason, process.failed) == (
        "detached",
        "no login",
        True,
    )
    assert await socket.close_code() == wire_terminals.CLOSE_ENDED
    assert socket.closed.result() == (
        wire_terminals.CLOSE_ENDED,
        "its daemon saw it end",
    )
    assert _warned(caplog) == [
        _failed_said("no login", exit_code=1, output="run the login first\n"),
    ]
    other = relay.process(_ENDED)
    assert other is not None
    assert other.state == "starting"
    # The socket's own close comes after, and never overwrites what the daemon said.
    assert relay.process(_SESSION) == process


async def test_an_end_after_the_socket_closed_still_gives_the_daemons_reason() -> None:
    relay = SessionRelay()
    _ = _start(relay)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    socket.push(1000)
    await served
    exited = await viewer.process()

    relay.ended(
        ProcessEnded(process_id=_PROCESS, exit_code=0, reason="bye"),
        host_id=_HOST,
    )

    told = await viewer.process()
    assert (told.state, told.reason, told.since) == ("detached", "bye", exited.since)
    assert told.failed is False


@pytest.mark.parametrize(
    ("exit_code", "reason", "output"),
    [
        (2, "exited with code 2", "Error: --detach needs a value\n"),
        (None, "no folder", ""),
        (0, "exited with code 0", "usage: agent run\n"),
    ],
    ids=["exited", "could not be started", "exited cleanly before its hello"],
)
async def test_an_end_that_never_started_is_warned_of_with_its_code_and_output(
    caplog: pytest.LogCaptureFixture,
    exit_code: int | None,
    reason: str,
    output: str,
) -> None:
    """The process never said ``Hello``: whatever its code, its start failed."""
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = await _taken(relay)

    relay.ended(
        ProcessEnded(
            process_id=_PROCESS,
            exit_code=exit_code,
            reason=reason,
            output=output,
        ),
        host_id=_HOST,
    )

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.state, process.reason, process.failed) == (
        "detached",
        reason,
        True,
    )
    assert relay.output(_SESSION) == output
    assert _warned(caplog) == [
        _failed_said(reason, exit_code=exit_code, output=output),
    ]
    assert _detached_said(reason) not in _said(caplog)


async def test_a_start_given_up_on_stays_failed_whatever_its_late_exit_says(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its late ``Hello`` was told it ended, and it exited 0: its start still failed."""
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = await _taken(relay)
    assert relay.give_up(_SESSION, process_id=_PROCESS, taken=True) is True

    relay.ended(
        ProcessEnded(process_id=_PROCESS, exit_code=0, reason="exited with code 0"),
        host_id=_HOST,
    )

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.reason, process.failed) == ("exited with code 0", True)
    assert _warned(caplog) == [
        _failed_said("not started"),
        _failed_said("exited with code 0", exit_code=0),
    ]


async def test_a_start_a_person_stopped_that_then_exits_cleanly_is_no_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It never said ``Hello``, but a person's stop is no failure."""
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = await _taken(relay)
    _ = await relay.stop(_SESSION)

    relay.ended(
        ProcessEnded(process_id=_PROCESS, exit_code=0, reason="exited with code 0"),
        host_id=_HOST,
    )

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.reason, process.failed) == ("exited with code 0", False)
    assert _warned(caplog) == []
    assert _detached_said("exited with code 0") in _said(caplog)


async def test_the_same_end_twice_tells_viewers_once() -> None:
    relay = SessionRelay()
    _ = _start(relay)
    viewer = _Viewer(relay)
    _ = await viewer.next()
    report = ProcessEnded(process_id=_PROCESS, exit_code=1, reason="gone")

    relay.ended(report, host_id=_HOST)
    relay.ended(report, host_id=_HOST)
    relay.publish_records(_SESSION, part=0, records=[RecordBody(idx=0, kind="X")])

    assert (await viewer.process()).reason == "gone"
    assert isinstance(await viewer.next(), RecordLine)


@pytest.mark.parametrize(
    ("process_id", "host_id"),
    [(_OTHER, _HOST), (_PROCESS, _LAPTOP)],
    ids=["another process", "another host"],
)
async def test_any_other_end_is_ignored(process_id: UUID, host_id: UUID) -> None:
    relay = SessionRelay()
    started = _start(relay)

    relay.ended(
        ProcessEnded(process_id=process_id, exit_code=1, reason="x"),
        host_id=host_id,
    )

    assert relay.process(_SESSION) == started


@pytest.mark.parametrize(
    ("code", "reason"),
    [(1000, "exited"), (1006, "disconnected"), (1001, "disconnected")],
)
async def test_a_socket_the_process_closes_detaches_it_by_its_code(
    caplog: pytest.LogCaptureFixture,
    code: int,
    reason: str,
) -> None:
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)

    socket.push(code)
    await served

    process = await viewer.process()
    assert (process.state, process.reason) == ("detached", reason)
    assert _detached_said(reason) in _said(caplog)


async def test_a_process_attaches_to_a_session_the_relay_holds_nothing_of() -> None:
    relay = SessionRelay()

    served = _serve(relay, _Socket())
    # The attach takes no turn of the loop: one is enough for the route to reach it.
    await asyncio.sleep(0)

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.process_id, process.state) == (_PROCESS, "attached")
    _ = served.cancel()


def _detached_said(reason: str) -> str:
    """Return the line the relay logs as it detaches ``_SESSION``'s ``_PROCESS``."""
    return f"process detached: session={_SESSION} process={_PROCESS} reason={reason!r}"


def _failed_said(
    reason: str,
    *,
    exit_code: int | None = None,
    output: str = "",
) -> str:
    """Return the warning logged as ``_SESSION``'s ``_PROCESS``, on lab-box, fails."""
    return (
        f"agent start failed: session={_SESSION} process={_PROCESS} host=lab-box "
        f"exit_code={exit_code} reason={reason!r} output={output!r}"
    )


def _said(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return each line logged, whole."""
    return [record.getMessage() for record in caplog.records]


def _warned(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return each line logged as a warning, whole."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]


def _tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have the relay's clock move a second on at each reading, from ``_SINCE``."""
    seconds = iter(range(1, 60))
    monkeypatch.setattr(
        session_relay,
        "_now",
        lambda: _SINCE.replace(second=next(seconds)),
    )


async def test_a_socket_that_cannot_be_sent_to_detaches_its_process() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    await socket.close()

    _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    await served

    assert (await viewer.process()).reason == "disconnected"


async def test_a_process_line_is_written_when_its_state_or_reason_changes() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()

    started = _start(relay)
    assert await viewer.process() == started
    _ = _serve(relay, _Socket())
    attached = await viewer.process()
    again = _Socket()
    _ = _serve(relay, again)
    again.push(_STATE)

    assert (started.state, attached.state) == ("starting", "attached")
    assert attached.since >= started.since
    # The same process on a new socket changes neither, so no line is written.
    assert await viewer.next() == _STATE


async def test_on_detach_the_kept_state_settings_and_lines_are_cleared() -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    for line in (_STATE, _SETTINGS, _event("ModelCallStarted")):
        socket.push(line)
        _ = await viewer.next()

    socket.push(1006)
    await served
    detached = await viewer.process()
    late = _Viewer(relay)

    assert await late.next() == Opened(process=detached)
    # Its next frame is the process again, not a kept line.
    _ = await _attached(relay, late, _Socket())


async def test_a_host_stopped_closes_its_sockets_and_ends_its_waits() -> None:
    relay = SessionRelay()
    waiting = asyncio.ensure_future(relay.wait(_HOST, wait_sec=5, dropped=_here))
    await asyncio.sleep(0)
    _ = _start(relay)
    _ = await waiting
    viewer = _Viewer(relay)
    _ = await viewer.next()
    socket = _Socket()
    served = await _attached(relay, viewer, socket)
    held = asyncio.ensure_future(relay.wait(_HOST, wait_sec=5, dropped=_here))
    await asyncio.sleep(0)

    stopped = await relay.stop_host(_HOST)
    await served

    assert stopped == [_SESSION]
    assert relay.processes(_HOST) == []
    assert await socket.close_code() == wire_terminals.CLOSE_HOST_REVOKED
    assert socket.closed.result() == (
        wire_terminals.CLOSE_HOST_REVOKED,
        "its host was revoked",
    )
    assert (await viewer.process()).reason == "host revoked"
    with pytest.raises(HostRevokedError, match=f"^host {_HOST} was revoked$"):
        await asyncio.wait_for(held, 1)


async def test_a_host_stopped_forgets_its_unanswered_starts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    _ = _start(relay)

    assert await relay.stop_host(_HOST) == [_SESSION]

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "host revoked")
    assert _detached_said("host revoked") in _said(caplog)
    assert await relay.wait(_HOST, wait_sec=0, dropped=_here) is None


async def test_a_host_stopped_leaves_other_hosts_processes_and_ended_ones() -> None:
    relay = SessionRelay()
    started = _start(relay, host_id=_LAPTOP)
    _ = _start(relay, session_id=_ENDED, process=_OTHER)
    ended = await relay.stop(_ENDED)

    assert await relay.stop_host(_HOST) == []

    assert relay.process(_SESSION) == started
    assert relay.process(_ENDED) == ended


async def test_a_hosts_processes_are_those_that_may_still_run_there() -> None:
    relay = SessionRelay()
    starting, attached, dropped, exited = (UUID(int=100 + each) for each in range(4))
    _ = _start(relay, session_id=starting)
    for session_id in (attached, dropped, exited):
        viewer = _Viewer(relay, session_id)
        _ = await viewer.next()
        socket = _Socket()
        served = _serve(relay, socket, session_id=session_id, host_id=_HOST)
        _ = await viewer.process()
        if session_id != attached:
            socket.push(1006 if session_id == dropped else 1000)
            await served
    _ = _start(relay, session_id=_ENDED, process=_OTHER)
    _ = await relay.stop(_ENDED)
    elsewhere = _start(relay, host_id=_LAPTOP)

    # Starting, attached, or detached by a drop alone: what a stop would stop.
    assert relay.processes(_HOST) == [
        relay.process(starting),
        relay.process(attached),
        relay.process(dropped),
    ]
    assert relay.processes(_LAPTOP) == [elsewhere]
    assert relay.processes(UUID(int=99)) == []


class _Rule:
    """A rule whose answer the test sets; it counts each time it is asked."""

    def __init__(self) -> None:
        self.holds = True
        self.asked = 0

    async def __call__(self) -> bool:
        self.asked += 1
        return self.holds


async def _unknown() -> bool:
    """Raise, as a rule that cannot be asked now does."""
    raise ConnectionError("the database did not answer")


async def test_a_sweep_ends_the_stream_of_a_viewer_who_may_no_longer_read() -> None:
    relay = SessionRelay()
    rule = _Rule()
    lost, kept = _Viewer(relay, allowed=rule), _Viewer(relay)
    _ = await lost.next(), await kept.next()

    await relay.sweep()
    assert rule.asked == 1
    rule.holds = False
    await relay.sweep()
    relay.publish_records(_SESSION, part=0, records=[RecordBody(idx=0, kind="X")])

    # It reads nothing more, the record published after the sweep least of all.
    assert await lost.ended()
    assert await kept.next() == RecordLine(part=0, record=RecordBody(idx=0, kind="X"))


async def test_a_sweep_closes_the_socket_of_a_process_its_key_may_no_longer_serve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=session_relay.__name__)
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    rule = _Rule()
    socket = _Socket()
    served = await _attached(relay, viewer, socket, allowed=rule)

    await relay.sweep()
    assert not socket.closed.done()
    rule.holds = False
    await relay.sweep()
    await served

    assert socket.closed.result() == (
        wire_terminals.CLOSE_REFUSED,
        "this key may no longer serve this session",
    )
    process = await viewer.process()
    assert (process.state, process.reason) == ("detached", "refused")
    assert _detached_said("refused") in _said(caplog)
    with pytest.raises(NotLiveError):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")


async def test_a_rule_that_cannot_be_asked_leaves_its_stream_and_socket_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = SessionRelay()
    viewer = _Viewer(relay, allowed=_unknown)
    _ = await viewer.next()
    socket = _Socket()
    _ = await _attached(relay, viewer, socket, allowed=_unknown)

    await relay.sweep()
    socket.push(_STATE)

    assert await viewer.next() == _STATE
    assert not socket.closed.done()
    unknown = [
        record
        for record in caplog.records
        if record.getMessage() == "session relay: whether a rule holds is unknown"
    ]
    # Once for the viewer, once for the socket, each with what the question raised.
    assert len(unknown) == 2
    assert all(
        isinstance(record.exc_info, tuple) and record.exc_info[0] is ConnectionError
        for record in unknown
    )


async def test_a_socket_replaced_while_its_rule_is_asked_is_left_to_its_successor() -> (
    None
):
    relay = SessionRelay()
    viewer = _Viewer(relay)
    _ = await viewer.next()
    asked, answer = asyncio.Event(), asyncio.Event()

    async def refused_late() -> bool:
        asked.set()
        _ = await answer.wait()
        return False

    first = await _attached(relay, viewer, _Socket(), allowed=refused_late)
    sweeping = asyncio.ensure_future(relay.sweep())
    _ = await asyncio.wait_for(asked.wait(), 1)
    successor = _Socket()
    _ = _serve(relay, successor)
    await first
    answer.set()
    await sweeping

    # The rule refused the socket it was asked of, which is gone; not its successor.
    assert not successor.closed.done()
    process = relay.process(_SESSION)
    assert process is not None
    assert process.state == "attached"


async def test_a_session_nothing_of_is_live_and_no_viewer_watches_is_forgotten() -> (
    None
):
    """A detached process whose key is still live may come back, and so is kept."""
    relay = SessionRelay()
    exited, dropped, keyed, watched, starting, attached = (
        UUID(int=100 + each) for each in range(6)
    )
    for session_id in (exited, dropped, keyed, watched, attached):
        socket = _Socket()
        served = _serve(
            relay,
            socket,
            session_id=session_id,
            process=UUID(int=200 + session_id.int),
        )
        await asyncio.sleep(0)
        if session_id != attached:
            socket.push(1006 if session_id == dropped else 1000)
            await served
    _ = _start(relay, session_id=starting)
    viewer = _Viewer(relay, watched)
    _ = await viewer.next()
    kept = {
        session_id: relay.process(session_id)
        for session_id in (keyed, watched, starting, attached)
    }

    relay.forget_idle(keyed=frozenset({UUID(int=200 + keyed.int)}))

    assert {session_id: relay.process(session_id) for session_id in kept} == kept
    assert set(relay._sessions) == set(kept)
    assert relay.watched(watched)


async def test_a_forgotten_sessions_next_process_attaches_as_a_new_one() -> None:
    relay = SessionRelay()
    socket = _Socket()
    served = _serve(relay, socket)
    await asyncio.sleep(0)
    socket.push(1000)
    await served

    relay.forget_idle(keyed=frozenset())
    assert relay.process(_SESSION) is None
    _ = _serve(relay, _Socket(), process=_OTHER)
    await asyncio.sleep(0)

    process = relay.process(_SESSION)
    assert process is not None
    assert (process.process_id, process.state) == (_OTHER, "attached")


async def test_a_viewer_of_nothing_leaves_nothing_behind() -> None:
    relay = SessionRelay()
    rule = _Rule()
    viewer = relay.lines(_SESSION, allowed=rule)
    assert await anext(viewer) == Opened()

    await viewer.aclose()
    await relay.sweep()

    assert relay.process(_SESSION) is None
    with pytest.raises(NotLiveError):
        _ = relay.send_input(_SESSION, _HALT, author="ada@x.y")
    # A closed stream's rule is asked no more, and the session is forgotten.
    assert rule.asked == 0
    assert relay._sessions == {}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
