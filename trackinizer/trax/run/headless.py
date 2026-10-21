"""Run a chat's CLI headless, and hand it to a terminal on takeover.

A Chat run on a person's machine does not draw a TUI. :class:`HeadlessRun` drives the
CLI through its programmatic protocol (a :class:`~.asks.Driver`) and writes what the
driver reports as the records of the chat's session: a card when the agent asks, and its
end when the ask is answered or released. The transcript still comes from tailing the
CLI's own session file, as it does for a TUI run. The run's state goes to the server
beside its drains (:class:`StateReporter`), so Chat can show a badge.

A person who wants the CLI itself takes the run over (``trax run attach NAME``). The
run stops the driver and nothing else: it goes on reading the session file and
uploading it, so the chat keeps logging while the CLI's own TUI runs on the same
session, in a terminal this run hosts. Chat lines are refused meanwhile. Giving the
chat its CLI back is a new run: the server turns this one away, and a launch of the
same name resumes the session this one was taken over on.

The run listens on a control socket for the takeover and for ``trax run stop|send``.
One request is one JSON line each way, so a launcher or an attaching terminal needs no
event loop of its own: :func:`request`.
"""

from __future__ import annotations

from concurrent import futures
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

import asyncio
import contextlib
import json
import logging
import queue
import signal
import sys
import threading

from treekle import json_loads, to_plain

from trackinizer.trax.run import asks
from trackinizer.trax.run.asks import Driver


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine
    from pathlib import Path

    from trackinizer.lib.agent.types.sessions import SystemMessage
    from trackinizer.trax.run.asks import Answer


__all__ = [
    "HeadlessRun",
    "HostState",
    "Reply",
    "Request",
    "RunEndedError",
    "StateReporter",
    "TakenOverError",
    "Tui",
    "request",
    "say",
]


_logger = logging.getLogger(__name__)

type HostState = Literal["idle", "running", "requires_action", "taken_over"]
"""Where a headless run stands: a driver's three states, and a person having the CLI."""

type Op = Literal["state", "takeover", "send", "stop"]
"""What the control socket takes."""

_CALL_SEC: Final = 60.0
"""How long a call from another thread waits for the run's loop. A driver answers in
well under a second; a takeover also waits for the terminal to listen."""

_TUI_START_SEC: Final = 30.0
"""How long a terminal gets to start listening for a person to attach to it."""

_REPORT_END_SEC: Final = 5.0
"""How long the run waits for its last state report to be posted when it ends."""

_REPORT_AGAIN_SEC: Final = 15.0
"""How often a state other than idle is posted again. The server keeps it on the run's
lease, which lapses after 45 seconds without a drain and is lost when the server
restarts; either way the run reads as idle until it is told again."""

_DRIVER_ERRORS: Final = (OSError, RuntimeError)
"""What a driver or a terminal raises when it cannot start: a missing binary or a dropped
pipe (``OSError``, which holds ``ConnectionError`` and ``TimeoutError``), or a refusal
(``RuntimeError``, which codex's ``CodexAppError`` is)."""


class TakenOverError(RuntimeError):
    """The chat is taken over: a terminal has the CLI, and a line has nowhere to go."""


class RunEndedError(RuntimeError):
    """The run is not there to take the call: it has not started or has ended."""


class Tui(Protocol):
    """The CLI's own terminal, hosted by the run while a person has taken it over."""

    def run(self) -> int:
        """Serve the terminal until its CLI exits; return the status. Blocks."""
        ...

    def terminate(self) -> None:
        """End the CLI; safe to call repeatedly."""
        ...

    def submit(self, text: str) -> None:
        """Type ``text`` into the CLI and press Enter."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    """One line to the control socket."""

    op: Op
    text: str = ""
    """What ``send`` types."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Reply:
    """The one line the control socket answers with."""

    ok: bool
    reason: str = ""
    """Why not, for a person to read; empty when ``ok``."""

    state: str = ""
    cli: str = ""
    session: str = ""
    """The CLI's own id for the session, which resumes it."""


_UNREADABLE: Final = Reply(ok=False, reason="unreadable request")


