"""The live session relay: an agent process's lines out to its viewers, their inputs in.

A session is live while an agent process holds a socket here. The relay passes each
line the process sends to every viewer as it arrives, keeps the last state, the last
settings and the reply while it is being written, so a viewer that opens late gets
them, and hands the process the inputs people post. A reply once finished is a record,
which a viewer reads with the records, so it is kept no longer. It also offers each
start to the daemon of the host it is for, which holds :meth:`SessionRelay.wait` and is
answered the moment the start is asked.

It holds nothing durable and reads no table itself: its state is in memory, and a
server restart drops it, after which processes reconnect and viewers reopen. A
session nothing of which is live any more, and which no viewer watches, it forgets
when told which processes still hold a live key (:meth:`SessionRelay.forget_idle`). The
rules the routes hand it (:data:`Allowed`, :data:`Dropped`) may ask the database.
The routes (``api/session_relay_routes.py``) check who may do what, mint and revoke
keys, and call it. It never reads an event's payload. It keeps the key each process
holds its socket with, which is how a message the process posts is known to come
from its session (:meth:`SessionRelay.socket_key`).

A stream or a socket can stay open long after the check that let it open, so each
viewer and each process socket brings that check along (:data:`Allowed`), and each
:meth:`SessionRelay.sweep` asks it again: a viewer whose rule no longer holds has its
stream ended, and a process its socket closed as a refused one is.

Who holds a session is decided by each process's :class:`Hello`:

- a ``Hello`` naming the session's own process, ``starting`` or ``detached`` because
  its socket dropped, attaches it; one naming it while ``attached`` replaces its old
  socket, which is closed without a code;
- a ``Hello`` naming another process is refused with ``CLOSE_TAKEN_OVER`` while the
  session's process is ``starting`` or ``attached``, and attaches as a process a person
  started once it is ``detached``;
- a ``Hello`` naming the session's own process after it was stopped or ended is closed
  with ``CLOSE_ENDED``: it has nothing left to serve.

A start is not lost. A wait asks whether its caller is still there just before it
takes a start, so a daemon that died while its wait was held takes none, and the next
wait on that host does. A start taken is still lost if its answer is, or its daemon
dies before running it: the route then has :meth:`SessionRelay.give_up` detach it as
``not started`` once it has had its time, and it is never offered again. A start no
daemon takes, its host's daemon away, is given up on the same way once it has had
its own time, as ``its host did not take it``.

Typical usage example:

  relay = SessionRelay()
  process = relay.start(
      session_id, spec, process_id=p, host_id=h, host="lab", token=t, remote=False
  )
  serve = await relay.wait(h, wait_sec=25, dropped=request.is_disconnected)
  relay.give_up(session_id, process_id=p, taken=True)  # later: never answered
  await relay.serve_process(
      session_id, socket, hello, host_id=h, allowed=still_writes, key=k
  )
  await relay.sweep()  # on a timer: end what its rule no longer lets through
  relay.forget_idle(keyed=live_keys)  # then forget what nothing of is live
  relay.socket_key(session_id)  # k, while that socket is attached
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

import asyncio
import logging

import pydantic

from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_session_relay import (
    MAX_FRAME_BYTES,
    AgentProcess,
    AgentState,
    EventLine,
    Hello,
    Input,
    Opened,
    ProcessFrame,
    ProcessLine,
    RecordLine,
    ServeSession,
    Setting,
    StateLine,
    ViewerFrame,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    from trackinizer.wire.wire_session_ir import RecordBody
    from trackinizer.wire.wire_session_relay import (
        NewInput,
        ProcessEnded,
        ProcessSpec,
    )
    from trackinizer.wire.wire_sessions import MessageKind


__all__ = [
    "BAD_FRAME",
    "NOT_STARTED",
    "NOT_TAKEN",
    "Allowed",
    "Attached",
    "Dropped",
    "HostRevokedError",
    "InputRefusedError",
    "NotLiveError",
    "ProcessSocket",
    "SessionRelay",
    "process_frame",
]

_logger = logging.getLogger(__name__)

BAD_FRAME: Final = 1008
"""The close code of a process socket that sent what the wire does not allow."""

NOT_STARTED: Final = "not started"
"""Why a start taken and followed by neither its ``Hello`` nor its daemon's report is
detached."""

NOT_TAKEN: Final = "its host did not take it"
"""Why a start no daemon took in its time is detached: its host's daemon is away."""

