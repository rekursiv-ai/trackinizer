"""Wire contract for agent-session lifecycle, messaging, and the feed.

Sessions captured by ``trax run`` flow to trackinizer through ``start``
(mint the ``AgentSession`` row) and ``end`` (mark it closed); the records
themselves ride the separate :mod:`wire.wire_session_ir` contract. This
module is the single source for these request/response shapes and their
path templates; the server registers handlers against them and the client
builds requests from them, so neither can drift.

It also carries the cross-session console feed (:class:`FeedEvent`,
:class:`FeedCursor`), its counts (:class:`FeedFacetsResponse`,
:class:`FeedHistogramResponse`) and the inbound/routed messaging bodies --
everything about a session that is not one of its records.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, Self

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trackinizer.wire.json_types import JSON, UtcDatetime
from trackinizer.wire.wire_science_chat import ChatForkAt


_MAX_MESSAGE_CHARS: Final = 16_384
"""Upper bound on an inbound/routed message body. Caps the bytes a single
``inject`` writes to the PTY under the pump lock (each write stalls human
keystrokes for its duration) and the memory a process-local queue holds."""

_MAX_ACCOUNT_CHARS: Final = 254
"""Upper bound on an account email, which RFC 5321 caps at 254 characters."""


# Every textual identity/scope field on these bodies (``cli``, ``actor``,
# ``cli_session_id``, ``room``) is matched or stored verbatim, so a whitespace-only
# value can never match and is almost certainly a client bug; reject it at the boundary
# rather than silently never-deliver or store junk. ``Field(min_length=1)`` alone admits
# ``" "``, so each such field binds this validator. The one rule for every scalar string
# field, mirroring the list-element ``_reject_blank_strings`` rule in ``bodies.py``.
def _reject_blank(value: str | None) -> str | None:
    """Reject an empty-or-whitespace scalar string; pass ``None`` through."""
    if value is not None and not value.strip():
        raise ValueError("value must be non-empty")
    return value


__all__ = [
    "CAMPAIGN_ASKER_INFIX",
    "CHAT_RUN_LIVE",
    "CHAT_TAKEN_OVER",
    "CHAT_TAKEN_OVER_MESSAGE",
    "CLI_REQUIRED",
    "CONSOLE_RELAY_MARK",
    "FEED_FACETS_PATH",
    "FEED_HISTOGRAM_PATH",
    "FEED_PATH",
    "GIT_LABEL_PREFIX",
    "PROVIDER_IS_ASSISTANT",
    "PROVIDER_IS_CLI",
    "PROVIDER_IS_OTHER_CLI",
    "RUN_ENDING_REFUSALS",
    "SEND_MESSAGE_PATH",
    "SESSION_API_PATHS",
    "SESSION_AUDIENCE_PATH",
    "SESSION_END_PATH",
    "SESSION_INBOUND_PATH",
    "SESSION_PARTS_PATH",
    "SESSION_PLACE_PATH",
    "SESSION_REATTACH_PATH",
    "SESSION_RECORDS_PATH",
    "SESSION_RUN_STATE_PATH",
    "SESSION_START_PATH",
    "SESSION_WORKSTREAM_PATH",
    "STATION_LABEL_PREFIX",
    "VERSION_PATH",
    "ConsoleRelay",
    "ConsoleRelayReceipt",
    "ConsoleTarget",
    "DrainInboundResponse",
    "FeedActorFacet",
    "FeedBucket",
    "FeedCursor",
    "FeedEvent",
    "FeedFacetsResponse",
    "FeedHistogramResponse",
    "FeedKindFacet",
    "FeedResponse",
    "FeedRoomFacet",
    "FeedWhose",
    "GitRef",
    "InboundDrainItem",
    "InboundEnqueueRequest",
    "InboundEnqueueResponse",
    "InboundKind",
    "MoveSession",
    "PlaceSession",
    "RunState",
    "SendMessage",
    "SendMessageResponse",
    "SessionAudienceChange",
    "SessionAudienceResponse",
    "SessionEnd",
    "SessionEndResponse",
    "SessionPlacement",
    "SessionRunState",
    "SessionStart",
    "SessionStartResponse",
    "WorkspaceMark",
    "WorkspaceMessageContext",
    "WorkspaceRecordContext",
    "WorkspaceVisibleVisual",
    "campaign_asker_label",
    "session_audience_path",
    "session_place_path",
    "session_reattach_path",
    "session_run_state_path",
]


CAMPAIGN_ASKER_INFIX: Final = ":asker:"
"""What joins a campaign's slug and the email of the person who launched it, in the
label ``<slug>:asker:<email>`` its brief carries: the feed's ``whose=campaigns`` keeps
the sessions labelled with, or in a room named for, a slug so labelled with the
caller."""


type FeedWhose = Literal["mine", "campaigns", "everyone"]
"""Whose sessions a feed read keeps: the caller's own, those of the campaigns the
caller launched, or every one the caller reads."""


def campaign_asker_label(slug: str, email: str) -> str:
    """Return the label that names who launched a campaign, for its brief.

    Args:
      slug: The campaign's slug, which its lead's session carries as a label.
      email: The launcher's account.

    Returns:
      label: ``<slug>:asker:<email>``, the email lowercase.

    """
    return f"{slug}{CAMPAIGN_ASKER_INFIX}{email.lower()}"


class SessionStart(BaseModel):
    """Open a new session: mint an ``AgentSession`` row, return its id.

    Mirrors the AgentSession submit fields; the server fills ``owner`` from
    the authenticated principal. The client never names the session id --
    it is server-minted and learned from the response.
    """

    cli: str = Field(min_length=1)
    """Wrapped CLI: ``claude`` / ``gemini`` / ``codex`` / ``cursor``."""

    cli_session_id: str | None = Field(default=None, min_length=1)
    """The CLI's own session id, when known at start; may be backfilled via
    ``end`` if the CLI only reveals it later."""

    attach: uuid.UUID | None = None
    """An existing AgentSession this run re-opens, appending a new part and keeping its
    ``cli_session_id``; exclusive with ``cli_session_id``."""

    title: str | None = None
    started: UtcDatetime | None = None
    actor: str | None = None
    account: str | None = None
    """The active user the session row is attributed to. ``None`` defaults to
    the authenticated creator; a non-``None`` value must be a live active user
    (validated server-side). See :attr:`Inquiry.account`."""

    rooms: list[str] | None = None
    """Initial room membership (``trax run --room``); namespaces the session
    can be addressed within. See :attr:`AgentSession.rooms`."""

    idempotency_key: uuid.UUID | None = None
    """Optional client-supplied key; a repeat ``start`` with the same key
    returns the original session id without minting a duplicate."""

    private: bool = False
    """Open the session hidden: ``private``, read only by its owner and the people who
    oversee. A key pinned to another audience than ``open`` or ``private`` is refused
    (403 ``audience_not_held``). A server that predates the field ignores it, which
    :attr:`SessionStartResponse.audience` then shows."""

    _reject_blank_scalars = field_validator(
        "cli",
        "cli_session_id",
        "actor",
        "account",
        mode="after",
    )(staticmethod(_reject_blank))

    # A room name must be a single clean token: non-blank (matched verbatim against
    # ``agentsession_rooms``) and comma-free (``trax run`` exports rooms comma-joined
    # into ``TRAX_ROOMS``, so a room ``'a,b'`` is indistinguishable from two rooms
    # ``'a'`` and ``'b'``). Mirrors the ``SubmitAgentSession.rooms`` rule so both create
    # paths agree.
    @field_validator("rooms", mode="after")
    @classmethod
    def _validate_rooms(cls, value: list[str] | None) -> list[str] | None:
        """Reject blank or comma-bearing room names."""
        if value is not None:
            for room in value:
                if not room.strip():
                    raise ValueError("rooms must be non-empty")
                if "," in room:
                    raise ValueError(f"room name must not contain a comma: {room!r}")
        return value

    @model_validator(mode="after")
    def _attach_excludes_cli_session_id(self) -> Self:
        """Refuse a start that re-opens a session and also names a CLI session id."""
        if self.attach is not None and self.cli_session_id is not None:
            raise ValueError("attach and cli_session_id are exclusive")
        return self


class SessionStartResponse(BaseModel):
    """The server-minted identity of a freshly opened (or resumed) session."""

    id: uuid.UUID
    seq: int
    """The event log's continuation seq: 0 for a fresh session, ``max(seq)+1``
    for a resumed one. The client seeds its sequence from this so a resumed run
    appends to the existing log instead of colliding at seq 0."""

    cli_session_id: str | None = None
    actor: str | None = None
    """The granted routing name. Equals the requested ``--as`` actor unless it
    collided with a live session, in which case the server appended a suffix
    (``scientist`` -> ``scientist#2``); the client adopts whatever is here."""

    audience: str | None = None
    """Who reads the session's transcript, as the server holds it; ``None`` from a
    server that predates the field."""


STATION_LABEL_PREFIX: Final = "station:"
"""A placed session's label naming its station: ``station:<workspace uuid>``."""

GIT_LABEL_PREFIX: Final = "git:"
"""A placed session's label naming its checkout: ``git:<origin url>@<branch>``."""


class GitRef(BaseModel):
    """Where a run's checkout stands: its origin URL and branch."""

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=500)
    """The ``origin`` remote URL. The server strips any credentials from it."""

    branch: str = Field(min_length=1, max_length=255)