def request(
    address: Path,
    op: Literal["state", "takeover", "stop"],
    *,
    timeout_sec: float = _CALL_SEC,
) -> Reply:
    """Ask the run listening at ``address`` to do ``op``; wait for its answer.

    Args:
      address: The run's control socket.
      op: What to do.
      timeout_sec: How long to wait for the answer.

    Returns:
      reply: The run's answer.

    Raises:
      OSError: Nothing listens at ``address``.
      TimeoutError: The run did not answer in time.

    """
    return asyncio.run(_ask(address, Request(op=op), timeout_sec))


def say(address: Path, text: str, *, timeout_sec: float = _CALL_SEC) -> Reply:
    """Type ``text`` into the run listening at ``address``; wait for its answer.

    Args:
      address: The run's control socket.
      text: The line. It goes to the terminal that has the CLI, else to the driver.
      timeout_sec: How long to wait for the answer.

    Returns:
      reply: The run's answer.

    Raises:
      OSError: Nothing listens at ``address``.
      TimeoutError: The run did not answer in time.

    """
    return asyncio.run(_ask(address, Request(op="send", text=text), timeout_sec))


class StateReporter:
    """Posts a run's state on a thread of its own, in the order it was reported.

    A report waits on the server, which the run's loop must not. A state other than
    idle is posted again every ``again_sec`` while it holds: the server forgets it when
    the run's lease lapses or the server restarts, and a run taken over that reads as
    idle would be sent lines it cannot take, with no Reattach offered.

    Args:
      post: Sends one state to the server; a failure is logged, and the next report
        goes on.
      again_sec: How long a state other than idle goes before it is posted again.

    """

    def __init__(
        self,
        post: Callable[[HostState], None],
        *,
        again_sec: float = _REPORT_AGAIN_SEC,
    ) -> None:
        self._post = post
        self._again_sec = again_sec
        self._states = queue.SimpleQueue[HostState | None]()
        self._thread = threading.Thread(
            name="run state",
            target=self._work,
            daemon=True,
        )

    def start(self) -> None:
        """Start posting."""
        self._thread.start()

    def report(self, state: HostState) -> None:
        """Queue ``state`` to be posted. Safe from any thread.

        Args:
          state: Where the run stands now.

        """
        self._states.put(state)

    def close(self) -> None:
        """Post what is queued, then stop. A reporter never started has nothing to stop."""
        self._states.put(None)
        if self._thread.ident is not None:
            self._thread.join(_REPORT_END_SEC)

    def _work(self) -> None:
        """Post each state until the end marker, and again one that is not idle."""
        held: HostState = "idle"
        while True:
            try:
                state = self._states.get(timeout=self._again_sec)
            except queue.Empty:
                if held == "idle":
                    continue
                state = held
            if state is None:
                return
            held = state
            try:
                self._post(state)
            except Exception:
                _logger.warning(
                    "trax run: could not report the run state",
                    exc_info=True,
                )


