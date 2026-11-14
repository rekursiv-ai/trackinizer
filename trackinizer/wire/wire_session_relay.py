"""Wire contract for a live session: an agent process's lines up, a person's inputs down.

A session is live while an agent process holds a socket on
:data:`SESSION_PROCESS_WS_PATH`. What each side says, one JSON object a frame:

::

    viewer                    server                     agent process
      GET lines   ─────────▶
                  ◀─────────    Opened
                                ◀──────────────────────    Hello
                  ◀─────────    ProcessLine
                                ◀──────────────────────    EventLine, StateLine,
                  ◀─────────    the same line                SettingsLine
                  ◀─────────    RecordLine (a record, once it is stored)
      NewInput    ─────────▶    ──────────────────────▶    Input
                  ◀─────────    InputSent

    person                    server                     daemon
      ProcessSpec ─────────▶    ──────────────────────▶    ServeSession
                  ◀─────────    AgentProcess
                                ◀──────────────────────    ProcessEnded

- A viewer reads :data:`SESSION_LINES_PATH`, a stream of server-sent frames.
- A person posts to :data:`SESSION_INPUTS_PATH`, and starts, reads and stops the
  process on :data:`SESSION_PROCESS_PATH`. A message sent to the session through
  ``POST /api/messages`` (``wire_sessions.SendMessage``), a person's or another
  session's agent's, comes down as an :class:`Input` too.
- The daemon is handed a start on its held ``GET`` of
  :data:`HOST_SESSIONS_WAIT_PATH`, and reports the process's end to
  :data:`HOST_SESSIONS_ENDED_PATH`.

Where an agent runs, where its tools run and where it signs in to its model are
three places, each some host, chosen apart. A start names the first and the third
(:class:`ProcessSpec`); the tools are on whichever hosts the agent is linked to. A
login is used on the host that holds it, so no key and no token crosses this wire.

A frame says what it is by its ``type``. An event and an input also name their
sagent class as ``kind`` and carry that object as ``payload``, as a stored record
does (``wire_session_ir.RecordBody``); the server passes a payload on unread.

A reader ignores a field its class lacks, so a newer peer may add one. The bodies a
person writes (:class:`NewInput`, :class:`Command`, :class:`ProcessSpec`) refuse
one, so a misspelt field is an error and not a default.

Each frame on the process socket is one text message of at most
:data:`MAX_FRAME_BYTES`. The server ends the socket with a close code of
``wire_terminals`` and a reason: ``CLOSE_ENDED`` when a person stopped the process,
``CLOSE_TAKEN_OVER`` when another process holds the session, ``CLOSE_REFUSED`` when
the key may not serve it, and ``CLOSE_HOST_REVOKED`` when its host was revoked. A
process ends on any of them and reconnects on every other close; one that ends
itself closes with 1000.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).

Typical usage example:

  frame = pydantic.TypeAdapter(ProcessFrame).validate_json(message)
  line = EventLine(kind="ModelResponsePartial", payload=payload).model_dump_json()
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trackinizer.wire.json_types import JSON, UtcDatetime
from trackinizer.wire.wire_session_ir import RecordBody
from trackinizer.wire.wire_sessions import MessageKind
from trackinizer.wire.wire_terminals import RunFolder


__all__ = [
    "HOST_SESSIONS_ENDED_PATH",
    "HOST_SESSIONS_WAIT_PATH",
    "INPUT_REFUSED",
    "LINES_PING_SEC",
    "LOGINS_FACT",
    "MAX_FRAME_BYTES",
    "MAX_REASON_CHARS",
    "NOT_LIVE",
    "NO_PLACE",
    "REMOTE_AGENTS_FACT",
    "SESSIONS_FACT",
    "SESSION_INPUTS_PATH",
    "SESSION_LINES_PATH",
    "SESSION_PROCESS_PATH",
    "SESSION_PROCESS_WS_PATH",
    "STALE_PART",
    "AgentProcess",
    "AgentState",
    "Command",
    "EventLine",
    "Hello",
    "Input",
    "InputKind",
    "InputSent",
    "NewInput",
    "Opened",
    "ProcessEnded",
    "ProcessFrame",
    "ProcessLine",
    "ProcessSpec",
    "RecordLine",
    "ServeSession",
    "Setting",
    "SettingsLine",
    "StateLine",
    "ViewerFrame",
]

SESSION_LINES_PATH: Final = "/api/sessions/{session_id}/lines"
"""``GET``: a viewer's stream, each frame a :data:`ViewerFrame`."""

SESSION_INPUTS_PATH: Final = "/api/sessions/{session_id}/inputs"
"""``POST`` a :class:`NewInput`; the answer is an :class:`InputSent`."""

SESSION_PROCESS_PATH: Final = "/api/sessions/{session_id}/process"
"""``PUT`` a :class:`ProcessSpec` to start one, ``GET`` it, ``DELETE`` to stop it;
each answers the :class:`AgentProcess`, or ``null`` for a session that has had none."""