class PlaceSession(BaseModel):
    """Ask the server to place a session in a workstream and a station."""

    model_config = ConfigDict(extra="forbid")

    workspace: uuid.UUID | None = None
    """The station the run was started from (``TRAX_WORKSPACE``), if any."""

    git: GitRef | None = None
    """The run's checkout, when it is on a branch of a git work tree."""


class SessionPlacement(BaseModel):
    """Where a session was placed; both ids are ``None`` for a session not placed."""

    workstream_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None


class MoveSession(BaseModel):
    """The workstream a session is moved to."""

    model_config = ConfigDict(extra="forbid")

    workstream_id: uuid.UUID


class FeedEvent(BaseModel):
    """One captured turn plus the session context the console needs.

    The cross-session feed (the multi-agent console) interleaves turns from
    every session into one time-ordered stream, so each item must carry which
    session it came from and that session's routing identity -- unlike a
    record read through :mod:`wire.wire_session_ir`, which is always read in
    the context of one known session and part. ``created`` (the server write
    clock) is the feed's order key, not the per-part ``seq``, because ``seq``
    is only monotonic within a part.
    """

    session_id: uuid.UUID
    actor: str
    """The session's routing name (``owner``); the feed's per-agent label."""

    rooms: list[str] = Field(default_factory=list)
    cli: str | None = None
    """The session's wrapped CLI; carried for a future per-CLI console badge,
    not yet rendered."""

    part: int = 0
    """Which source FILE the record came from.

    A session spans several (claude splits on compaction, codex forks), and
    ``seq`` restarts within each -- so the pair is what identifies a record.
    Legacy turns backfilled from ``agent_session_events`` carry ``-1``, a
    reserved namespace that cannot collide with a real part."""

    seq: int = Field(ge=0)
    """Position within ``part``. Named ``seq`` rather than ``idx`` because the
    console's cursor protocol is public; it IS the record's ``idx``."""

    kind: str = Field(min_length=1)
    """The record class's name. Widened from the closed 8-member legacy
    Literal: the IR has 21 concrete kinds and a console that rejected an
    unknown one would break on every new record type."""

    created: datetime
    """Server write clock -- the feed's cross-session order key."""

    timestamp: datetime | None = None
    model: str | None = None
    message: JSON = Field(default_factory=dict)
    """The record's payload, under the legacy field name so the console's
    renderer needs no rewrite for the shape it already reads."""

    text: str = ""
    """The record's searchable prose, which the console renders directly."""


