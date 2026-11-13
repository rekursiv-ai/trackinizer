"""An agent process's socket to Trackinizer: its lines up, a person's inputs down.

The process dials :data:`SESSION_PROCESS_WS_PATH` with its key and says ``Hello``
first on every connection, then sends again the last state and settings, which the
relay forgets when a process drops. Then each line goes up as it is sent, one JSON
text frame each, and each ``Input`` comes down.

::

    process                              Trackinizer
      Hello, StateLine, SettingsLine  ─▶
      EventLine, StateLine, ...       ─▶
                                      ◀─  Input
                                      ◀─  close: CLOSE_ENDED, CLOSE_TAKEN_OVER,
                                          CLOSE_REFUSED or CLOSE_HOST_REVOKED

The agent keeps running while the socket is down: a line sent then is dropped, since
a viewer that reconnects is told where the session stands afresh. Any close but the
four ends in a redial with the same key; one of the four, or a key refused at the
handshake, ends the socket for good.

Typical usage example:

  async with SessionSocket(client, session_id=session_id, hello=hello) as socket:
      socket.send(StateLine(state=state))       # Queued; never blocks.
      async for sent in socket.inputs():
          ...
      code = await socket.closed
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, override

import asyncio
import contextlib
import logging

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from trackinizer.sagent.types import AsyncResource
from trackinizer.wire.wire_session_relay import (
    MAX_FRAME_BYTES,
    SESSION_PROCESS_WS_PATH,
    Input,
    SettingsLine,
    StateLine,
)
from trackinizer.wire.wire_terminals import (
    CLOSE_ENDED,
    CLOSE_HOST_REVOKED,
    CLOSE_REFUSED,
    CLOSE_TAKEN_OVER,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator
    from uuid import UUID

    from websockets.asyncio.client import ClientConnection

    from trackinizer.wire.wire_session_relay import EventLine, Hello


__all__ = [
    "GAVE_UP",
    "SessionSocket",
    "SocketClient",
    "SocketClosedError",
    "retry_delays",
]


GAVE_UP: Final = 1006
"""What :attr:`SessionSocket.closed` resolves to once it has been down too long: the
close code of a connection lost with no close frame."""

_FINAL: Final = frozenset(
    {CLOSE_ENDED, CLOSE_TAKEN_OVER, CLOSE_REFUSED, CLOSE_HOST_REVOKED},
)

_LOG = logging.getLogger(__name__)


class SocketClient(Protocol):
    """Where a socket dials, and the key it dials with: a ``Client``'s own."""

    @property
    def base_url(self) -> str:
        """The server's ``http://`` or ``https://`` address."""
        ...

    @property
    def api_key(self) -> str:
        """Sent as a bearer token."""
        ...


class SocketClosedError(ConnectionError):
    """The socket closed for good before the server took its first ``Hello``.

    Args:
      code: The close code, as :attr:`SessionSocket.closed` would resolve to.
      reason: Why, as the server or the socket said.

    """

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"the session's socket closed with {code}: {reason}")
        self.code = code
        self.reason = reason


