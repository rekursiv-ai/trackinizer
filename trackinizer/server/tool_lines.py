"""Read each line going to a host, gate it, and describe it for the relay's log.

The agent's socket of a tool connection carries lines (see ``trackinizer.sagent.host.wire``) that the
daemon acts on. The host's reader checks every field of every line, and a line
it cannot read ends that tool connection alone. So this module reads, by JSON alone (Trackinizer
does not import sagent), only what the relay needs to decide and to log:

- A line's kind is its ``py/object``; only :data:`ALLOWED_LINES` pass. Any other is
  dropped, and answered if it names a string ``id``, so the agent does not wait: an
  agent may well send a ``Note``, and sends it again after each reconnect.
- A ``Hello`` names the session, as a string, once. A ``Call`` comes after it, with a
  string ``id``, ``agent`` and ``tool`` and an object of ``arguments``: what is
  logged, the arguments by their size alone. Each is gated by an :data:`Authorize`; a
  denied one is answered to the agent and never forwarded.
- An ``Ack`` names, by its ``id``, a result the agent holds, and :class:`ResultIds`
  reads the ``id`` of each ``Result`` the host sends: between a call and either, the
  call is running, which the relay's idle rule needs.
- What is forwarded is this module's own ``json.dumps`` of the parsed line, never the
  bytes received, so what is logged is what the host reads, whatever a duplicate
  or escaped key made of the original.

Anything else ends the tool connection: :meth:`ToolLines.feed` raises ``ValueError`` saying why,
and nothing of that frame is forwarded. A line over the tool connection's ``line_bytes`` is one;
the agent then reads that call as lost.

Typical usage example:

  lines = ToolLines(asker=a, host=m, authorize=authorize, line_bytes=n)
  routed, calls = lines.feed(frame)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal, cast

import json


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


__all__ = [
    "ALLOWED_LINES",
    "Authorize",
    "GatedCall",
    "Outcome",
    "ResultIds",
    "Routed",
    "ToolLines",
    "Verdict",
    "authorize",
    "refused_line",
]

ALLOWED_LINES: Final = frozenset(
    {"Hello", "Call", "Cancel", "Ack", "Describe", "Ping", "Bye"},
)
"""The kinds an agent may send a host; ``Note``, ``Peers`` and the daemon's do not pass."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Routed:
    """Where the bytes read go."""

    to_daemon: bytes = b""
    """Lines for the host."""

    to_agent: bytes = b""
    """Lines for the agent: the answers to what was refused."""

    dropped: tuple[int, ...] = ()
    """The size of each line dropped, of a kind not offered."""

    acks: tuple[str, ...] = ()
    """The ``id`` of each ``Ack``: a result the agent holds."""


type Outcome = Literal["allow", "deny"]
"""The gate's verdict on a call, not what the tool did.

``allow`` means it was let through to the host, not that it arrived or ran."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GatedCall:
    """One call the gate judged, as the relay logs it: never what its arguments say.

    Every field but ``asker`` and ``host`` is read from the agent's own
    ``Call`` line, and its ``Hello``. What the tool did is in the session's record,
    which ``session`` and ``call_id`` join this to.
    """

    asker: str
    agent: str
    session: str
    host: str
    call_id: str
    """The ``id`` of the agent's ``Call`` line, as the agent sent it."""

    tool: str
    argument_bytes: int
    outcome: Outcome


@dataclass(frozen=True, slots=True, kw_only=True)
class Verdict:
    """Whether a call may run."""

    outcome: Outcome
    reason: str = ""
    """Why it was denied, as the agent is told."""


type Authorize = Callable[[GatedCall], Verdict]
"""Judge a call, from what is logged of it."""


def authorize(call: GatedCall) -> Verdict:
    """Return the verdict on ``call``: every call is allowed in v1."""
    del call
    return Verdict(outcome="allow")