class FeedCursor(BaseModel):
    """A keyset cursor into the feed's ``(created, session_id, part, seq)`` order.

    The cursor must carry EVERY order-key component, not just ``created``: a
    page boundary can fall inside a group of rows sharing a ``created``
    instant, and a bare-``created`` resume (``> created``) would skip every
    tied row after it. ``part`` joined the key when the feed moved to IR
    records, since ``seq`` restarts within each source file.
    """

    created: datetime
    session_id: uuid.UUID
    part: int = 0
    """Part of the order key, so a boundary inside one session's records does
    not skip the rest."""

    seq: int = Field(ge=0)


class FeedResponse(BaseModel):
    """One page of the cross-session console feed, oldest first.

    ``events`` is ordered by ``(created, session_id, part, seq)`` so the console can
    append in order and resume from ``next_after`` on its next poll. The cursor
    is composite (see :class:`FeedCursor`) so a same-``created`` tie split
    across a page boundary is not skipped. ``next_after`` is ``None`` only when
    the page is empty and no prior cursor was supplied (a poll that drained the
    tail echoes back the cursor it was given).
    """

    events: list[FeedEvent]
    next_after: FeedCursor | None = None


type RunState = Literal["idle", "running", "requires_action", "taken_over"]
"""What a chat's run says it is doing, so Chat can show it.

Values:
  idle: Waiting for the person's next line.
  running: Working on a turn.
  requires_action: Waiting for the person to answer an ask.
  taken_over: Stopped as a headless run because someone opened its CLI in a terminal;
    Chat refuses lines until the person reattaches.
"""