class HeadlessRun:
    """One chat's CLI run on a driver, which a terminal may take over.

    The loop runs on the thread that calls :meth:`run`; every other method is safe to
    call from any other thread, as the poll thread does.

    Args:
      cli: The CLI the driver drives, for what it says to a person.
      cwd: The folder the CLI works in.
      resume: The CLI session to resume; empty starts a new one.
      model: The model to run; empty keeps the CLI's own.
      effort: The reasoning effort; empty keeps the CLI's own.
      make_driver: Builds a driver. A driver runs once, so a terminal that cannot start
        hands the chat back to another.
      make_tui: Builds the terminal for a takeover, given the ``session_id`` to continue
        and the ``listening`` call that says it is up. It raises when the CLI has no
        session to take over.
      record: Takes each record the run writes; it must not block.
      on_session: Told the CLI's session id each time a driver starts.
      on_listening: Told once the run is up, which is what a launcher waits for.
      on_state: Told each change of the run's state; it must not block.
      on_takeover: Told the CLI's session id once a terminal has it, which is the
        session a launch of the same name resumes.
      control: Where to listen for the control requests; ``None`` for none.

    """

    def __init__(
        self,
        *,
        cli: str,
        cwd: Path,
        resume: str,
        model: str,
        effort: str,
        make_driver: Callable[[], Driver],
        make_tui: Callable[..., Tui],
        record: Callable[[SystemMessage], None],
        on_session: Callable[[str], None],
        on_listening: Callable[[], None] | None = None,
        on_state: Callable[[HostState], None] | None = None,
        on_takeover: Callable[[str], None] | None = None,
        control: Path | None = None,
    ) -> None:
        self._cli = cli
        self._cwd = cwd
        self._resume = resume
        self._model = model
        self._effort = effort
        self._make_driver = make_driver
        self._make_tui = make_tui
        self._record = record
        self._on_session = on_session
        self._on_listening = on_listening
        self._on_state = on_state
        self._on_takeover = on_takeover
        self._control = control
        self._state: HostState = "idle"
        self._session_id = ""
        self._live: _Live | None = None
        self._terminal: _Open | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # The status the run ends with: the first one put here.
        self._done = asyncio.Queue[int]()

    @property
    def state(self) -> HostState:
        """Where the run stands."""
        return self._state

    @property
    def session_id(self) -> str:
        """The CLI's own id for the session; empty before the driver starts."""
        return self._session_id

    def run(self, *, on_started: Callable[[], None] | None = None) -> int:
        """Start the driver and serve until the run ends. Blocks.

        Args:
          on_started: Called once the driver is up, on this thread, before the run
            waits; the caller starts whatever feeds the run lines.

        Returns:
          status: 0 when the run was stopped; 1 when the driver could not start or
            ended by itself; ``128 + signal`` when the process was signalled.

        """
        return asyncio.run(self._serve(on_started))

    def terminate(self) -> None:
        """End the run: the driver and any terminal stop. Safe from any thread."""
        loop = self._loop
        if loop is None:
            return
        # The loop is closed once the run has ended, and there is then nothing to end.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._finish, 0)

    def send_line(self, text: str) -> None:
        """Send the person's line to the CLI.

        Args:
          text: The line, as it should reach the model.

        Raises:
          TakenOverError: A terminal has the CLI.
          RunEndedError: The run is not there.

        """
        self._call(self._to_driver(lambda driver: driver.send_line(text)))

    def command(self, verb: str, args: str) -> None:
        """Run a slash command such as ``/compact``.

        Args:
          verb: The command, with or without its slash.
          args: What follows it.

        Raises:
          TakenOverError: A terminal has the CLI.
          RunEndedError: The run is not there.
          ValueError: The driver has no such command.

        """
        self._call(self._to_driver(lambda driver: driver.command(verb, args)))

    def answer(self, answer: Answer) -> None:
        """Hand the CLI the person's choice on an open ask.

        Args:
          answer: The choice.

        Raises:
          TakenOverError: A terminal has the CLI.
          RunEndedError: The run is not there.
          ValueError: The ask offers no such option.

        """
        self._call(self._to_driver(lambda driver: driver.answer(answer)))

    def interrupt(self) -> None:
        """Stop the turn in progress; the session stays.

        Raises:
          TakenOverError: A terminal has the CLI.
          RunEndedError: The run is not there.

        """
        self._call(self._to_driver(lambda driver: driver.interrupt()))

    def settings(self, *, model: str, effort: str) -> None:
        """Change the model and effort for the turns that follow.

        Args:
          model: The model; empty leaves it.
          effort: The reasoning effort; empty leaves it.

        Raises:
          TakenOverError: A terminal has the CLI.
          RunEndedError: The run is not there.

        """
        self._call(
            self._to_driver(lambda driver: driver.settings(model=model, effort=effort)),
        )

    async def _serve(self, on_started: Callable[[], None] | None) -> int:
        """Start the driver, listen, and wait for the end."""
        self._loop = asyncio.get_running_loop()
        try:
            await self._begin(self._resume)
        except _DRIVER_ERRORS as error:
            sys.stderr.write(
                f"[trax run] could not start {self._cli} headless: {error}\n",
            )
            return 1
        # Listening only now: a request that came while the driver started would find
        # no driver, and say the chat was taken over.
        async with self._listen():
            try:
                if self._on_listening is not None:
                    self._on_listening()
                if on_started is not None:
                    on_started()
                return await self._done.get()
            finally:
                await self._close_tui()
                await self._end_driver()

    # A host has no controlling terminal, so HUP and INT arrive only when someone sends
    # them, and each means the same as TERM. Only the main thread may install the
    # handlers; a run on another thread is stopped through :meth:`terminate`.
    @contextlib.asynccontextmanager
    async def _listen(self) -> AsyncGenerator[None]:
        """Take control requests and signals for as long as the block runs."""
        loop = asyncio.get_running_loop()
        async with contextlib.AsyncExitStack() as stack:
            try:
                for each in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    loop.add_signal_handler(each, self._finish, 128 + each)
                    stack.callback(loop.remove_signal_handler, each)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            if self._control is not None:
                server = await asyncio.start_unix_server(
                    self._serve_control,
                    path=self._control,
                )
                # Before 3.13 ``close`` leaves the path behind, and a stale socket file
                # reads as a run that is listening until a connect fails.
                stack.callback(self._control.unlink, missing_ok=True)
                stack.push_async_callback(server.wait_closed)
                stack.callback(server.close)
            yield

    def _finish(self, status: int) -> None:
        """End the run with ``status``, unless it has already ended."""
        if self._done.empty():
            self._done.put_nowait(status)

    async def _begin(self, resume: str) -> None:
        """Start a driver on ``resume`` and pump what it reports."""
        driver = self._make_driver()
        self._session_id = await driver.start(
            cwd=self._cwd,
            resume=resume,
            model=self._model,
            effort=self._effort,
        )
        pump = asyncio.create_task(self._events(driver))
        self._live = _Live(driver=driver, pump=pump)
        self._on_session(self._session_id)

    async def _end_driver(self) -> None:
        """Stop the driver, if one runs, and write the last of what it reports first."""
        live, self._live = self._live, None
        if live is not None:
            await live.driver.stop()
            await live.pump

    async def _events(self, driver: Driver) -> None:
        """Write each event the driver reports; end the run if it ends alone."""
        async for event in driver.events():
            match event:
                case asks.Ask():
                    self._record(asks.opened_record(event, at=_now()))
                case asks.Resolved():
                    self._record(asks.resolved_record(event, at=_now()))
                case asks.StateChanged():
                    self._set_state(event.state)
        if self._live is not None and self._live.driver is driver:
            self._finish(1)

    def _set_state(self, state: HostState) -> None:
        """Move to ``state``, and say so if it is a change."""
        before, self._state = self._state, state
        if state != before and self._on_state is not None:
            self._on_state(state)

    def _call[T](self, work: Coroutine[None, None, T]) -> T:
        """Run ``work`` on the run's loop from another thread; wait for it."""
        loop = self._loop
        if loop is None or loop.is_closed():
            work.close()
            raise RunEndedError(f"the {self._cli} run is not running")
        try:
            return asyncio.run_coroutine_threadsafe(work, loop).result(_CALL_SEC)
        except futures.CancelledError as error:
            raise RunEndedError(f"the {self._cli} run ended") from error

    async def _to_driver(
        self,
        send: Callable[[Driver], Coroutine[None, None, None]],
    ) -> None:
        """Hand ``send`` the driver, unless a terminal has the CLI."""
        if self._state == "taken_over":
            raise TakenOverError(
                "taken over in a terminal; reattach from Chat or the Console",
            )
        if self._live is None:
            raise RunEndedError(f"the {self._cli} run has no driver to take the call")
        await send(self._live.driver)

    async def _serve_control(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Answer one request line with one reply line."""
        try:
            line = await reader.readline()
            # A liveness probe connects and leaves without a word.
            if line:
                answer = await self._answer(json_loads(line.decode(), Request))
                writer.write(json.dumps(to_plain(answer)).encode() + b"\n")
        except ValueError:
            writer.write(json.dumps(to_plain(_UNREADABLE)).encode() + b"\n")
        finally:
            writer.close()

    async def _answer(self, asked: Request) -> Reply:
        """Do what the request asks; say why when it cannot be done."""
        try:
            return await self._dispatch(asked)
        except Exception as error:
            _logger.warning("trax run: a control request failed", exc_info=True)
            return Reply(ok=False, reason=str(error) or type(error).__name__)

    async def _dispatch(self, asked: Request) -> Reply:
        """Do what the request asks."""
        match asked.op:
            case "state":
                return self._reply()
            case "takeover":
                return await self._takeover()
            case "send":
                return await self._send(asked.text)
            case "stop":
                self._finish(0)
                return self._reply()

    def _reply(self) -> Reply:
        """Return the answer that says where the run stands."""
        return Reply(
            ok=True,
            state=self._state,
            cli=self._cli,
            session=self._session_id,
        )

    async def _send(self, text: str) -> Reply:
        """Type a line into the terminal that has the CLI, else give it to the driver."""
        try:
            if self._terminal is not None:
                await asyncio.to_thread(self._terminal.tui.submit, text)
            else:
                await self._to_driver(lambda driver: driver.send_line(text))
        except (TakenOverError, OSError, ValueError) as error:
            return Reply(ok=False, reason=str(error))
        return self._reply()

    async def _takeover(self) -> Reply:
        """Stop the driver, then host the CLI's terminal on the same session."""
        await self._end_driver()
        self._set_state("taken_over")
        if self._terminal is None:
            failure = await self._open_tui()
            if failure:
                return await self._hand_back(failure)
            if self._on_takeover is not None:
                self._on_takeover(self._session_id)
        return self._reply()

    async def _open_tui(self) -> str:
        """Start the terminal; return why it did not listen, or nothing."""
        listening = threading.Event()
        try:
            tui = self._make_tui(session_id=self._session_id, listening=listening.set)
        except _DRIVER_ERRORS as error:
            return str(error)
        work = asyncio.get_running_loop().run_in_executor(None, tui.run)
        heard = asyncio.ensure_future(asyncio.to_thread(listening.wait, _TUI_START_SEC))
        _ = await asyncio.wait({work, heard}, return_when=asyncio.FIRST_COMPLETED)
        started = listening.is_set()
        # Releases the thread that waits for a terminal that never listened.
        listening.set()
        if not started:
            return await self._reap_tui(tui, work)
        self._terminal = _Open(tui=tui, work=work)
        work.add_done_callback(self._tui_ended)
        return ""

    async def _reap_tui(self, tui: Tui, work: asyncio.Future[int]) -> str:
        """End a terminal that did not listen; return why it did not."""
        silent = not work.done()
        tui.terminate()
        try:
            status = await work
        except _DRIVER_ERRORS as error:
            return f"{self._cli} could not start: {error}"
        if silent:
            return f"{self._cli} did not start listening in time"
        return f"{self._cli} exited with status {status} before it could be attached"

    # The chat stays taken over.
    def _tui_ended(self, work: asyncio.Future[int]) -> None:
        """Forget a terminal whose CLI exited, and say if it failed."""
        if self._terminal is not None and self._terminal.work is work:
            self._terminal = None
        if (error := work.exception()) is not None:
            sys.stderr.write(f"[trax run] the {self._cli} terminal failed: {error}\n")

    # Not waited for here: the loop's shutdown waits for the thread that serves it.
    async def _close_tui(self) -> None:
        """End the terminal, if one is open."""
        opened, self._terminal = self._terminal, None
        if opened is not None:
            opened.tui.terminate()

    async def _hand_back(self, why: str) -> Reply:
        """Give the chat its CLI back, because the terminal could not start for ``why``."""
        try:
            await self._begin(self._session_id)
        except _DRIVER_ERRORS as error:
            return Reply(
                ok=False,
                reason=f"{why}; {self._cli} could not restart either: {error}",
                state=self._state,
            )
        self._set_state("idle")
        return Reply(ok=False, reason=why, state=self._state)


def _now() -> datetime:
    """Return the current time, in UTC."""
    return datetime.now(UTC)


async def _ask(address: Path, asked: Request, timeout_sec: float) -> Reply:
    """Write one request line and read the one reply line."""
    reader, writer = await asyncio.open_unix_connection(address)
    try:
        writer.write(json.dumps(to_plain(asked)).encode() + b"\n")
        async with asyncio.timeout(timeout_sec):
            line = await reader.readline()
    finally:
        writer.close()
    return json_loads(line.decode(), Reply)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Live:
    """A driver that runs, and the task that writes what it reports."""

    driver: Driver
    pump: asyncio.Task[None]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Open:
    """A terminal that listens, and the thread future that serves it."""

    tui: Tui
    work: asyncio.Future[int]