type Allowed = Callable[[], Awaitable[bool]]
"""Asks again the whole rule that let a viewer's stream or a process's socket open:
whether it still holds."""

type Dropped = Callable[[], Awaitable[bool]]
"""Asks whether the caller a wait is held for has gone, as a request's own
disconnect check does."""

type Attached = Callable[[], Awaitable[None]]
"""What a process's socket has done while the relay holds it as the session's process,
such as telling the reaper the session is alive: a socket the relay refuses does
none of it."""


async def _nothing() -> None:
    """Do nothing while a socket is attached."""


_PROCESS_FRAMES: Final = pydantic.TypeAdapter[ProcessFrame](ProcessFrame)
"""Reads one frame a process sends."""


class NotLiveError(Exception):
    """An input was sent to a session that no agent process serves."""


class InputRefusedError(Exception):
    """An input the relay will not hold for the session's process; its text says why."""


class HostRevokedError(Exception):
    """The host was revoked while its daemon waited for a start."""


class ProcessSocket(Protocol):
    """An agent process's socket, as the relay reads, writes and closes it."""

    async def receive(self) -> str | bytes | int:
        """Return the next text frame, a binary frame, or the code the peer closed with."""
        ...

    async def send_text(self, text: str) -> None:
        """Send one text frame; raise if the peer has gone."""
        ...

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close with ``code`` and ``reason``; closing a closed socket is a no-op."""
        ...


def process_frame(received: str | bytes | int) -> ProcessFrame | None:
    """Return the frame a process sent, or ``None`` for one the wire does not allow.

    Args:
      received: One read of the process's socket.

    Returns:
      frame: The frame; ``None`` for a binary frame, a close, a text frame over
        :data:`MAX_FRAME_BYTES`, or one that is not a :data:`ProcessFrame`.

    """
    if not isinstance(received, str) or len(received.encode()) > MAX_FRAME_BYTES:
        return None
    try:
        return _PROCESS_FRAMES.validate_json(received)
    except pydantic.ValidationError:
        return None


class SessionRelay:
    """Passes a live session's lines to its viewers and their inputs to its process.

    Args:
      kept_bytes: The most of the reply being written that is kept for a viewer that
        opens late, counted as each event line's bytes; past it none is kept until
        the next model call.
      viewer_lag: The most lines a viewer may fall behind; past it, it is closed.
      held_inputs: The most inputs held for a process that is still starting.
      input_bytes: The most bytes of a session's inputs its process has not been sent
        yet, held while it starts or waiting on its socket, each at most one frame:
        8 MiB, four frames, room for a few lines with a file each, sent while an agent
        starts or works. Counted by inputs alone, 256 frames (512 MiB) could wait.

    """

    def __init__(
        self,
        *,
        kept_bytes: int = 2 << 20,
        viewer_lag: int = 1024,
        held_inputs: int = 256,
        input_bytes: int = 4 * MAX_FRAME_BYTES,
    ) -> None:
        self._kept_bytes = kept_bytes
        self._viewer_lag = viewer_lag
        self._held_inputs = held_inputs
        self._input_bytes = input_bytes
        self._sessions: dict[UUID, _Session] = {}
        self._offers: dict[UUID, deque[ServeSession]] = {}
        self._waiters: dict[UUID, list[asyncio.Future[bool]]] = {}

    async def serve_process(
        self,
        session_id: UUID,
        socket: ProcessSocket,
        hello: Hello,
        *,
        host_id: UUID | None,
        allowed: Allowed,
        key: UUID | None = None,
        attached: Attached = _nothing,
    ) -> None:
        """Serve an agent process's socket until it closes, however it closes.

        Args:
          session_id: The session the process serves.
          socket: Its socket, its ``Hello`` already read.
          hello: The first frame it sent.
          host_id: The host the process is known by, or ``None``; read only for a
            process the relay does not know.
          allowed: The rule its key was let in by, which each :meth:`sweep` asks again.
          key: The id of the API key it opened its socket with, which
            :meth:`socket_key` tells; ``None`` for none.
          attached: What runs while the socket is the session's process's: started
            once the relay has taken it, cancelled as it ends; a socket refused never
            starts it.

        """
        live = self._sessions.setdefault(session_id, _Session())
        refused = _refusal(live.process, hello)
        if refused is not None:
            _logger.info(
                "process refused: session=%s process=%s code=%d",
                session_id,
                hello.process_id,
                refused[0],
            )
            await socket.close(*refused)
            return
        connection = self._attach(
            session_id,
            live,
            hello,
            host_id=host_id,
            connection=_Connection(socket=socket, allowed=allowed, key=key),
        )
        reading = asyncio.ensure_future(self._read(session_id, live, connection))
        writing = asyncio.ensure_future(_write(connection))
        alive = asyncio.ensure_future(attached())
        try:
            _ = await asyncio.wait(
                {reading, writing, connection.ended},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (reading, writing, alive):
                _ = task.cancel()
            # The code the process closed with, when it was the one that closed; else
            # ``None``, or what stopped the read.
            read, _, lived = await asyncio.gather(
                reading,
                writing,
                alive,
                return_exceptions=True,
            )
            if isinstance(lived, Exception):
                _logger.warning(
                    "work while attached failed: session=%s",
                    session_id,
                    exc_info=lived,
                )
            if live.connection is connection:
                self._detach(
                    session_id,
                    live,
                    reason="exited" if read == _NORMAL_CLOSURE else "disconnected",
                )
            ending = connection.ended.result() if connection.ended.done() else None
            await (socket.close() if ending is None else socket.close(*ending))

    async def lines(
        self,
        session_id: UUID,
        *,
        allowed: Allowed,
    ) -> AsyncGenerator[ViewerFrame]:
        """Yield where a session stands, then each line as it arrives.

        Args:
          session_id: The session to watch.
          allowed: The rule the viewer was let in by, which each :meth:`sweep` asks
            again.

        Yields:
          frame: An :class:`Opened` with the last state, the process and the last
            settings, then the event lines of a reply still being written, if one is,
            then each line live. It ends when the viewer falls ``viewer_lag`` lines
            behind, or a sweep finds its rule no longer holds.

        """
        live = self._sessions.setdefault(session_id, _Session())
        viewer = _Viewer(allowed=allowed)
        live.viewers.add(viewer)
        # Taken now, with the viewer added in the same step, so no line is both kept
        # and queued for it, and none falls between the two.
        opened = Opened(state=live.state, process=live.process, settings=live.settings)
        kept = list(live.kept)
        try:
            yield opened
            for line in kept:
                yield line
            while True:
                while viewer.lines:
                    yield viewer.lines.popleft()
                if viewer.dropped:
                    return
                viewer.woken.clear()
                _ = await viewer.woken.wait()
        finally:
            live.viewers.discard(viewer)
            self._forget_if_idle(session_id, live)

    def watched(self, session_id: UUID) -> bool:
        """Return whether a viewer follows ``session_id`` now, which records go to.

        Args:
          session_id: The session.

        Returns:
          watched: Whether :meth:`publish_records` would pass a record to anyone.

        """
        live = self._sessions.get(session_id)
        return live is not None and bool(live.viewers)

    def publish_records(
        self,
        session_id: UUID,
        *,
        part: int,
        records: Sequence[RecordBody],
    ) -> None:
        """Pass records just stored to every viewer of the session; none is kept.

        Args:
          session_id: The session the records were stored in.
          part: The number of the part that holds them.
          records: The records, as ``GET .../records`` returns them.

        """
        live = self._sessions.get(session_id)
        if live is None:
            return
        for record in records:
            self._fan_out(live, RecordLine(part=part, record=record))

    def send_input(
        self,
        session_id: UUID,
        new: NewInput,
        *,
        author: str,
        recipient: str = "",
        booked: MessageKind | None = None,
    ) -> UUID:
        """Send ``new`` to the agent process serving ``session_id``.

        Passed to the process while its socket is open, and held while it is still
        starting, up to ``held_inputs``; either way, up to ``input_bytes`` of inputs
        not sent yet. It is never sent twice.

        Args:
          session_id: The session whose agent the input is for.
          new: A line, a stop or a slash line.
          author: The sender, which the caller attested: a person's email, or
            ``label#number`` for an agent of another session.
          recipient: The agent it is for, by its label there; ``""`` for the root.
          booked: How the sending agent's team booked it; ``None`` from a person.

        Returns:
          input_id: The id the process tells the input's effect back under.

        Raises:
          NotLiveError: No agent process serves the session.
          InputRefusedError: The input is larger than a frame, which the process
            would not read; or with it the inputs not sent yet would pass
            ``input_bytes``, or a starting process's pass ``held_inputs``.

        """
        sent = Input(
            id=new.id,
            author=author,
            kind=new.kind,
            payload=new.payload,
            recipient=recipient,
            booked=booked,
        )
        text = sent.model_dump_json()
        unsent = _Unsent(text=text, size=len(text.encode()))
        if unsent.size > MAX_FRAME_BYTES:
            raise InputRefusedError(
                f"an input is at most {MAX_FRAME_BYTES} bytes; this one is "
                f"{unsent.size}",
            )
        live = self._sessions.get(session_id)
        if live is not None and live.connection is not None:
            self._admit(live.connection.unsent, unsent)
            live.connection.unsent += unsent.size
            live.connection.outbox.put_nowait(unsent)
            return sent.id
        if live is None or live.process is None or live.process.state != "starting":
            raise NotLiveError(f"no agent process serves session {session_id}")
        if len(live.held) >= self._held_inputs:
            raise InputRefusedError(
                f"the agent is starting and holds {len(live.held)} inputs already; "
                "send this once it has started",
            )
        self._admit(sum(held.size for held in live.held), unsent)
        live.held.append(unsent)
        return sent.id

    def start(
        self,
        session_id: UUID,
        spec: ProcessSpec,
        *,
        process_id: UUID,
        host_id: UUID,
        host: str,
        token: str,
        remote: bool,
    ) -> AgentProcess:
        """Make a session's process ``starting``, and offer it to its host's daemon.

        Args:
          session_id: The session to serve.
          spec: The start as asked; its ``host`` is replaced by ``host``.
          process_id: The new process's id.
          host_id: The host the route found to run it.
          host: That host's name.
          token: The key minted for the process, handed to it whole.
          remote: Whether the route placed it with a remote agent service, which the
            offer says, so a daemon that confines nothing refuses it.

        Returns:
          process: The new process, ``starting``; the one already ``starting`` or
            ``attached`` when there is one, which this leaves as it is.

        """
        live = self._sessions.setdefault(session_id, _Session())
        if live.process is not None and live.process.state != "detached":
            return live.process
        offer = ServeSession(
            session_id=session_id,
            process_id=process_id,
            token=token,
            spec=spec.model_copy(update={"host": host}),
            remote=remote,
        )
        live.offer = offer
        live.host_id = host_id
        live.held.clear()
        self._offers.setdefault(host_id, deque()).append(offer)
        process = AgentProcess(
            session_id=session_id,
            process_id=process_id,
            host=host,
            login_host=spec.login_host,
            state="starting",
            since=_now(),
        )
        self._change(live, process)
        self._wake(host_id)
        return process

    def socket_key(self, session_id: UUID) -> UUID | None:
        """Return the key the session's attached process holds its socket with.

        Args:
          session_id: The session.

        Returns:
          key: The id of the API key it opened its socket with; ``None`` while no
            process is attached, or for one that named no key.

        """
        live = self._sessions.get(session_id)
        connection = None if live is None else live.connection
        return None if connection is None else connection.key

    def process(self, session_id: UUID) -> AgentProcess | None:
        """Return the process serving a session, or the last that did.

        Args:
          session_id: The session.

        Returns:
          process: The process; ``None`` for a session that has had none here.

        """
        live = self._sessions.get(session_id)
        return None if live is None else live.process

    def live_process_ids(self) -> frozenset[UUID]:
        """Return the id of each process ``starting`` or ``attached``, on any host.

        Returns:
          process_ids: What a key minted for a process may still be held by.

        """
        return frozenset(
            live.process.process_id
            for live in self._sessions.values()
            if live.process is not None and live.process.state != "detached"
        )

    def processes(self, host_id: UUID) -> list[AgentProcess]:
        """Return the processes of a host that may still run, each naming its session.

        Args:
          host_id: The host.

        Returns:
          processes: Each process ``starting``, ``attached``, or detached by a drop,
            that the host was offered or its ``Hello`` was known by: what
            :meth:`stop_host` would stop.

        """
        return [
            live.process
            for live in self._sessions.values()
            if live.host_id == host_id
            and live.process is not None
            and _stoppable(live.process)
        ]

    async def stop(self, session_id: UUID) -> AgentProcess | None:
        """Stop a session's process: close its socket, or forget an unanswered start.

        A process that already ended is left as it is. One whose socket dropped is
        stopped too, so it is told to end if it comes back.

        Args:
          session_id: The session.

        Returns:
          process: The process, ``detached``; ``None`` for a session that has had none.

        """
        live = self._sessions.get(session_id)
        if live is None or live.process is None:
            return None
        if not _stoppable(live.process):
            return live.process
        # The one it detached: a start may replace ``live.process`` while the
        # socket closes, and the caller revokes the key of the process returned.
        return await self._end(
            session_id,
            live,
            reason="stopped",
            closing=(wire_terminals.CLOSE_ENDED, "a person stopped this process"),
        )

    async def wait(
        self,
        host_id: UUID,
        *,
        wait_sec: float,
        dropped: Dropped,
    ) -> ServeSession | None:
        """Return the host's next start, holding up to ``wait_sec`` for one.

        A start is handed out once, and only to a caller still there to take it.

        Args:
          host_id: The host whose daemon asks.
          wait_sec: How long to hold.
          dropped: Says whether the caller has gone; asked only with a start to
            take, just before taking it.

        Returns:
          serve: The oldest start offered to the host; ``None`` when none came in time,
            or the caller has gone.

        Raises:
          HostRevokedError: The host was revoked while its daemon waited.

        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_sec
        while True:
            if host_id in self._offers:
                if await dropped():
                    return None
                # Nothing is awaited between the answer and the take, so a start goes
                # to no caller that left after it was asked. Another wait may have
                # taken it while this one asked.
                if (offer := self._take(host_id)) is not None:
                    return offer
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            waiter = loop.create_future()
            self._waiters.setdefault(host_id, []).append(waiter)
            try:
                _ = await asyncio.wait({waiter}, timeout=remaining)
            finally:
                waiters = self._waiters[host_id]
                waiters.remove(waiter)
                if not waiters:
                    del self._waiters[host_id]
            if waiter.done() and waiter.result():
                raise HostRevokedError(f"host {host_id} was revoked")

    def give_up(self, session_id: UUID, *, process_id: UUID, taken: bool) -> bool:
        """Detach a start that neither attached nor was reported in its time.

        A start its daemon took has its time from the take, and one still queued from
        the start; each give-up leaves the other kind as it is, so a start taken a
        moment before its queued time ran out still has its own. Any other process,
        and this one once it said ``Hello``, was stopped or ended, is left as it is.

        Args:
          session_id: The session.
          process_id: The process its start was for.
          taken: Whether to give up on the start once its daemon took it; else while
            no daemon has.

        Returns:
          gave_up: Whether it was detached, as :data:`NOT_STARTED` once taken and
            :data:`NOT_TAKEN` while queued; the caller then revokes its key.

        """
        live = self._sessions.get(session_id)
        if (
            live is None
            or live.process is None
            or live.process.process_id != process_id
            or live.process.state != "starting"
            or (live.offer is None) != taken
        ):
            return False
        self._detach(session_id, live, reason=NOT_STARTED if taken else NOT_TAKEN)
        return True

    def ended(self, report: ProcessEnded, *, host_id: UUID) -> None:
        """Take a daemon's report that a process it started has exited.

        It counts only for the session's current process, reported by the host it was
        offered to: the daemon's reason replaces any other, and the process is
        detached if it is not already. Any other report is ignored.

        Args:
          report: The process and why it ended.
          host_id: The host whose daemon reports it.

        """
        for session_id, live in self._sessions.items():
            process = live.process
            if (
                process is None
                or process.process_id != report.process_id
                or live.host_id != host_id
            ):
                continue
            if process.state == "detached":
                self._change(live, process.model_copy(update={"reason": report.reason}))
                return
            connection = live.connection
            self._detach(session_id, live, reason=report.reason)
            if connection is not None:
                _close(connection, wire_terminals.CLOSE_ENDED, "its daemon saw it end")
            return

    async def stop_host(self, host_id: UUID) -> list[UUID]:
        """Stop every process of a host taken out of service, and end its waits.

        Args:
          host_id: The host, revoked or removed.

        Returns:
          session_ids: The sessions whose process it stopped, in the order it did.

        """
        stopped: list[UUID] = []
        for session_id, live in list(self._sessions.items()):
            if (
                live.host_id == host_id
                and live.process is not None
                and _stoppable(live.process)
            ):
                _ = await self._end(
                    session_id,
                    live,
                    reason="host revoked",
                    closing=(wire_terminals.CLOSE_HOST_REVOKED, "its host was revoked"),
                )
                stopped.append(session_id)
        self._wake(host_id, revoked=True)
        return stopped

    async def sweep(self) -> None:
        """End every viewer's stream and process's socket whose rule no longer holds.

        A viewer's stream ends, so the one it opens next hears the true answer. A
        process's socket is closed as a refused one is, and the process detached. A
        question that fails leaves that one up and asks about the rest.
        """
        for session_id, live in list(self._sessions.items()):
            for viewer in list(live.viewers):
                if not await _still_allowed(viewer.allowed):
                    _drop(viewer)
            connection = live.connection
            if (
                connection is not None
                and not await _still_allowed(connection.allowed)
                # Asking took a turn of the loop, in which the socket may have gone.
                and live.connection is connection
            ):
                _ = await self._end(
                    session_id,
                    live,
                    reason="refused",
                    closing=(
                        wire_terminals.CLOSE_REFUSED,
                        "this key may no longer serve this session",
                    ),
                )

    def forget_idle(self, *, keyed: frozenset[UUID]) -> None:
        """Forget each session nothing of is live and no viewer watches.

        Nothing of it is live when it has had no process, or its process is detached
        and its key is not: a process whose key still lives may say ``Hello`` again,
        and must still hear that it was stopped. A session forgotten reads as one
        that has had no process.

        Args:
          keyed: Each process a live key was minted for.

        """
        for session_id, live in list(self._sessions.items()):
            process = live.process
            if not live.viewers and (
                process is None
                or (process.state == "detached" and process.process_id not in keyed)
            ):
                del self._sessions[session_id]

    def _attach(
        self,
        session_id: UUID,
        live: _Session,
        hello: Hello,
        *,
        host_id: UUID | None,
        connection: _Connection,
    ) -> _Connection:
        """Make ``connection`` the session's process's, as its ``Hello`` says it is."""
        process = live.process
        if process is not None and process.process_id == hello.process_id:
            replaced = live.connection
            live.connection = connection
            if replaced is not None:
                # The same process again: the old socket is one a cut left open on
                # this side alone, and it would otherwise lock the process out.
                _close(replaced, None)
                live.process = process.model_copy(update={"version": hello.version})
            else:
                self._take_back_offer(live)
                self._change(
                    live,
                    process.model_copy(
                        update={
                            "state": "attached",
                            "reason": "",
                            "version": hello.version,
                            "since": _now(),
                        },
                    ),
                )
        else:
            live.host_id = host_id
            live.connection = connection
            self._change(
                live,
                AgentProcess(
                    session_id=session_id,
                    process_id=hello.process_id,
                    host=hello.host,
                    state="attached",
                    version=hello.version,
                    since=_now(),
                ),
            )
        for held in live.held:
            connection.unsent += held.size
            connection.outbox.put_nowait(held)
        live.held.clear()
        # Either way above holds the process, attached.
        assert isinstance(live.process, AgentProcess)
        _logger.info(
            "process attached: session=%s process=%s host=%s",
            session_id,
            hello.process_id,
            live.process.host,
        )
        return connection

    # Return the code it closed with.
    #
    # A frame the wire does not allow ends the connection with :data:`BAD_FRAME`, and
    # ``None`` is returned.
    async def _read(
        self,
        session_id: UUID,
        live: _Session,
        connection: _Connection,
    ) -> int | None:
        """Pass the process's lines on until it closes."""
        while True:
            received = await connection.socket.receive()
            if isinstance(received, int):
                return received
            frame = process_frame(received)
            if frame is None or isinstance(frame, Hello):
                _logger.warning(
                    "process sent a bad frame: session=%s bytes=%d",
                    session_id,
                    len(received),
                )
                _close(connection, BAD_FRAME, "the server reads one JSON line a frame")
                return None
            # Only a text frame reads as a frame.
            assert isinstance(received, str)
            if isinstance(frame, EventLine):
                self._keep(live, frame, size=len(received.encode()))
            elif isinstance(frame, StateLine):
                live.state = frame.state
            else:
                live.settings = frame.settings
            self._fan_out(live, frame)

    # A finished reply is a record, stored or about to be, which a viewer reads with the
    # records: kept lines replaying it would draw it twice.
    def _keep(self, live: _Session, line: EventLine, *, size: int) -> None:
        """Keep ``line`` if it belongs to the reply being written, within the budget."""
        if line.kind == "ModelCallStarted":
            live.kept = [line]
            live.kept_bytes = size
        elif live.kept:
            live.kept.append(line)
            live.kept_bytes += size
        if live.kept_bytes > self._kept_bytes or line.kind in {
            "ModelResponseComplete",
            "ModelResponseCancelled",
        }:
            live.kept = []

    def _admit(self, unsent_bytes: int, unsent: _Unsent) -> None:
        """Refuse ``unsent`` when, with ``unsent_bytes`` not sent yet, it passes the cap."""
        if unsent_bytes + unsent.size > self._input_bytes:
            raise InputRefusedError(
                f"the agent holds {unsent_bytes} bytes of inputs it has not read, of "
                f"the {self._input_bytes} it may; send this once it has read them",
            )

    def _fan_out(self, live: _Session, line: ViewerFrame) -> None:
        """Queue ``line`` for every viewer, closing one that falls too far behind."""
        for viewer in live.viewers:
            if viewer.dropped:
                continue
            if len(viewer.lines) >= self._viewer_lag:
                _drop(viewer)
            else:
                viewer.lines.append(line)
                viewer.woken.set()

    async def _end(
        self,
        session_id: UUID,
        live: _Session,
        *,
        reason: str,
        closing: tuple[int, str],
    ) -> AgentProcess:
        """Detach the session's process for ``reason``, closing its socket; return it."""
        connection = live.connection
        self._detach(session_id, live, reason=reason)
        # Read before the close is awaited, while it is still the session's.
        detached = live.process
        assert isinstance(detached, AgentProcess)
        if connection is not None:
            _close(connection, *closing)
            await connection.socket.close(*closing)
        return detached

    def _detach(self, session_id: UUID, live: _Session, *, reason: str) -> None:
        """Detach the process: forget its socket, its start and all it said it was."""
        process = live.process
        assert isinstance(process, AgentProcess)
        live.connection = None
        live.held.clear()
        self._take_back_offer(live)
        live.state = None
        live.settings = ()
        live.kept = []
        self._change(
            live,
            process.model_copy(
                update={
                    "state": "detached",
                    "reason": reason,
                    "since": process.since if process.state == "detached" else _now(),
                },
            ),
        )
        _logger.info(
            "process detached: session=%s process=%s reason=%r",
            session_id,
            process.process_id,
            reason,
        )

    def _change(self, live: _Session, process: AgentProcess) -> None:
        """Hold ``process`` as the session's, telling viewers when its state or reason moved."""
        before = live.process
        live.process = process
        if before is None or (before.state, before.reason) != (
            process.state,
            process.reason,
        ):
            self._fan_out(live, ProcessLine(process=process))

    def _take_back_offer(self, live: _Session) -> None:
        """Forget a start no daemon has taken yet."""
        offer, live.offer = live.offer, None
        if offer is None:
            return
        # A start is queued under the host it names, and leaves the queue only with
        # ``offer`` cleared, here or in ``_take``.
        assert isinstance(live.host_id, UUID)
        queue = self._offers[live.host_id]
        queue.remove(offer)
        if not queue:
            del self._offers[live.host_id]

    def _take(self, host_id: UUID) -> ServeSession | None:
        """Hand out the host's oldest start, once."""
        queue = self._offers.get(host_id)
        if queue is None:
            return None
        offer = queue.popleft()
        if not queue:
            del self._offers[host_id]
        self._sessions[offer.session_id].offer = None
        _logger.info(
            "process offered: session=%s process=%s host_id=%s",
            offer.session_id,
            offer.process_id,
            host_id,
        )
        return offer

    def _wake(self, host_id: UUID, *, revoked: bool = False) -> None:
        """Wake every wait held for the host, saying whether it was revoked."""
        for waiter in self._waiters.get(host_id, []):
            if not waiter.done():
                waiter.set_result(revoked)

    def _forget_if_idle(self, session_id: UUID, live: _Session) -> None:
        """Drop a session that holds nothing: no process, viewer or held input."""
        if live.process is None and not live.viewers and not live.held:
            _ = self._sessions.pop(session_id, None)