SESSION_PROCESS_WS_PATH: Final = "/api/sessions/{session_id}/process/ws"
"""The agent process's socket: :data:`ProcessFrame` up, :class:`Input` down."""

HOST_SESSIONS_WAIT_PATH: Final = "/api/hosts/{host_id}/sessions/wait"
"""``GET ?instance=ID``, held as the tool wait is: the daemon's next
:class:`ServeSession`, or ``null`` when none came."""

HOST_SESSIONS_ENDED_PATH: Final = "/api/hosts/{host_id}/sessions/ended"
"""``POST`` a :class:`ProcessEnded`: a process the daemon started has exited."""

SESSIONS_FACT: Final = "sessions"
"""The fact a daemon reports as ``true`` when it starts agent processes."""

REMOTE_AGENTS_FACT: Final = "remote_agents"
"""The fact a daemon reports as ``true`` when it is a remote agent service: it
confines each agent process it starts, so that it may run agents for accounts other
than its own."""

LOGINS_FACT: Final = "logins"
"""The fact a daemon reports as ``true`` when its host's model login answers the
model calls of agents that run elsewhere."""

NOT_LIVE: Final = "not_live"
"""The ``code`` of a 409 for an input to a session no process serves."""

INPUT_REFUSED: Final = "input_refused"
"""The ``code`` of a 409 for an input the server will not hold for the session's
process: larger than a frame, or past what it holds unread; its message says which."""

NO_PLACE: Final = "no_place"
"""The ``code`` of a 409 for a start a named place cannot serve: its host starts
no agent process for the caller, or its login's host answers them no model call."""

STALE_PART: Final = "stale_part"
"""The ``code`` of a 409 for a record of a part that is no longer the session's
newest: another process continued the session."""

MAX_FRAME_BYTES: Final = 2 << 20
"""The largest message either end of the process socket sends."""

LINES_PING_SEC: Final = 25
"""The longest a viewer's stream stays silent before the server sends its
keep-alive. A reader that hears nothing for 2.5 times this has lost the stream."""

MAX_REASON_CHARS: Final = 4_096
"""The most characters a daemon reports of why a process ended, and of its output."""

type InputKind = Literal["UserMessage", "Halt", "Command"]
"""What a person may send the root agent: a line, a stop of the turn, or a slash
line."""


class Hello(BaseModel):
    """The first frame an agent process sends on each connection: who it is."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["hello"] = "hello"
    process_id: UUID
    """The :class:`ServeSession`'s, or one a process a person started minted."""

    version: str
    """The version of the client the process runs."""

    host: str
    """The name of the host it runs on."""


class AgentState(BaseModel):
    """What the root agent is doing now: the whole source of a viewer's status line."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    line: str
    """The status line as sagent's own terminal shows it, with no spinner."""

    busy: bool
    """Whether a model call, a tool call or a compaction runs."""

    since: UtcDatetime
    """When ``busy`` last changed; a viewer counts up from it while the agent is busy."""

    pending: tuple[JSON, ...] = ()
    """Each message sent mid-turn that waits for the model, in the events' codec."""


class AgentProcess(BaseModel):
    """The agent process serving a session, or the last one that did."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    session_id: UUID
    process_id: UUID
    host: str
    """The name of the host it runs on."""

    login_host: str = ""
    """The name of the host whose login makes its model calls; empty for its own."""

    state: Literal["starting", "attached", "detached"]
    reason: str = ""
    """Why it is ``detached``: ``stopped``, ``exited``, ``disconnected``, or what its
    daemon reported; empty otherwise."""

    failed: bool = False
    """Whether it is ``detached`` as a failure: given up on before its ``Hello``, ended
    while still starting, or reported by its daemon with an exit code other than 0,
    or none. A person's stop is no failure."""

    version: str = ""
    """Its client's version, once it has said :class:`Hello`."""

    since: UtcDatetime
    """When it entered ``state``."""

    output: str = ""
    """The end of its output, as its daemon reported it when it ended: answered by
    ``GET`` :data:`SESSION_PROCESS_PATH` to a writer of the session alone, and empty
    for anyone else and in every line a viewer reads."""


class EventLine(BaseModel):
    """One event the runtime published, or what a slash command said. Never stored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["event"] = "event"
    kind: str = Field(min_length=1)
    """The event's class name, which selects what ``payload`` holds."""

    payload: JSON
    """The event as the process's codec writes it; the server passes it on unread."""