class FeedActorFacet(BaseModel):
    """One session's share of a feed window, as the console's agent list shows it."""

    actor: str
    """The session's routing name (``owner``), as :class:`FeedEvent` carries it."""

    session_id: uuid.UUID
    cli: str | None
    rooms: list[str]
    count: int = Field(ge=0)
    """Records in the window."""

    conversation: int = Field(ge=0)
    """Of ``count``, the records that are conversation: what a person or agent
    said, not tool calls, thinking or bookkeeping."""

    last: datetime
    """``created`` of the session's newest record in the window."""

    ended: datetime | None
    """When the session ended; ``None`` while it is live."""

    account: str = ""
    """Who the session is held under."""

    audience: str | None = None
    """Who reads its transcript: ``open``, ``private`` or ``staff``."""

    hideable: bool = False
    """Whether the caller may hide the session, or show it again: its owner, while it
    is not ``staff``. Never for a science chat, which is hidden from Chat."""

    state: RunState = "idle"
    """What its run is doing while a poller drains it: ``requires_action`` while an ask
    is open, ``taken_over`` after its CLI was opened in a terminal, else what the run
    last reported. ``idle`` for a session no run drains."""


class FeedRoomFacet(BaseModel):
    """One room's share of a feed window."""

    room: str
    count: int = Field(ge=0)
    """Records in the window from sessions in the room."""

    actors: list[str]
    """The routing names of those sessions."""


class FeedKindFacet(BaseModel):
    """One record kind's share of a feed window."""

    kind: str
    count: int = Field(ge=0)


class FeedFacetsResponse(BaseModel):
    """What a feed window holds, by session, room and record kind.

    ``actors`` is newest ``last`` first; ``rooms`` and ``kinds`` are largest
    ``count`` first.
    """

    actors: list[FeedActorFacet]
    rooms: list[FeedRoomFacet]
    kinds: list[FeedKindFacet]


class FeedBucket(BaseModel):
    """Records written in ``[start, start + bucket_seconds)``."""

    start: datetime
    count: int = Field(ge=0)


class FeedHistogramResponse(BaseModel):
    """Feed records per time bucket, every bucket from ``start`` to ``end``.

    Buckets are ``bucket_seconds`` wide and aligned to the Unix epoch, so the
    first starts at or before the window's start and the last ends after its
    end; buckets with no records are listed with a ``count`` of 0.
    """

    start: datetime
    end: datetime
    bucket_seconds: int = Field(ge=1)
    counts: list[FeedBucket]


type InboundKind = Literal["line", "command", "answer", "settings", "interrupt"]
"""What an inbound message is, so the run delivers it the way its CLI expects.

Values:
  line: A person's chat line, delivered with its sender.
  command: A slash command typed verbatim, such as ``/compact``, with no sender.
  answer: A decision on a pending prompt. Its payload names the prompt.
  settings: A model or effort change.
  interrupt: Stops the running turn.

Payload by kind: ``answer`` is ``{"prompt_id": str, "option": str, "updated_input":
object | null, "message": str}``, where ``option`` is the id of one of the ask's own
options; the older ``"decision": "allow" | "deny"`` stands in for ``option``, and an
answer names one of the two. ``settings`` is ``{"model": str | null, "effort": str |
null}``. The other kinds carry no payload.
"""