_NORMAL_CLOSURE: Final = 1000
"""The close code of a process that ended itself."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Unsent:
    """An input not sent to the process yet, as it goes on the socket."""

    text: str
    size: int
    """The bytes of ``text``, which count against the session's ``input_bytes``."""


@dataclass(slots=True, kw_only=True, eq=False)
class _Connection:
    """One socket of the process serving a session."""

    socket: ProcessSocket
    allowed: Allowed
    """The rule its key was let in by, asked again by each sweep."""

    key: UUID | None
    """The id of the API key it was opened with."""

    outbox: asyncio.Queue[_Unsent] = field(default_factory=asyncio.Queue[_Unsent])
    """Inputs waiting to be sent on it, each sent once."""

    unsent: int = 0
    """The bytes of the inputs waiting in ``outbox`` and of the one being sent."""

    ended: asyncio.Future[tuple[int, str] | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """Set when the relay ends it: the code and reason it is closed with, or ``None``
    for a close with no code of the relay's own."""


@dataclass(slots=True, kw_only=True, eq=False)
class _Viewer:
    """One viewer's stream: the lines it has yet to read."""

    allowed: Allowed
    """The rule it was let in by, asked again by each sweep."""

    lines: deque[ViewerFrame] = field(default_factory=deque[ViewerFrame])
    woken: asyncio.Event = field(default_factory=asyncio.Event)
    dropped: bool = False
    """Whether it is closed: it fell too far behind, or its rule no longer holds."""


@dataclass(slots=True, kw_only=True, eq=False)
class _Session:
    """What the relay holds of one session."""

    process: AgentProcess | None = None
    host_id: UUID | None = None
    """The host its process was offered to, or that its ``Hello`` named."""

    connection: _Connection | None = None
    offer: ServeSession | None = None
    """A start no daemon has taken yet."""

    state: AgentState | None = None
    settings: tuple[Setting, ...] = ()
    kept: list[EventLine] = field(default_factory=list[EventLine])
    """The lines of the reply being written; none while no reply is, since the lines
    last ran over, or once the reply ended."""

    kept_bytes: int = 0
    """The bytes of ``kept``, read only while it holds a line."""

    viewers: set[_Viewer] = field(default_factory=set[_Viewer])
    held: list[_Unsent] = field(default_factory=list[_Unsent])
    """Inputs sent while the process was starting."""


def _refusal(process: AgentProcess | None, hello: Hello) -> tuple[int, str] | None:
    """Return the close a ``Hello`` gets instead of the session, or ``None``."""
    if process is None:
        return None
    if process.state != "detached":
        if hello.process_id == process.process_id:
            return None
        return (wire_terminals.CLOSE_TAKEN_OVER, "another process holds this session")
    if hello.process_id == process.process_id and process.reason != "disconnected":
        return (wire_terminals.CLOSE_ENDED, "this process was stopped or has ended")
    return None


def _stoppable(process: AgentProcess) -> bool:
    """Say whether a process may still run: not detached, or detached by a drop."""
    return process.state != "detached" or process.reason == "disconnected"


def _drop(viewer: _Viewer) -> None:
    """Close a viewer's stream: it reads nothing more, and wakes to find so."""
    viewer.dropped = True
    viewer.lines.clear()
    viewer.woken.set()


# As ``ToolRelay._still_alive`` does: a database that cannot answer now must not end
# every stream and socket on the server.
async def _still_allowed(allowed: Allowed) -> bool:
    """Ask whether a rule still holds; a question that fails says yes."""
    try:
        return await allowed()
    except Exception:
        _logger.warning("session relay: whether a rule holds is unknown", exc_info=True)
        return True


def _close(connection: _Connection, code: int | None, reason: str = "") -> None:
    """End ``connection`` as the relay ends it, with ``code`` or with none of its own."""
    if not connection.ended.done():
        connection.ended.set_result(None if code is None else (code, reason))


async def _write(connection: _Connection) -> None:
    """Send the connection's inputs to the process as they come, each once."""
    while True:
        unsent = await connection.outbox.get()
        await connection.socket.send_text(unsent.text)
        connection.unsent -= unsent.size


def _now() -> datetime:
    """Return the wall clock, as ``since`` fields hold it."""
    return datetime.now(UTC)