def refused_line(call_id: str, reason: str) -> bytes:
    """Return the ``Result`` line that fails call ``call_id`` with ``reason``.

    Args:
      call_id: The id of the line refused.
      reason: What the agent is told.

    Returns:
      line: A ``Result`` whose record is a failed ``ToolResult`` with ``reason``
        on ``stderr``, newline included.

    """
    return _dumped(
        {
            "py/object": "Result",
            "id": call_id,
            "record": {
                "py/object": "ToolResult",
                "call_id": "",
                "state": "failed",
                "stderr": [reason],
                "extra": {},
            },
        },
    )


class ToolLines:
    """Reads the bytes an agent sends a host, a frame at a time."""

    def __init__(
        self,
        *,
        asker: str,
        host: str,
        authorize: Authorize,
        line_bytes: int,
    ) -> None:
        """Hold the tool connection's parties, its gate, and the longest line it reads."""
        self._asker = asker
        self._host = host
        self._authorize = authorize
        self._line_bytes = line_bytes
        self._pending = bytearray()
        self._session: str | None = None
        self._failed = False

    def feed(self, payload: bytes) -> tuple[Routed, Sequence[GatedCall]]:
        """Return where ``payload``'s complete lines go, and the calls among them.

        A line split across frames is held until its newline arrives.

        Args:
          payload: Bytes the agent sent, cut anywhere.

        Returns:
          routed: The lines for the host and for the agent, and the calls read.

        Raises:
          ValueError: A line ends the tool connection; it says why. Nothing from this frame is
            forwarded, and every later feed raises too.

        """
        if self._failed:
            msg = "the tool connection has been spent"
            raise ValueError(msg)
        try:
            return self._feed(payload)
        except ValueError:
            self._spend()
            raise
        except RecursionError as error:
            self._spend()
            msg = "a line nests too deep"
            raise ValueError(msg) from error

    def _spend(self) -> None:
        self._failed = True
        self._pending.clear()

    def _feed(self, payload: bytes) -> tuple[Routed, Sequence[GatedCall]]:
        to_daemon = bytearray()
        to_agent = bytearray()
        calls: list[GatedCall] = []
        dropped: list[int] = []
        acks: list[str] = []
        *ended, rest = payload.split(b"\n")
        for piece in ended:
            self._hold(piece)
            line = bytes(self._pending)
            self._pending.clear()
            if not self._route(line, to_daemon, to_agent, calls, acks):
                dropped.append(len(line))
        self._hold(rest)
        routed = Routed(
            to_daemon=bytes(to_daemon),
            to_agent=bytes(to_agent),
            dropped=tuple(dropped),
            acks=tuple(acks),
        )
        return routed, calls

    def _hold(self, piece: bytes) -> None:
        """Add ``piece`` to the line being read; past the cap it ends the tool connection."""
        if len(self._pending) + len(piece) > self._line_bytes:
            msg = f"a line is over {self._line_bytes} bytes"
            raise ValueError(msg)
        self._pending += piece

    def _route(
        self,
        raw: bytes,
        to_daemon: bytearray,
        to_agent: bytearray,
        calls: list[GatedCall],
        acks: list[str],
    ) -> bool:
        """Route one line; return whether its kind is offered."""
        line = _parsed(raw)
        kind = line.get("py/object")
        if not isinstance(kind, str) or kind not in ALLOWED_LINES:
            call_id = line.get("id")
            if isinstance(call_id, str):
                to_agent += refused_line(call_id, f"{kind} is not offered.")
            return False
        if kind == "Hello":
            self._hello(line)
        elif kind == "Ack" and isinstance(acked := line.get("id"), str):
            acks.append(acked)
        elif kind == "Call":
            call, verdict = self._call(line)
            calls.append(call)
            if call.outcome != "allow":
                to_agent += refused_line(
                    call.call_id,
                    verdict.reason or "The call is not allowed.",
                )
                return True
        to_daemon += _dumped(line)
        return True

    def _hello(self, line: dict[str, object]) -> None:
        session = line.get("session_id")
        if self._session is not None or not isinstance(session, str):
            msg = "a Hello names one session, as a string, once"
            raise ValueError(msg)
        self._session = session

    # ``argument_bytes`` is over the wire form of ``arguments`` as forwarded
    # (``{"py/dict": ...}`` and the like), not over what the tool reads, so it sizes the
    # bytes the host is sent.
    def _call(self, line: dict[str, object]) -> tuple[GatedCall, Verdict]:
        """Return the call ``line`` makes, and the verdict on it."""
        call_id, agent, tool = line.get("id"), line.get("agent"), line.get("tool")
        arguments = line.get("arguments")
        if (
            self._session is None
            or not isinstance(call_id, str)
            or not isinstance(agent, str)
            or not isinstance(tool, str)
            or not isinstance(arguments, dict)
        ):
            msg = (
                "a Call follows a Hello, with an id, agent and tool that are strings and "
                "arguments that are an object"
            )
            raise ValueError(msg)
        shown = json.dumps(arguments, sort_keys=True, allow_nan=False).encode()
        call = GatedCall(
            asker=self._asker,
            agent=agent,
            session=self._session,
            host=self._host,
            call_id=call_id,
            tool=tool,
            argument_bytes=len(shown),
            outcome="allow",
        )
        verdict = self._authorize(call)
        return replace(
            call,
            outcome="allow" if verdict.outcome == "allow" else "deny",
        ), verdict