class SessionRunState(BaseModel):
    """What a run reports of itself, between its drains of the session's queue."""

    model_config = ConfigDict(extra="forbid")

    state: RunState
    instance: str = Field(pattern=r"^[0-9a-f]{32}$")
    """The run's own id, as its inbound drains name it. A report from a run other than
    the one holding the session's lease changes nothing."""


class InboundEnqueueRequest(BaseModel):
    """A message a client asks trackinizer to inject into a live session.

    The enqueue **request** carries ``text`` -- the one field the route delivers --
    and, from the configured assistant alone, the ``asker`` it sends for. ``source``
    is attested by the route from the authenticated principal (a client cannot forge
    another sender), and ``room`` has no enqueue semantics (the direct-session
    enqueue route discards it; routed delivery is the separate ``@actor:room``
    send-message path). ``extra="forbid"`` makes a client-sent ``source`` or ``room``
    a 422 rather than a silently-ignored field -- the request and response shapes are
    deliberately distinct (the response :class:`InboundDrainItem` carries both
    ``source`` and ``room``), so neither can be confused for the other.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)
    """The message as a person reads it: for ``answer`` the one-line form, "Yes" or
    "No: <reason>"; for ``interrupt``, "interrupt"."""

    kind: InboundKind = "line"
    """What the message is; a person's line unless the client names another kind."""

    payload: dict[str, object] | None = None
    """The kind's structured data, as :data:`InboundKind` documents it; ``None`` for
    the kinds that carry none."""

    asker: str | None = Field(default=None, max_length=_MAX_ACCOUNT_CHARS)
    """The account of the user the configured assistant sends this line for.

    The server lets the assistant send to a session the assistant cannot read only
    for a user who may write to it (that session's account, or a writer who holds its
    audience), so the assistant names whom it speaks for. Any other sender's ``asker``
    is ignored.
    """

    _validate_scalars = field_validator("asker", mode="after")(
        staticmethod(_reject_blank),
    )


class WorkspaceRecordContext(BaseModel):
    """A graph record the sender sees on screen, with a bounded title preview."""

    id: uuid.UUID
    kind: str
    seq: int
    title: str = Field(max_length=512)


class WorkspaceVisibleVisual(BaseModel):
    """A visual present on the canvas when a message was sent."""

    id: uuid.UUID
    type: str
    record: WorkspaceRecordContext | None = None
    """The record the visual shows, when it shows one that exists."""

    params: dict[str, str | int | bool] = Field(default_factory=dict)
    """The visual's parameters as the canvas holds them, such as the narrowing a
    reader set in the pane, so an agent that shows it again can keep them."""


class WorkspacePage(BaseModel):
    """A page the sender was on, with the record it shows when it names one."""

    route: str
    """The `#/...` address."""

    record: WorkspaceRecordContext | None = None


class WorkspaceMark(BaseModel):
    """A red box drawn on the canvas since the sender's last line, with what it covers."""

    id: uuid.UUID
    """What ``trax workspace ID mark remove`` takes."""

    author: str
    page: str | None = None
    """The `#/...` page it was drawn on."""

    left: float = Field(default=0.0, ge=0, le=1)
    top: float = Field(default=0.0, ge=0, le=1)
    right: float = Field(default=0.0, ge=0, le=1)
    bottom: float = Field(default=0.0, ge=0, le=1)
    """Where it stands, as fractions of the width and height of the ``tile``'s body,
    or of the stage when there is no tile."""

    tile: WorkspaceVisibleVisual | None = None
    """The visual it was drawn on, when it was drawn on one."""

    instances: list[WorkspaceVisibleVisual] = Field(default_factory=list)
    """The visuals under it that are still on the canvas."""

    records: list[WorkspaceRecordContext] = Field(default_factory=list)
    """The records under it that exist."""


class ConsoleTarget(BaseModel):
    """An agent a Console line was sent to, as ``@actor`` or ``@actor:room``."""

    actor: str = Field(min_length=1)
    room: str | None = None

    _validate_scalars = field_validator("actor", "room", mode="after")(
        staticmethod(_reject_blank),
    )