class StateLine(BaseModel):
    """The agent's state, replacing the last one a viewer held."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["state"] = "state"
    state: AgentState


class Setting(BaseModel):
    """One thing a slash line can name: sagent's own ``Setting``, as data."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: tuple[str, ...] = Field(min_length=1)
    """One segment per level, such as ``("model", "thinking", "effort")``."""

    help_short: str
    """One line saying what it is."""

    help_long: str = ""
    usage: str = ""
    """What may follow the name, such as ``LEVEL``; empty for nothing."""

    value: str | None = None
    """Its current value; ``None`` for an action, which a bare name runs."""

    options: tuple[str, ...] = ()
    """What it may take; empty when unlisted."""

    flags: tuple[str, ...] = ()
    """An action's own options, such as ``--headless``."""


class SettingsLine(BaseModel):
    """Every setting a slash line can name now, replacing the last a viewer held."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["settings"] = "settings"
    settings: tuple[Setting, ...]


class RecordLine(BaseModel):
    """A record the server has stored, as ``GET .../records`` returns it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["record"] = "record"
    part: int
    """The number of the part that holds it."""

    record: RecordBody


class ProcessLine(BaseModel):
    """The process serving the session changed state."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["process"] = "process"
    process: AgentProcess


class Opened(BaseModel):
    """The first frame of a viewer's stream: where the session stands as it opens.

    The event lines of a reply still being written follow it, if one is; a finished
    reply is a record, read with the records.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["opened"] = "opened"
    state: AgentState | None = None
    """The agent's last state; ``None`` when no process serves the session."""

    process: AgentProcess | None = None
    """The process serving it, or the last that did; ``None`` before its first."""

    settings: tuple[Setting, ...] = ()
    """What a slash line can name; empty when no process serves the session."""


type ViewerFrame = Annotated[
    Opened | EventLine | StateLine | SettingsLine | RecordLine | ProcessLine,
    Field(discriminator="type"),
]
"""One frame of a viewer's stream: an :class:`Opened` first, then the event lines of a
reply still being written, then each line."""

type ProcessFrame = Annotated[
    Hello | EventLine | StateLine | SettingsLine,
    Field(discriminator="type"),
]
"""One frame an agent process sends: a :class:`Hello` first, then its lines."""


class Command(BaseModel):
    """What a ``Command`` input carries: one slash line, as typed at the agent's prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    line: str = Field(pattern="^/")
    """It starts with ``/``: the agent's prompt takes any other line for a user's own
    message, which names no sender."""


class NewInput(BaseModel):
    """What a person posts for the session's root agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    """The sender's own id for it, which the answer and the line it is stored as
    repeat, so a sender whose answer is lost still knows it; a fresh one when the
    body names none."""

    kind: InputKind
    payload: JSON
    """That sagent message in the events' codec; for ``Command``, a :class:`Command`."""

    @model_validator(mode="after")
    def _a_command_is_a_slash_line(self) -> Self:
        """Refuse a ``Command`` whose payload is not one slash line."""
        if self.kind == "Command":
            _ = Command.model_validate(self.payload)
        return self


class InputSent(BaseModel):
    """The answer to a posted input: the id its effect is told back under."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID


class Input(BaseModel):
    """An input as the server sends it to the agent process."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["input"] = "input"
    id: UUID
    author: str
    """Who sent it, as the server attests: a person's email, or ``label#number`` for
    an agent of another session."""

    kind: InputKind
    payload: JSON
    recipient: str = ""
    """The agent it is for, by its label in the session; empty for the root."""

    booked: MessageKind | None = None
    """How the sending agent's team booked it; ``None`` for a person's input."""


class ProcessSpec(BaseModel):
    """Where to bring up a session's agent process, and with which sagent flags.

    The two places it names are independent: any host that runs agents, with any
    host's login.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = ""
    """The name of the host the agent runs on; empty for the one the server runs
    agents on."""

    login_host: str = ""
    """The name of the host whose model login the agent uses, and which sends its
    model calls; empty for the agent's own host."""

    cwd: RunFolder = "~"
    """The folder on ``host`` to start in."""

    args: tuple[str, ...] = ()
    """sagent's own command-line flags, as a person would type them."""


class ServeSession(BaseModel):
    """A start, as the daemon receives it and hands it whole to the process."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    session_id: UUID
    process_id: UUID
    token: str = Field(repr=False)
    """An API key for this process alone, revoked when it is stopped or ends."""

    spec: ProcessSpec
    remote: bool = False
    """Whether the server placed it with a remote agent service, whose daemon must
    confine it: one that confines nothing refuses it."""


class ProcessEnded(BaseModel):
    """A daemon's report that a process it started exited, or never started."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    process_id: UUID
    exit_code: int | None
    """Its exit status; ``None`` when it could not be started, or ended unwatched."""

    reason: str = Field(max_length=MAX_REASON_CHARS)
    """Why it ended in the daemon's own words: its exit code, or why it could not be
    started. Every reader of the session is told it."""

    output: str = Field(default="", max_length=MAX_REASON_CHARS)
    """The end of its output, which may hold anything the process printed: only a
    writer of the session reads it."""
