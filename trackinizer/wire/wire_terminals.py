"""Wire contract for the browser terminal: paths, bodies, limits, close codes, frames.

A terminal is a shell on a machine its owner connected, shown in the owner's
browser. The browser and the host each dial the server, which pairs them and
relays bytes:

- The browser ``POST``s :data:`TERMINALS_PATH`, then opens
  :data:`TERMINAL_WS_PATH` and sends ``ATTACH`` first.
- The host holds a ``GET`` on :data:`MACHINE_TERMINAL_WAIT_PATH`; an open request
  returns at once, and the host dials :data:`MACHINE_TERMINAL_WS_PATH`.

One WebSocket binary message is one frame: a kind byte, a four-byte big-endian
length, and the payload (``trackinizer.lib.posix.host.encode_frame``). The kinds below
equal that module's ``Frame`` values; a test holds them equal.

- Browser to server: ``ATTACH`` (:data:`TICKET_CHARS` ASCII ticket bytes then a
  u64 offset) first, then ``INPUT``, ``RESIZE`` (u16 rows, u16 cols), ``STOP``.
- Server to browser: ``ACK`` (u64 offset the stream resumes at), ``OUTPUT``, ``EXIT``
  (u32 status).
- Server to host: ``INPUT``, ``RESIZE``, ``STOP``. Host to server: ``OUTPUT``, ``EXIT``.

A direct link (a WebRTC data channel between the browser and the host) is set up
over these same sockets. The server forwards ``SIGNAL`` (a :class:`DirectSignal`)
and the browser's ``ROUTE`` (u8 route, u64 offset) without reading them. The host
sends ``KEEPALIVE`` (u32 milliseconds since it last saw input) every
:data:`KEEPALIVE_SEC` while output goes direct, so the idle limit still holds. A
host that returns to the relay answers with ``ACK`` (the offset it resumes at)
then ``OUTPUT``; the server restarts its ring there.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trackinizer.wire.wire_machines import NAME_PATTERN, MachineStatus


__all__ = [
    "CLOSE_ENDED",
    "CLOSE_HOST_LOST",
    "CLOSE_IDLE",
    "CLOSE_LIFETIME",
    "CLOSE_MACHINE_REVOKED",
    "CLOSE_REFUSED",
    "CLOSE_TAKEN_OVER",
    "DETACH_GRACE_SEC",
    "DIRECT_FRAME_BYTES",
    "DIRECT_OPEN_TIMEOUT_SEC",
    "FINAL_CLOSE_CODES",
    "FRAME_ACK",
    "FRAME_ATTACH",
    "FRAME_EXIT",
    "FRAME_INPUT",
    "FRAME_KEEPALIVE",
    "FRAME_OUTPUT",
    "FRAME_RESIZE",
    "FRAME_ROUTE",
    "FRAME_SIGNAL",
    "FRAME_STOP",
    "IDLE_SEC",
    "KEEPALIVE_SEC",
    "LIFETIME_SEC",
    "MACHINE_TERMINAL_WAIT_PATH",
    "MACHINE_TERMINAL_WS_PATH",
    "MAX_COLS",
    "MAX_ICE_SERVERS",
    "MAX_INPUT_BYTES_PER_SEC",
    "MAX_INPUT_FRAME_BYTES",
    "MAX_OPENS_PER_MIN_PER_USER",
    "MAX_OUTPUT_BYTES_PER_SEC",
    "MAX_ROWS",
    "MAX_SIGNALS_PER_MIN",
    "MAX_SIGNAL_BYTES",
    "OPEN_TTL_SEC",
    "RECONNECT_BACKOFF_SEC",
    "RECONNECT_WINDOW_SEC",
    "RING_BYTES",
    "ROUTE_DIRECT",
    "ROUTE_RELAY",
    "SWEEP_SEC",
    "TERMINALS_PATH",
    "TERMINAL_ATTACH_PATH",
    "TERMINAL_CLOSE_PATH",
    "TERMINAL_CONFIRM_PATH",
    "TERMINAL_MACHINES_PATH",
    "TERMINAL_WS_PATH",
    "TICKET_CHARS",
    "TICKET_TTL_SEC",
    "WAIT_LEASE_SEC",
    "WAIT_MAX_SEC",
    "DirectSignal",
    "LiveTerminal",
    "OpenTerminal",
    "TerminalCommand",
    "TerminalMachine",
    "TerminalMachines",
    "TerminalOpened",
    "TerminalTicket",
    "TerminalWait",
    "decode_keepalive",
    "decode_route",
    "encode_keepalive",
    "encode_route",
]

TERMINALS_PATH: Final = "/api/terminals"
"""``POST``: open a terminal (the owner's browser, cookie only)."""

TERMINAL_MACHINES_PATH: Final = "/api/terminals/machines"
"""``GET``: the caller's own machines; ``?workspace=ID`` also lists its live terminals.
The configured assistant passes ``?owner=EMAIL`` to list the person it answers."""

TERMINAL_CONFIRM_PATH: Final = "/api/terminals/machines/{name}/confirm"
"""``POST``: the owner confirms a machine enrolled with a key, so terminals may open."""

TERMINAL_ATTACH_PATH: Final = "/api/terminals/{terminal_id}/attach"
"""``POST``: mint a reconnect ticket for the opener."""

TERMINAL_CLOSE_PATH: Final = "/api/terminals/{terminal_id}/close"
"""``POST``: send ``STOP`` to the terminal's shell."""

TERMINAL_WS_PATH: Final = "/api/terminals/{terminal_id}/ws"
"""The browser's WebSocket; its first frame is ``ATTACH``."""

MACHINE_TERMINAL_WAIT_PATH: Final = "/api/machines/{machine_id}/terminals/wait"
"""``GET ?instance=ID``, held up to :data:`WAIT_MAX_SEC`: the host's next open request,
or ``null`` when none came."""

MACHINE_TERMINAL_WS_PATH: Final = (
    "/api/machines/{machine_id}/terminals/{terminal_id}/ws"
)
"""The host's WebSocket, authenticated by the machine credential."""

FRAME_ATTACH: Final = 1
FRAME_INPUT: Final = 2
FRAME_RESIZE: Final = 3
FRAME_STOP: Final = 5
FRAME_OUTPUT: Final = 6
FRAME_ACK: Final = 7
FRAME_EXIT: Final = 8
FRAME_SIGNAL: Final = 9
FRAME_ROUTE: Final = 10
FRAME_KEEPALIVE: Final = 11

ROUTE_RELAY: Final = 0
ROUTE_DIRECT: Final = 1
"""The path output takes: the server's relay, or the direct link."""

MAX_SIGNAL_BYTES: Final = 16_384
"""The most a ``SIGNAL`` payload may carry: far above an SDP with a few candidates."""

MAX_SIGNALS_PER_MIN: Final = 6
"""``SIGNAL`` frames the server forwards from a terminal's browser in a minute."""

DIRECT_FRAME_BYTES: Final = 16_384
"""The most a frame may carry on the data channel: the size every browser accepts."""

DIRECT_OPEN_TIMEOUT_SEC: Final = 8
"""How long the browser waits for the data channel before it stays on the relay."""

KEEPALIVE_SEC: Final = 10
MAX_ICE_SERVERS: Final = 4

TICKET_CHARS: Final = 43
"""Length of a ticket (256 random bits, url-safe base64) in the ``ATTACH`` payload."""

CLOSE_ENDED: Final = 4000
CLOSE_TAKEN_OVER: Final = 4001
CLOSE_IDLE: Final = 4002
CLOSE_LIFETIME: Final = 4003
CLOSE_HOST_LOST: Final = 4004
CLOSE_REFUSED: Final = 4403
CLOSE_MACHINE_REVOKED: Final = 4410

FINAL_CLOSE_CODES: Final = frozenset(
    {CLOSE_ENDED, CLOSE_TAKEN_OVER, CLOSE_IDLE, CLOSE_LIFETIME, CLOSE_MACHINE_REVOKED},
)
"""Close codes after which the browser does not reconnect."""

MAX_ROWS: Final = 500
MAX_COLS: Final = 1_000

MAX_INPUT_FRAME_BYTES: Final = 65_536
MAX_INPUT_BYTES_PER_SEC: Final = 1_048_576
MAX_OUTPUT_BYTES_PER_SEC: Final = 4_194_304

RING_BYTES: Final = 262_144
"""Output the server keeps for a browser that re-attaches."""

TICKET_TTL_SEC: Final = 30
OPEN_TTL_SEC: Final = 60
WAIT_MAX_SEC: Final = 25
WAIT_LEASE_SEC: Final = 45
"""A machine that waited within this long can take an open."""

DETACH_GRACE_SEC: Final = 60
IDLE_SEC: Final = 3_600
LIFETIME_SEC: Final = 86_400
SWEEP_SEC: Final = 60

MAX_OPENS_PER_MIN_PER_USER: Final = 10

RECONNECT_BACKOFF_SEC: Final = (0.5, 1.0, 2.0, 4.0, 8.0)
RECONNECT_WINDOW_SEC: Final = 60

type IceServers = Annotated[
    list[Annotated[str, Field(pattern=r"^stun:\S{1,200}$")]],
    Field(max_length=MAX_ICE_SERVERS),
]
"""STUN servers the browser and the host both use to find each other's address."""

type TerminalCommand = Literal["claude", "codex"]
"""A CLI a terminal may start in place of a login shell; the host allows two."""

type RunFolder = Annotated[
    str,
    Field(max_length=1_024, pattern=r"^[/~][^\x00-\x1f\x7f]*$"),
]
"""The folder a run starts in: absolute or ``~``-relative, with no control character,
since the host writes it into the terminal."""


class OpenTerminal(BaseModel):
    """Open a terminal on one of the caller's machines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machine: str = Field(pattern=NAME_PATTERN)
    rows: Annotated[int, Field(ge=1, le=MAX_ROWS)]
    cols: Annotated[int, Field(ge=1, le=MAX_COLS)]

    workspace: uuid.UUID | None = None
    """The station the terminal opens from; the shell's trax context names it."""

    requested_by: (
        Annotated[str, Field(max_length=63, pattern=r"^[\x20-\x7e]*$")] | None
    ) = None
    """The actor that placed the tile asking for it: shown and logged by the host, never trusted."""

    surface: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None = None
    """The view that opened it (Chat's terminal side, or one tile): it re-attaches to its own."""

    command: TerminalCommand | None = None
    """Start ``trax run`` of this CLI in the terminal, in place of a login shell."""

    attach: uuid.UUID | None = None
    """The AgentSession the run re-opens; needs ``command``."""

    run_name: str | None = Field(default=None, pattern=NAME_PATTERN)
    """The detached run's name, for ``trax run attach NAME``; needs ``command``."""

    cwd: RunFolder | None = None
    """The run's folder on the machine, absolute or ``~``; needs ``command``."""

    @model_validator(mode="after")
    def _run_fields_need_a_command(self) -> Self:
        """Refuse an ``attach``, ``run_name`` or ``cwd`` that names no CLI."""
        if self.command is None and (
            self.attach is not None or self.run_name is not None or self.cwd is not None
        ):
            raise ValueError("attach, run_name and cwd need a command")
        return self


class TerminalOpened(BaseModel):
    """A terminal was queued for the host; attach with the ticket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_id: uuid.UUID
    ticket: str = Field(repr=False)

    ice_servers: IceServers = Field(default_factory=list)
    """The STUN servers to find the host with; empty means the same network only."""


class TerminalTicket(BaseModel):
    """A one-use ticket to attach to a live terminal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket: str = Field(repr=False)

    ice_servers: IceServers = Field(default_factory=list)


class TerminalMachine(BaseModel):
    """One machine the caller owns, and whether it can take a terminal now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    status: MachineStatus
    terminal_ready: bool
    """Online, and its host waited for an open within :data:`WAIT_LEASE_SEC`."""

    confirmed: bool
    """Whether its owner confirmed it; a terminal opens only on a confirmed machine
    (409 ``terminal_unconfirmed``). An admin's enroll confirms; a writer's does not."""

    clis: list[str] = Field(default_factory=list)
    """The CLIs its facts list, such as ``claude`` or ``codex``, so a picker offers
    only what is installed."""

    cwd: RunFolder | None = None
    """The folder its owner last chose for a CLI run; None until one is named."""


class LiveTerminal(BaseModel):
    """A terminal that is open now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_id: uuid.UUID
    machine: str = Field(pattern=NAME_PATTERN)
    surface: str | None = None
    """The ``surface`` it was opened with."""


class TerminalMachines(BaseModel):
    """The caller's machines, and their live terminals for the asked station."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machines: list[TerminalMachine]
    """Newest first, by when each machine was registered."""

    terminals: list[LiveTerminal] = Field(default_factory=list)
    """The caller's live terminals opened from ``?workspace=``, oldest first."""


class TerminalWait(BaseModel):
    """An open request, as the host receives it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_id: uuid.UUID
    opener: str
    """The opener's email as the server claims it; the host logs it."""

    requested_by: str | None = None
    """Who asked for the terminal, as the opener's browser claimed; the host logs it."""

    rows: int
    cols: int
    ice_servers: IceServers = Field(default_factory=list)
    """The STUN servers to find the browser with."""

    workspace: uuid.UUID | None = None
    workstream: uuid.UUID | None = None

    command: TerminalCommand | None = None
    """As :attr:`OpenTerminal.command`: the CLI to start in place of a login shell."""

    attach: uuid.UUID | None = None
    """As :attr:`OpenTerminal.attach`: the AgentSession the run re-opens."""

    run_name: str | None = Field(default=None, pattern=NAME_PATTERN)
    """As :attr:`OpenTerminal.run_name`: the detached run's name."""

    cwd: RunFolder | None = None
    """As :attr:`OpenTerminal.cwd`: the folder the run starts in, on this machine."""


class DirectSignal(BaseModel):
    """One step of setting up a direct link, carried in a ``SIGNAL`` frame as JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gen: Annotated[int, Field(ge=1)]
    """The browser's attempt counter; the host ignores one not above the last it took."""

    type: Literal["offer", "answer", "abandon"]
    """``abandon``: the host cannot or will not take a direct link."""

    sdp: str = Field(default="", max_length=MAX_SIGNAL_BYTES)


def encode_route(route: int, offset: int) -> bytes:
    """Return the ``ROUTE`` payload: a route byte, then the u64 stream offset."""
    return bytes([route]) + offset.to_bytes(8)


def decode_route(payload: bytes) -> tuple[int, int]:
    """Return the route and offset a ``ROUTE`` payload carries.

    Args:
      payload: The frame's bytes.

    Returns:
      route: :data:`ROUTE_RELAY` or :data:`ROUTE_DIRECT`.
      offset: The stream offset the sender has reached.

    Raises:
      ValueError: The payload is not a known route and a u64.

    """
    if len(payload) != 9 or payload[0] not in {ROUTE_RELAY, ROUTE_DIRECT}:
        raise ValueError(f"ROUTE payload of {len(payload)} bytes")
    return payload[0], int.from_bytes(payload[1:])


def encode_keepalive(idle_ms: int) -> bytes:
    """Return the ``KEEPALIVE`` payload: milliseconds since the host last saw input."""
    return min(idle_ms, 2**32 - 1).to_bytes(4)


def decode_keepalive(payload: bytes) -> int:
    """Return the idle milliseconds a ``KEEPALIVE`` payload carries.

    Args:
      payload: The frame's bytes.

    Returns:
      idle_ms: Milliseconds since the host last saw input.

    Raises:
      ValueError: The payload is not a u32.

    """
    if len(payload) != 4:
        raise ValueError(f"KEEPALIVE payload of {len(payload)} bytes")
    return int.from_bytes(payload)