CONSOLE_RELAY_MARK: Final = "→ "
"""What a Console relay line starts with in a station's chat: an arrow, then a space.
The web draws a line that starts with it as a relay, neither the person's nor the
partner's."""


class ConsoleRelay(BaseModel):
    """What a person sent agents directly from the Console, told to their station's chat.

    Posted by the browser after the per-agent sends, and carried in the context of
    the message the chat's partner then receives. The partner records it in the
    chat as :meth:`line` and answers nothing, but reads it in its next prompt, so it
    never contradicts what the person told the agents.
    """

    targets: list[ConsoleTarget] = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)

    def line(self) -> str:
        """Return the chat line that records the relay: ``-> a, b:room: text``.

        Returns:
          line: The relay mark, each target as ``actor`` or ``actor:room``, then
            the text.

        """
        names = ", ".join(
            target.actor if target.room is None else f"{target.actor}:{target.room}"
            for target in self.targets
        )
        return f"{CONSOLE_RELAY_MARK}{names}: {self.text}"


class ConsoleRelayReceipt(BaseModel):
    """Whether the station's chat partner took a Console relay."""

    delivered: bool
    """False when the station has no chat, or its partner is not running or is full.
    The agents already have the message, so this is not an error."""


class WorkspaceMessageContext(BaseModel):
    """Server-derived canvas context attached to an inbound message."""

    workspace_id: uuid.UUID
    visible_visuals: list[WorkspaceVisibleVisual]
    agent_instructions: str | None = Field(default=None, max_length=8_192)
    continuation_record_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    """The Chat conversation the message belongs to; an assistant answers there."""

    fork: ChatForkAt | None = None
    """Set on the first line of a fork: the line of another conversation's session it
    starts from. The assistant opens the conversation with the lines up to it."""

    page: WorkspacePage | None = None
    """The page the sender is on as they send: what "this" means to them."""

    trail: list[WorkspacePage] = Field(default_factory=list)
    """The pages they came through before it, oldest first."""

    marks: list[WorkspaceMark] = Field(default_factory=list)
    """The boxes drawn on the station since its last line, oldest first."""

    relay: ConsoleRelay | None = None
    """Set when the message is not a question for the partner but a note of what the
    person told agents from the Console; the message text is the relay's text."""


class InboundDrainItem(BaseModel):
    """One message drained for a session, with its attested sender.

    The drain **response** item: unlike the enqueue request, it carries
    ``source`` -- the producer's attested ``--as`` / principal -- so the
    poller can render the ``[room] sender:`` injection context. Inbound-only
    and transient: it is **not** an ``agent_session_events`` row; when the
    agent consumes it, the capture path logs it as a normal turn.
    """

    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)
    """The message as a person reads it; see :attr:`InboundEnqueueRequest.text`."""

    kind: InboundKind = "line"
    """What the message is, as the enqueue named it; see :data:`InboundKind`."""

    payload: dict[str, object] | None = None
    """The kind's structured data, as :data:`InboundKind` documents it."""

    source: str | None = None
    source_role: str | None = None
    """The sender's role (``viewer``, ``writer`` or ``admin``) as the server attested
    it, beside ``source``; ``None`` for a sender the server cannot name, and from a
    server that predates the field."""

    room: str | None = None
    """The room a routed message was scoped to, for the ``[room] sender:``
    injection prefix; ``None`` for a direct (session-id) enqueue."""

    context: WorkspaceMessageContext | None = None

    _validate_room = field_validator("room", mode="after")(staticmethod(_reject_blank))


class InboundEnqueueResponse(BaseModel):
    """Receipt for an enqueued inbound message.

    ``queued`` is the current count pending for the session after this enqueue,
    including on an idempotent retry. It is
    an honest receipt -- the message is queued for the session's poller, not
    proven delivered; a session whose ``trax run`` is gone never drains it.
    """

    queued: int


class DrainInboundResponse(BaseModel):
    """The inbound messages drained for one session (oldest first)."""

    messages: list[InboundDrainItem]


