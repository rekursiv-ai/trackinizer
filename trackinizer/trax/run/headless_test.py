"""Tests for the headless run: a driver in place of a TUI, and the takeover from it.

The drivers and the takeover's terminal are faked in memory; the control socket is a
real one in a temp folder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Self, cast

import asyncio
import logging
import os
import signal
import socket
import threading
import time

from treekle import PlainTree, from_plain, json_loads

import pytest

from trackinizer.lib.agent.types.sessions import SystemMessage
from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run import headless
from trackinizer.trax.run.asks import (
    Answer,
    Ask,
    Option,
    Resolved,
    RunState,
    StateChanged,
)
from trackinizer.trax.run.headless import (
    HeadlessRun,
    HostState,
    Reply,
    RunEndedError,
    StateReporter,
    TakenOverError,
    request,
    say,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from trackinizer.trax.run.asks import Driver


_WAIT: Final = 5.0

_ASK: Final = Ask(
    id="a1",
    provider="codex",
    kind="command",
    tool="shell",
    command="touch x",
    options=(
        Option(id="accept", label="Yes", kind="allow_once"),
        Option(id="decline", label="No", kind="deny"),
    ),
)


class _FakeDriver:
    """A scripted driver: it reports what it is pushed, and remembers what it is told."""

    def __init__(self, *, session: str = "s1", fail: Exception | None = None) -> None:
        self.session = session
        self.fail = fail
        self.started: tuple[Path, str, str, str] | None = None
        self.told: list[tuple[str, object]] = []
        self.stopped = False
        self.refuse: Exception | None = None
        self.hold_start: threading.Event | None = None
        self.starting = threading.Event()
        self.hang = False
        self.hanging = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue = asyncio.Queue[Ask | Resolved | StateChanged | None]()

    def push(self, event: Ask | Resolved | StateChanged | None) -> None:
        """Report ``event`` from any thread; ``None`` ends the driver by itself."""
        loop = self._loop
        assert loop is not None
        loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def start(self, *, cwd: Path, resume: str, model: str, effort: str) -> str:
        self._loop = asyncio.get_running_loop()
        self.starting.set()
        if self.hold_start is not None:
            _ = await asyncio.to_thread(self.hold_start.wait, _WAIT)
        if self.fail is not None:
            raise self.fail
        self.started = (cwd, resume, model, effort)
        return self.session

    async def send_line(self, text: str) -> None:
        await self._tell("line", text)

    async def command(self, verb: str, args: str) -> None:
        await self._tell("command", (verb, args))

    async def answer(self, answer: Answer) -> None:
        await self._tell("answer", answer)

    async def interrupt(self) -> None:
        await self._tell("interrupt", None)

    async def settings(self, *, model: str, effort: str) -> None:
        await self._tell("settings", (model, effort))

    async def stop(self) -> None:
        self.stopped = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[Ask | Resolved | StateChanged]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def _tell(self, what: str, detail: object) -> None:
        if self.refuse is not None:
            raise self.refuse
        if self.hang:
            self.hanging.set()
            _ = await asyncio.Event().wait()
        self.told.append((what, detail))


type _TuiMode = Literal["listens", "late", "silent", "crashes"]


class _FakeTui:
    """A takeover terminal: it listens, then runs until it is ended.

    ``late`` listens a moment after it starts, ``silent`` never listens, and ``crashes``
    fails after it listens.
    """

    def __init__(
        self,
        *,
        listening: Callable[[], None],
        exits_with: int | None,
        mode: _TuiMode,
    ) -> None:
        self._listening = listening
        self._exits_with = exits_with
        self._mode = mode
        self.lines: list[str] = []
        self.ended = threading.Event()
        self.over = threading.Event()

    def run(self) -> int:
        if self._exits_with is not None:
            return self._exits_with
        if self._mode == "late":
            _ = self.ended.wait(0.03)
        if self._mode != "silent":
            self._listening()
        if self._mode == "crashes":
            raise RuntimeError("pty died")
        _ = self.ended.wait(_WAIT)
        self.over.set()
        return 0

    def terminate(self) -> None:
        self.ended.set()

    def submit(self, text: str) -> None:
        self.lines.append(text)

    def finish(self) -> None:
        """Quit the way a person does: the terminal ends by itself."""
        self.ended.set()


@dataclass(slots=True, kw_only=True)
class _Rig:
    """A headless run in a thread, with its fake drivers and terminal."""

    drivers: list[_FakeDriver] = field(default_factory=lambda: [_FakeDriver()])
    tui_exits: tuple[int | None, ...] = ()
    tui_mode: _TuiMode = "listens"
    no_tui: Exception | None = None
    resume: str = ""
    control: Path | None = None
    records: list[SystemMessage] = field(default_factory=list[SystemMessage])
    sessions: list[str] = field(default_factory=list[str])
    states: list[HostState] = field(default_factory=list[HostState])
    taken: list[str] = field(default_factory=list[str])
    tuis: list[_FakeTui] = field(default_factory=list[_FakeTui])
    tui_for: list[str] = field(default_factory=list[str])
    status: list[int] = field(default_factory=list[int])
    started: threading.Event = field(default_factory=threading.Event)
    listening: threading.Event = field(default_factory=threading.Event)
    run: HeadlessRun = field(init=False)
    _thread: threading.Thread = field(init=False)
    _made: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.run = HeadlessRun(
            cli="codex",
            cwd=Path("/work"),
            resume=self.resume,
            model="m1",
            effort="high",
            make_driver=self._make_driver,
            make_tui=self._make_tui,
            record=self.records.append,
            on_session=self.sessions.append,
            on_listening=self.listening.set,
            on_state=self.states.append,
            on_takeover=self.taken.append,
            control=self.control,
        )
        self._thread = threading.Thread(
            target=lambda: self.status.append(
                self.run.run(on_started=self.started.set),
            ),
            daemon=True,
        )

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.run.terminate()
        self._thread.join(_WAIT)
        assert not self._thread.is_alive(), "the run did not end"

    def wait_started(self) -> None:
        assert self.started.wait(_WAIT), "the run did not start"

    def join(self) -> int:
        self._thread.join(_WAIT)
        assert not self._thread.is_alive(), "the run did not end"
        return self.status[0]

    def _make_driver(self) -> Driver:
        driver = self.drivers[self._made]
        self._made += 1
        return cast("Driver", driver)

    def _make_tui(self, *, session_id: str, listening: Callable[[], None]) -> _FakeTui:
        if self.no_tui is not None:
            raise self.no_tui
        self.tui_for.append(session_id)
        nth = len(self.tuis)
        tui = _FakeTui(
            listening=listening,
            exits_with=self.tui_exits[nth] if nth < len(self.tui_exits) else None,
            mode=self.tui_mode,
        )
        self.tuis.append(tui)
        return tui


def _wait_for(check: Callable[[], bool]) -> None:
    done = threading.Event()
    for _ in range(int(_WAIT / 0.005)):
        if check():
            return
        _ = done.wait(0.005)
    raise AssertionError("the run never reached the state awaited")


class TestStart:
    """The run starts its driver where the chat's run was told to."""

    def test_the_driver_starts_in_the_folder_with_the_model(self) -> None:
        with _Rig(resume="old") as rig:
            rig.wait_started()
            assert rig.drivers[0].started == (Path("/work"), "old", "m1", "high")
            assert rig.run.session_id == "s1"
            assert rig.sessions == ["s1"]

    def test_the_launcher_hears_the_run_is_up_after_the_driver_is(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            assert rig.listening.is_set()

    def test_a_driver_that_cannot_start_ends_the_run_with_a_status(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        driver = _FakeDriver(fail=FileNotFoundError("claude"))
        with _Rig(drivers=[driver]) as rig:
            assert rig.join() == 1
            assert not rig.started.is_set()
            assert not rig.listening.is_set()
        assert "could not start codex headless: claude" in capsys.readouterr().err


class TestEvents:
    """What the driver reports becomes the records of the chat's session."""

    def test_an_ask_is_a_card_and_its_end_closes_it(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            rig.drivers[0].push(_ASK)
            rig.drivers[0].push(
                Resolved(ask_id="a1", option="accept", by="chat", decision="allow"),
            )
            _wait_for(lambda: len(rig.records) == 2)
            opened, closed = rig.records
            assert (
                from_plain(opened.extra["$interaction"], dict[str, PlainTree])["id"]
                == "a1"
            )
            assert (
                from_plain(opened.extra["$interaction"], dict[str, PlainTree])[
                    "provider"
                ]
                == "codex"
            )
            assert (
                from_plain(closed.extra["$interaction_resolved"], dict[str, PlainTree])[
                    "decision"
                ]
                == "allow"
            )

    def test_an_ask_names_when_it_was_asked(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            rig.drivers[0].push(_ASK)
            _wait_for(lambda: len(rig.records) == 1)
            stamp = rig.records[0].timestamp
            assert stamp is not None
            assert abs((datetime.now(UTC) - datetime.fromisoformat(stamp)).seconds) < 5

    def test_each_change_of_state_is_reported(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            turn: tuple[RunState, ...] = (
                "running",
                "requires_action",
                "running",
                "idle",
            )
            for state in turn:
                rig.drivers[0].push(StateChanged(state=state))
            _wait_for(lambda: len(rig.states) == 4)
            assert rig.states == ["running", "requires_action", "running", "idle"]
            assert rig.run.state == "idle"

    def test_a_state_that_did_not_change_is_not_reported_again(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            quiet: tuple[RunState, ...] = ("idle", "running", "running")
            for state in quiet:
                rig.drivers[0].push(StateChanged(state=state))
            rig.drivers[0].push(_ASK)
            _wait_for(lambda: len(rig.records) == 1)
            assert rig.states == ["running"]

    def test_a_driver_that_ends_by_itself_ends_the_run(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            rig.drivers[0].push(None)
            assert rig.join() == 1

    def test_the_run_stop_ends_the_driver_and_is_not_a_failure(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            rig.run.terminate()
            assert rig.join() == 0
            assert rig.drivers[0].stopped


class TestWhatThePersonSends:
    """The poll thread's calls reach the driver."""

    def test_a_line_a_command_an_answer_an_interrupt_and_settings(self) -> None:
        answer = Answer(ask_id="a1", option="accept", who="jp")
        with _Rig() as rig:
            rig.wait_started()
            rig.run.send_line("hello")
            rig.run.command("compact", "")
            rig.run.answer(answer)
            rig.run.interrupt()
            rig.run.settings(model="m2", effort="low")
            assert rig.drivers[0].told == [
                ("line", "hello"),
                ("command", ("compact", "")),
                ("answer", answer),
                ("interrupt", None),
                ("settings", ("m2", "low")),
            ]

    def test_what_the_driver_refuses_comes_back_to_the_sender(self) -> None:
        with _Rig() as rig:
            rig.wait_started()
            rig.drivers[0].refuse = ValueError("no such option")
            with pytest.raises(ValueError, match="no such option"):
                rig.run.answer(Answer(ask_id="a1", option="x"))

    def test_a_call_after_the_run_ended_says_so(self) -> None:
        rig = _Rig()
        with rig:
            rig.wait_started()
        with pytest.raises(RunEndedError):
            rig.run.send_line("late")

    def test_a_call_before_the_run_started_says_so(self) -> None:
        rig = _Rig()
        with pytest.raises(RunEndedError):
            rig.run.interrupt()


class TestTakeover:
    """A terminal takes the chat's CLI over, and the chat can have it back."""

    def test_the_driver_stops_and_the_session_is_handed_to_the_terminal(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            reply = request(control, "takeover")
            assert reply.ok
            assert reply.state == "taken_over"
            assert rig.drivers[0].stopped
            assert rig.tui_for == ["s1"]
            assert rig.states == ["taken_over"]
            assert rig.run.state == "taken_over"
            assert rig.taken == ["s1"]

    def test_a_line_is_refused_while_it_is_taken_over(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert request(control, "takeover").ok
            for call in (lambda: rig.run.send_line("hello"), rig.run.interrupt):
                with pytest.raises(TakenOverError) as refused:
                    call()
                assert str(refused.value) == (
                    "taken over in a terminal; reattach from Chat or the Console"
                )

    def test_the_asks_open_at_the_takeover_are_released_first(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            rig.drivers[0].push(_ASK)
            _wait_for(lambda: len(rig.records) == 1)
            rig.drivers[0].push(
                Resolved(ask_id="a1", option="", by="released", decision="released"),
            )
            assert request(control, "takeover").ok
            assert (
                from_plain(
                    rig.records[1].extra["$interaction_resolved"],
                    dict[str, PlainTree],
                )["by"]
                == "released"
            )
            assert rig.states == ["taken_over"]

    def test_a_second_takeover_while_it_is_open_changes_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert request(control, "takeover").ok
            assert request(control, "takeover").ok
            assert len(rig.tuis) == 1
            assert rig.states == ["taken_over"]
            assert rig.taken == ["s1"]

    def test_a_terminal_that_was_quit_is_opened_again_by_the_next_takeover(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert request(control, "takeover").ok
            rig.tuis[0].finish()
            assert rig.tuis[0].over.wait(_WAIT)
            _wait_for(lambda: request(control, "state").state == "taken_over")
            assert request(control, "takeover").ok
            assert len(rig.tuis) == 2
            assert rig.tui_for == ["s1", "s1"]
            assert len(rig.drivers) == 1
            assert rig.taken == ["s1", "s1"]

    def test_a_terminal_that_will_not_start_hands_the_chat_back(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        drivers = [_FakeDriver(), _FakeDriver()]
        with _Rig(control=control, drivers=drivers, tui_exits=(3,)) as rig:
            rig.wait_started()
            reply = request(control, "takeover")
            assert not reply.ok
            assert "exited with status 3" in reply.reason
            assert drivers[1].started == (Path("/work"), "s1", "m1", "high")
            assert rig.run.state == "idle"
            assert rig.states == ["taken_over", "idle"]
            assert rig.taken == []
            rig.run.send_line("hello")
            assert drivers[1].told == [("line", "hello")]

    def test_a_driver_the_chat_is_handed_back_to_may_name_another_session(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        drivers = [_FakeDriver(), _FakeDriver(session="s2")]
        with _Rig(control=control, drivers=drivers, tui_exits=(3,)) as rig:
            rig.wait_started()
            assert not request(control, "takeover").ok
            assert rig.sessions == ["s1", "s2"]
            assert rig.run.session_id == "s2"

    def test_a_terminal_and_a_driver_that_will_not_start_leave_it_taken_over(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        drivers = [_FakeDriver(), _FakeDriver(fail=ConnectionError("gone"))]
        with _Rig(control=control, drivers=drivers, tui_exits=(3,)) as rig:
            rig.wait_started()
            reply = request(control, "takeover")
            assert not reply.ok
            assert "exited with status 3" in reply.reason
            assert "codex could not restart either: gone" in reply.reason
            assert reply.state == "taken_over"
            assert request(control, "state").state == "taken_over"

    def test_a_terminal_that_cannot_be_built_hands_the_chat_back(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        drivers = [_FakeDriver(), _FakeDriver()]
        no_tui = RuntimeError("codex has no turn in this chat yet")
        with _Rig(control=control, drivers=drivers, no_tui=no_tui) as rig:
            rig.wait_started()
            reply = request(control, "takeover")
            assert not reply.ok
            assert reply.reason == "codex has no turn in this chat yet"
            assert rig.run.state == "idle"
            assert drivers[1].started is not None

    def test_a_chat_line_typed_through_send_reaches_the_terminal(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert request(control, "takeover").ok
            assert say(control, "hi").ok
            assert rig.tuis[0].lines == ["hi"]


class TestControl:
    """What a person's command reaches the run through."""

    def test_state_names_the_run(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            reply = request(control, "state")
            assert (reply.ok, reply.state, reply.cli, reply.session) == (
                True,
                "idle",
                "codex",
                "s1",
            )

    def test_send_types_a_line_for_the_driver(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert say(control, "hi").ok
            assert rig.drivers[0].told == [("line", "hi")]

    def test_a_line_the_driver_refuses_is_reported(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            rig.drivers[0].refuse = ConnectionError("claude ended")
            reply = say(control, "hi")
            assert not reply.ok
            assert reply.reason == "claude ended"

    def test_the_reattach_that_was_never_added_is_refused(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            raw = asyncio.run(_raw(control, b'{"op": "reattach"}\n'))
            assert b"unreadable request" in raw

    def test_a_request_that_fails_is_answered_with_why(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            rig.drivers[0].refuse = RuntimeError("boom")
            reply = say(control, "hi")
            assert not reply.ok
            assert reply.reason == "boom"
            assert request(control, "state").ok
        assert [each.getMessage() for each in caplog.records] == [
            "trax run: a control request failed",
        ]
        assert caplog.records[0].levelno == logging.WARNING
        assert isinstance(caplog.records[0].exc_info, tuple)

    def test_an_error_with_no_message_is_named_by_its_type(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            rig.drivers[0].refuse = RuntimeError()
            assert say(control, "hi").reason == "RuntimeError"

    def test_stop_ends_the_run(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert request(control, "stop").ok
            assert rig.join() == 0
            assert rig.drivers[0].stopped

    def test_stop_ends_a_terminal_that_is_open(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert request(control, "takeover").ok
            assert request(control, "stop").ok
            assert rig.join() == 0
            assert rig.tuis[0].over.is_set()
        assert [
            each for each in caplog.records if each.levelno >= logging.WARNING
        ] == []

    def test_the_socket_goes_with_the_run(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            assert control.exists()
        assert not control.exists()

    def test_nothing_listening_is_an_oserror(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _ = request(socket_address(tmp_path / "none.sock"), "state")

    def test_a_request_that_is_not_one_is_refused(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            raw = asyncio.run(_raw(control, b'{"op": "dance"}\n'))
            assert b'"ok": false' in raw
            assert b"unreadable request" in raw
            assert request(control, "state").ok


async def _raw(address: Path, line: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(address)
    writer.write(line)
    reply = await reader.readline()
    writer.close()
    return reply


class TestStateReporter:
    """The run's state is posted on a thread of its own, in order."""

    def test_each_state_is_posted_in_the_order_it_was_reported(self) -> None:
        posted: list[HostState] = []
        reporter = StateReporter(posted.append)
        reporter.start()
        told: tuple[HostState, ...] = ("running", "requires_action", "taken_over")
        for state in told:
            reporter.report(state)
        reporter.close()

        assert posted == ["running", "requires_action", "taken_over"]

    def test_a_post_that_fails_costs_only_itself(self) -> None:
        posted: list[HostState] = []

        def post(state: HostState) -> None:
            if state == "running":
                raise ConnectionError("down")
            posted.append(state)

        reporter = StateReporter(post)
        reporter.start()
        reporter.report("running")
        reporter.report("idle")
        reporter.close()

        assert posted == ["idle"]

    def test_the_posting_is_not_on_the_callers_thread(self) -> None:
        threads: list[str] = []

        def post(state: HostState) -> None:
            del state
            threads.append(threading.current_thread().name)

        reporter = StateReporter(post)
        reporter.start()
        reporter.report("idle")
        reporter.close()

        assert threads == ["run state"]

    def test_a_reporter_that_never_started_closes_at_once(self) -> None:
        StateReporter(lambda _state: None).close()

    # The server keeps a run's state on its lease alone: after the lease lapses, or the
    # server restarts, it reads the run as idle until the run says otherwise again.
    def test_a_state_other_than_idle_is_posted_again_while_it_holds(self) -> None:
        posted: list[HostState] = []
        thrice = threading.Event()

        def post(state: HostState) -> None:
            posted.append(state)
            if len(posted) >= 3:
                thrice.set()

        reporter = StateReporter(post, again_sec=0.001)
        reporter.start()
        reporter.report("taken_over")
        assert thrice.wait(_WAIT)
        reporter.close()

        assert set(posted) == {"taken_over"}

    def test_idle_is_not_posted_again(self) -> None:
        posted: list[HostState] = []
        reporter = StateReporter(posted.append, again_sec=0.001)
        reporter.start()
        reporter.report("idle")
        time.sleep(0.02)
        reporter.close()

        assert posted == ["idle"]


class TestAHungDriver:
    """A call that waits on a driver that never answers does not wait forever."""

    def test_the_caller_gives_up_with_a_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(headless, "_CALL_SEC", 0.05)
        with _Rig() as rig:
            rig.wait_started()
            rig.drivers[0].hang = True
            with pytest.raises(TimeoutError):
                rig.run.send_line("hello")

    def test_a_call_in_flight_when_the_run_ends_says_the_run_ended(self) -> None:
        caught: list[Exception] = []

        def call(rig: _Rig) -> None:
            try:
                rig.run.send_line("hello")
            except RunEndedError as error:
                caught.append(error)

        with _Rig() as rig:
            rig.wait_started()
            rig.drivers[0].hang = True
            caller = threading.Thread(target=call, args=(rig,), daemon=True)
            caller.start()
            assert rig.drivers[0].hanging.wait(_WAIT)
            rig.run.terminate()
            caller.join(_WAIT)
        assert [str(each) for each in caught] == ["the codex run ended"]


class TestARunThatIsStarting:
    """A call that comes while the driver starts has no driver to go to."""

    def test_it_is_told_so_and_not_that_the_chat_was_taken_over(self) -> None:
        driver = _FakeDriver()
        driver.hold_start = threading.Event()
        with _Rig(drivers=[driver]) as rig:
            assert driver.starting.wait(_WAIT)
            with pytest.raises(RunEndedError) as refused:
                rig.run.send_line("hello")
            assert str(refused.value) == "the codex run has no driver to take the call"
            driver.hold_start.set()
            rig.wait_started()


class TestAFreshRun:
    """What a run says before it has started."""

    def test_it_is_idle_with_no_session_and_nothing_to_end(self) -> None:
        run = _Rig().run

        run.terminate()

        assert (run.state, run.session_id) == ("idle", "")

    def test_a_call_before_the_run_started_says_it_is_not_running(self) -> None:
        with pytest.raises(RunEndedError) as raised:
            _Rig().run.interrupt()
        assert str(raised.value) == "the codex run is not running"


class TestTheTerminalsEdges:
    """A terminal that is silent, stuck or crashing costs the chat nothing."""

    def test_a_terminal_that_never_listens_is_ended_and_the_chat_handed_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(headless, "_TUI_START_SEC", 0.05)
        control = socket_address(tmp_path / "c.sock")
        drivers = [_FakeDriver(), _FakeDriver()]
        with _Rig(control=control, drivers=drivers, tui_mode="silent") as rig:
            rig.wait_started()
            reply = request(control, "takeover")
            assert (reply.ok, reply.reason) == (
                False,
                "codex did not start listening in time",
            )
            assert reply.state == "idle"
            assert rig.tuis[0].over.wait(_WAIT)

    def test_a_terminal_that_listens_a_moment_late_is_waited_for(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control, tui_mode="late") as rig:
            rig.wait_started()
            reply = request(control, "takeover")
            assert (reply.ok, reply.state) == (True, "taken_over")

    def test_a_terminal_that_fails_after_it_listened_says_so_and_the_chat_stays_taken(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        seen: list[str] = []

        def said() -> bool:
            seen.append(capsys.readouterr().err)
            return "[trax run] the codex terminal failed: pty died\n" in "".join(seen)

        with _Rig(control=control, tui_mode="crashes") as rig:
            rig.wait_started()
            assert request(control, "takeover").ok
            _wait_for(said)
            assert request(control, "state").state == "taken_over"

    def test_a_chat_handed_back_can_be_taken_over_again(self, tmp_path: Path) -> None:
        control = socket_address(tmp_path / "c.sock")
        drivers = [_FakeDriver(), _FakeDriver()]
        with _Rig(control=control, drivers=drivers, tui_exits=(3, None)) as rig:
            rig.wait_started()
            assert not request(control, "takeover").ok
            reply = request(control, "takeover")
            assert reply.ok
            assert reply.state == "taken_over"
            assert drivers[1].stopped
            assert len(rig.tuis) == 2
            assert rig.taken == ["s1"]


class TestTheControlSocketsEdges:
    """A request that is not one, a probe, and a run that never answers."""

    def test_a_line_that_is_not_a_request_is_answered_as_unreadable(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            raw = asyncio.run(_raw(control, b"not json\n"))
        assert json_loads(raw.decode(), Reply) == Reply(
            ok=False,
            reason="unreadable request",
        )

    def test_a_line_longer_than_the_reader_takes_is_unreadable_too(
        self,
        tmp_path: Path,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            raw = asyncio.run(_raw(control, b"x" * 100_000 + b"\n"))
            assert b"unreadable request" in raw
            assert request(control, "state").ok

    def test_a_probe_that_connects_and_leaves_costs_nothing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        control = socket_address(tmp_path / "c.sock")
        with _Rig(control=control) as rig:
            rig.wait_started()
            for _ in range(3):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(str(control))
                probe.close()
            assert request(control, "state").ok
        assert [
            each for each in caplog.records if each.levelno >= logging.WARNING
        ] == []

    def test_a_run_that_never_answers_is_given_up_on(self, tmp_path: Path) -> None:
        address = socket_address(tmp_path / "mute.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(address))
        listener.listen(1)
        try:
            with pytest.raises(TimeoutError):
                _ = request(address, "state", timeout_sec=0.02)
            with pytest.raises(TimeoutError):
                _ = say(address, "hi", timeout_sec=0.02)
        finally:
            listener.close()


class TestSignals:
    """A signal to the process ends the run, as ``trax run stop`` does."""

    @pytest.mark.parametrize(
        "number",
        [signal.SIGTERM, signal.SIGHUP, signal.SIGINT],
    )
    def test_a_signal_ends_the_run_with_its_number_and_the_handler_goes(
        self,
        number: signal.Signals,
    ) -> None:
        noted: list[int] = []
        # A handler of ours first, so a run that did not take the signal fails the
        # test and does not end the process running it.
        previous = signal.signal(number, lambda sent, _frame: noted.append(sent))
        rig = _Rig()
        try:
            status = rig.run.run(
                on_started=lambda: threading.Timer(
                    0.05,
                    os.kill,
                    args=(os.getpid(), number),
                ).start(),
            )
        finally:
            restored = signal.getsignal(number)
            _ = signal.signal(number, previous)
        assert noted == []
        assert status == 128 + number
        assert restored == (
            signal.default_int_handler if number == signal.SIGINT else signal.SIG_DFL
        )
        assert rig.drivers[0].stopped


class TestTheStateReportersEdges:
    """The reporter's thread, and what it does when a post fails or hangs."""

    def test_the_thread_is_a_daemon_named_for_what_it_does(self) -> None:
        reporter = StateReporter(lambda _state: None)
        reporter.start()
        try:
            [thread] = [
                each for each in threading.enumerate() if each.name == "run state"
            ]
            assert thread.daemon
        finally:
            reporter.close()

    def test_a_post_that_fails_is_logged_with_its_traceback(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def post(state: HostState) -> None:
            raise ConnectionError(state)

        reporter = StateReporter(post)
        reporter.start()
        reporter.report("running")
        reporter.close()

        [record] = caplog.records
        assert record.getMessage() == "trax run: could not report the run state"
        assert record.levelno == logging.WARNING
        assert isinstance(record.exc_info, tuple)

    def test_a_post_that_hangs_does_not_hold_the_run_past_the_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(headless, "_REPORT_END_SEC", 0.01)
        release = threading.Event()

        def post(state: HostState) -> None:
            del state
            _ = release.wait(1.0)

        reporter = StateReporter(post)
        reporter.start()
        reporter.report("running")
        began = time.monotonic()
        try:
            reporter.close()
            assert time.monotonic() - began < 0.5
        finally:
            release.set()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