class SessionSocket(AsyncResource):
    """One agent process's socket to its session, redialled until it closes for good.

    Entering it waits until the first ``Hello`` is sent on an open connection.

    Args:
      client: Where to dial, and the key to dial with.
      session_id: The session the process serves.
      hello: What the process says first on each connection.
      give_up_sec: How long it may be down before :attr:`closed` resolves to
        :data:`GAVE_UP`; ``None`` to never give up.

    Raises:
      SocketClosedError: On entering, the socket closed for good, or was down past
        ``give_up_sec``, before its first ``Hello`` went.

    """

    def __init__(
        self,
        client: SocketClient,
        *,
        session_id: UUID,
        hello: Hello,
        give_up_sec: float | None = None,
    ) -> None:
        base = client.base_url.rstrip("/").replace("http", "ws", 1)
        self._url = f"{base}{SESSION_PROCESS_WS_PATH.format(session_id=session_id)}"
        self._key = client.api_key
        self._hello = hello.model_dump_json()
        self._give_up_sec = give_up_sec
        self.closed = asyncio.Future[int](loop=asyncio.get_running_loop())
        """One of the four close codes, or :data:`GAVE_UP`, once it closes for good."""

        self.reason = ""
        """Why it closed, as the server's close frame said; empty until then."""

        self._state: str | None = None
        self._settings: str | None = None
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._inputs: asyncio.Queue[Input] = asyncio.Queue()
        self._up = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._giving_up: asyncio.TimerHandle | None = None

    @override
    async def aopen(self) -> None:
        self._down()
        task = self._task = asyncio.create_task(self._run())
        up = asyncio.ensure_future(self._up.wait())
        try:
            _ = await asyncio.wait(
                (up, self.closed, task),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            up.cancel()
        if self.closed.done():
            await self.aclose()
            raise SocketClosedError(self.closed.result(), self.reason)
        if task.done():
            # It ended neither up nor closed: what it raised is the failure.
            task.result()

    @override
    async def aclose(self) -> None:
        if self._giving_up is not None:
            self._giving_up.cancel()
        assert isinstance(self._task, asyncio.Task)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    def send(self, line: EventLine | StateLine | SettingsLine) -> None:
        """Queue ``line`` to go up; drop it while the socket is down.

        Args:
          line: An event, the agent's state, or its settings. The last state and
            settings are sent again on each new connection.

        """
        text = line.model_dump_json()
        if isinstance(line, StateLine):
            self._state = text
        elif isinstance(line, SettingsLine):
            self._settings = text
        if not self._up.is_set():
            return
        # The relay closes a socket that sends more, which would cost every line
        # after this one a redial.
        if len(text.encode()) > MAX_FRAME_BYTES:
            _LOG.warning("dropped a %s line of %d bytes", line.type, len(text))
            return
        self._outbox.put_nowait(text)

    async def inputs(self) -> AsyncIterator[Input]:
        """Yield each input the server sends, across reconnects, in order."""
        while True:
            yield await self._inputs.get()

    async def _run(self) -> None:
        """Dial and serve until closed for good; a refused key closes it as refused."""
        try:
            await self._dialing()
        except InvalidStatus as refused:
            status = refused.response.status_code
            self._close(CLOSE_REFUSED, f"the server refused the key: HTTP {status}")

    # Not closed when left: each connection is closed as its serving ends, and the
    # generator that made it is finalized with the loop's others.
    async def _dialing(self) -> None:
        """Dial, serve each connection, and redial until one of the four codes."""
        dialing = connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._key}"},
            max_size=MAX_FRAME_BYTES,
            ping_interval=20,
            close_timeout=1,
            process_exception=_fatal,
            reconnect_delays=retry_delays,
        )
        async for connection in dialing:
            try:
                await self._serve(connection)
            except ConnectionClosed as closed:
                said = closed.rcvd
                if said is not None and said.code in _FINAL:
                    self._close(said.code, said.reason)
                    return
            self._down()

    async def _serve(self, connection: ClientConnection) -> None:
        """Say hello, then pass lines up and inputs down until the connection ends."""
        # Lines queued while the last connection was ending went nowhere.
        self._outbox = asyncio.Queue()
        sending = asyncio.create_task(self._sending(connection))
        try:
            await connection.send(self._hello)
            # Until they stand still: a state or settings sent while one of these sends
            # waits is kept, not queued, since the socket is not up yet.
            while True:
                sent = (self._state, self._settings)
                for kept in sent:
                    if kept is not None:
                        await connection.send(kept)
                if sent == (self._state, self._settings):
                    break
            self._up.set()
            if self._giving_up is not None:
                self._giving_up.cancel()
            async for message in connection:
                self._take(message)
        finally:
            sending.cancel()
            self._up.clear()
            # Closing an open connection with 1000 says the process ended itself.
            await connection.close()

    async def _sending(self, connection: ClientConnection) -> None:
        """Send each queued line in order."""
        with contextlib.suppress(ConnectionClosed):
            while True:
                await connection.send(await self._outbox.get())

    def _take(self, message: str | bytes) -> None:
        """Queue an input the server sent; pass over a frame that is not one."""
        try:
            sent = Input.model_validate_json(message)
        # A pydantic validation error is a ``ValueError``.
        except ValueError as error:
            _LOG.warning("passed over a frame that is no input: %s", error)
            return
        self._inputs.put_nowait(sent)

    def _down(self) -> None:
        """Start counting toward giving up, if it ever does."""
        if self._give_up_sec is None:
            return
        if self._giving_up is not None:
            self._giving_up.cancel()
        self._giving_up = asyncio.get_running_loop().call_later(
            self._give_up_sec,
            self._give_up,
        )

    def _give_up(self) -> None:
        """Close for good, as down too long, and stop dialing."""
        self._close(GAVE_UP, f"no connection for {self._give_up_sec:g} s")
        assert isinstance(self._task, asyncio.Task)
        self._task.cancel()

    # Once: a waiter that stops waiting cancels the future, and a close after that
    # has no one to tell.
    def _close(self, code: int, reason: str) -> None:
        """Resolve :attr:`closed`, unless it is already done."""
        if self.closed.done():
            return
        self.reason = reason
        self.closed.set_result(code)


def retry_delays() -> Generator[float]:
    """Yield the waits between tries while the server is down: 1 s, doubling to 30 s.

    Yields:
      delay_sec: How long to wait before the next try.

    """
    delay_sec = 1.0
    while True:
        yield delay_sec
        delay_sec = min(2 * delay_sec, 30.0)


def _fatal(error: Exception) -> Exception | None:
    """Return ``error`` when the server refused the key, which no redial changes."""
    if isinstance(error, InvalidStatus) and error.response.status_code in {401, 403}:
        return error
    return None