class SendMessage(BaseModel):
    """Send a message to live sessions named by ``@actor[:room]``.

    The server resolves the actor (optionally scoped to a room) to the live
    sessions that match and enqueues the text to each. The sender is attested
    by the route from the authenticated principal, never the body, so a forged
    ``source`` / sender field is rejected (``extra="forbid"``) rather than
    silently ignored -- symmetric with :class:`InboundEnqueueRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1)
    """Target routing name (an ``AgentSession`` owner)."""

    room: str | None = None
    """Optional room scope; when set, only sessions in that room match."""

    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)
    """The message body to inject into each matched session."""

    asker: str | None = Field(default=None, max_length=_MAX_ACCOUNT_CHARS)
    """The account of the user the configured assistant sends this message for.

    See :attr:`InboundEnqueueRequest.asker`: the assistant names whom it speaks for,
    and any other sender's ``asker`` is ignored.
    """

    _validate_scalars = field_validator("actor", "room", "asker", mode="after")(
        staticmethod(_reject_blank),
    )


class SendMessageResponse(BaseModel):
    """Receipt for a ``send``: which live sessions the message was queued to.

    ``delivered`` lists the session ids enqueued (drop-if-absent: a session
    whose ``trax run`` is gone may never drain it). Empty means no live
    session matched the target -- the honest "undelivered" signal.
    """

    delivered: list[uuid.UUID]


class SessionAudienceChange(BaseModel):
    """Hide a session from everyone but its owner and those who oversee, or show it."""

    model_config = ConfigDict(extra="forbid")

    audience: Literal["open", "private"]
    """``private`` hides the session; ``open`` shows it again."""


class SessionAudienceResponse(BaseModel):
    """The audience a session now has, and every session that moved with it."""

    session_id: uuid.UUID
    audience: Literal["open", "private"]
    sessions: list[uuid.UUID]
    """The sessions whose audience this changed: the one named, or for a science
    chat every session under its id. Empty when it already had the audience."""


class SessionEnd(BaseModel):
    """Mark a session closed, optionally backfilling late-known fields."""

    ended: UtcDatetime | None = None
    cli_session_id: str | None = Field(default=None, min_length=1)
    """Set when the CLI only revealed its session id mid-run."""

    actor: str | None = None

    cli: str | None = Field(default=None, min_length=1)
    """The CLI of the run that ends the session, as its inbound drain names it."""

    instance: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    """The id of the run that ends the session, as its inbound drain names it. With
    ``cli``, it lets the server tell the end of a run it replaced, or of one beside
    the run that holds the chat, from the end of the run that holds it."""

    _reject_blank_scalars = field_validator("cli_session_id", "actor", mode="after")(
        staticmethod(_reject_blank),
    )


class SessionEndResponse(BaseModel):
    """Confirmation that a session was closed."""

    id: uuid.UUID
    ended: datetime | None = None


# API route paths -- wire contract shared with the client, not tunables.
SESSION_START_PATH: Final = "/api/sessions/start"
SESSION_RECORDS_PATH: Final = "/api/sessions/{session_id}/records"
SESSION_PARTS_PATH: Final = "/api/sessions/{session_id}/parts"
SESSION_END_PATH: Final = "/api/sessions/{session_id}/end"
SESSION_AUDIENCE_PATH: Final = "/api/sessions/{session_id}/audience"
SESSION_INBOUND_PATH: Final = "/api/sessions/{session_id}/inbound"
SESSION_RUN_STATE_PATH: Final = "/api/sessions/{session_id}/run-state"
SESSION_REATTACH_PATH: Final = "/api/sessions/{session_id}/reattach"
SESSION_PLACE_PATH: Final = "/api/sessions/{session_id}/place"
SESSION_WORKSTREAM_PATH: Final = "/api/sessions/{session_id}/workstream"
SEND_MESSAGE_PATH: Final = "/api/messages"
VERSION_PATH: Final = "/api/version"
FEED_PATH: Final = "/api/web/feed"
FEED_FACETS_PATH: Final = "/api/web/feed/facets"
FEED_HISTOGRAM_PATH: Final = "/api/web/feed/histogram"

# The ``code`` of a science chat's refused drain (``GET /api/sessions/{id}/inbound``).
# The chat's station names who answers it, and its queue has one drainer and one run.
PROVIDER_IS_CLI: Final = "provider_is_cli"
"""The station answers the chat with a CLI, and the assistant's key drained it."""