class ResultIds:
    """Reads which calls the ``Result`` lines a daemon sends answer.

    The host's frames are relayed as they came; this reads a copy, by JSON alone, as
    :class:`ToolLines` reads the agent's. A line it cannot read, or one longer than
    ``line_bytes``, is passed over and never ends the tool connection: what the host sends is the
    agent's to judge, and the agent's ``Ack`` of a result passed over still says it
    came.
    """

    def __init__(self, *, line_bytes: int) -> None:
        """Hold the longest line read; a longer one is passed over, not kept."""
        self._line_bytes = line_bytes
        self._pending = bytearray()
        self._over = False

    def feed(self, payload: bytes) -> list[str]:
        """Return the call ids of the ``Result`` lines ``payload`` completes.

        Args:
          payload: Bytes the host sent, cut anywhere.

        Returns:
          call_ids: Each completed ``Result``'s ``id``, in order.

        """
        call_ids: list[str] = []
        *ended, rest = payload.split(b"\n")
        for piece in ended:
            self._hold(piece)
            line, over = bytes(self._pending), self._over
            self._pending.clear()
            self._over = False
            if not over and (call_id := _result_id(line)) is not None:
                call_ids.append(call_id)
        self._hold(rest)
        return call_ids

    def _hold(self, piece: bytes) -> None:
        """Add ``piece`` to the line being read; past the cap, drop it to its end."""
        if self._over:
            return
        if len(self._pending) + len(piece) > self._line_bytes:
            self._over = True
            self._pending.clear()
            return
        self._pending += piece


def _result_id(raw: bytes) -> str | None:
    """Return the ``id`` of the ``Result`` line ``raw`` is; ``None`` for any other."""
    try:
        line = _parsed(raw)
    except (ValueError, RecursionError):
        return None
    call_id = line.get("id")
    return (
        call_id
        if line.get("py/object") == "Result" and isinstance(call_id, str)
        else None
    )


def _dumped(line: dict[str, object]) -> bytes:
    """Return ``line`` as the one line forwarded; a number JSON lacks is refused."""
    return (json.dumps(line, allow_nan=False) + "\n").encode()


def _parsed(raw: bytes) -> dict[str, object]:
    """Return the object ``raw`` holds, or raise ``ValueError``."""
    line = cast(object, json.loads(raw.decode("utf-8")))
    if not isinstance(line, dict):
        msg = "a line is not an object"
        raise ValueError(msg)  # noqa: TRY004 -- The caller ends the tool connection on ValueError.
    return cast(dict[str, object], line)