PROVIDER_IS_ASSISTANT: Final = "provider_is_assistant"
"""The station answers the chat with the assistant, and a run drained it."""

PROVIDER_IS_OTHER_CLI: Final = "provider_is_other_cli"
"""The station answers the chat with one CLI, and a run of the other drained it."""

CHAT_RUN_LIVE: Final = "chat_run_live"
"""Another run of the chat's CLI holds its lease: a chat has one live run at a time."""

CLI_REQUIRED: Final = "cli_required"
"""The chat is answered by a CLI and the drain named none, as a ``trax`` older than the
name does: the server cannot tell which CLI polls, so it takes no lease."""

RUN_ENDING_REFUSALS: Final = frozenset(
    {PROVIDER_IS_ASSISTANT, PROVIDER_IS_OTHER_CLI, CHAT_RUN_LIVE},
)
"""The refusals after which no drain of that run can succeed, so ``trax run`` stops
the run it is polling for, instead of retrying a drain the chat will keep refusing."""


CHAT_TAKEN_OVER: Final = "chat_taken_over"
"""The ``code`` of a post refused because the chat's CLI was opened in a terminal."""

CHAT_TAKEN_OVER_MESSAGE: Final = (
    "taken over in a terminal; reattach from Chat or Console"
)
"""Why Chat refuses a post to a chat whose run is :data:`RunState` ``taken_over``."""


# Every non-field API path the client, SPA, or deploy probe depends on, as a
# FastAPI route template. These routes are hand-registered (unlike the derived
# inquiry-field table), so this registry is the single source a drift test
# checks against the live app -- the analogue of ``inquiry_field_routes`` for
# the session/messaging/feed/version family. A new route in this family belongs
# here so the same drift guard covers it.
SESSION_API_PATHS: tuple[str, ...] = (
    SESSION_START_PATH,
    SESSION_RECORDS_PATH,
    SESSION_PARTS_PATH,
    SESSION_END_PATH,
    SESSION_AUDIENCE_PATH,
    SESSION_INBOUND_PATH,
    SESSION_RUN_STATE_PATH,
    SESSION_REATTACH_PATH,
    SESSION_PLACE_PATH,
    SESSION_WORKSTREAM_PATH,
    SEND_MESSAGE_PATH,
    VERSION_PATH,
    FEED_PATH,
    FEED_FACETS_PATH,
    FEED_HISTOGRAM_PATH,
)


def session_records_path(session_id: uuid.UUID) -> str:
    """Return the record append/read path for one session."""
    return SESSION_RECORDS_PATH.format(session_id=session_id)


def session_end_path(session_id: uuid.UUID) -> str:
    """Return the end path for one session."""
    return SESSION_END_PATH.format(session_id=session_id)


def session_place_path(session_id: uuid.UUID) -> str:
    """Return the path that places one session in a workstream and station."""
    return SESSION_PLACE_PATH.format(session_id=session_id)


def session_audience_path(session_id: uuid.UUID) -> str:
    """Return the path that hides one session or shows it again."""
    return SESSION_AUDIENCE_PATH.format(session_id=session_id)


def session_inbound_path(session_id: uuid.UUID) -> str:
    """Return the inbound-message path for one session (POST enqueue, GET drain)."""
    return SESSION_INBOUND_PATH.format(session_id=session_id)


def session_reattach_path(session_id: uuid.UUID) -> str:
    """Return the path that reopens a chat's run headless."""
    return SESSION_REATTACH_PATH.format(session_id=session_id)


def session_run_state_path(session_id: uuid.UUID) -> str:
    """Return the path a run reports its state to."""
    return SESSION_RUN_STATE_PATH.format(session_id=session_id)


def inbound_read_timeout(wait_sec: float) -> float | None:
    """Return the read timeout a drain that holds ``wait_sec`` needs.

    Args:
      wait_sec: How long the server may hold the drain request open.

    Returns:
      timeout: Seconds the client may wait to read the response, or ``None``
        to keep the transport's own timeout when the drain does not wait.

    """
    # The server returns an empty drain AT ``wait_sec``, so a read deadline equal to
    # it races that response and turns a normal empty result into a transport error.
    return wait_sec + 10.0 if wait_sec else None
