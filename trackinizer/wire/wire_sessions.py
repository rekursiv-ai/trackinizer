"""Wire contract for agent-session lifecycle, messaging, and the feed.

Sessions captured by the agent runner flow to trackinizer through ``start``
(mint the ``AgentSession`` row) and ``end`` (mark it closed); the records
themselves ride the separate :mod:`wire.wire_session_ir` contract. This
module is the single source for these request/response shapes and their
path templates; the server registers handlers against them and the client
builds requests from them, so neither can drift.

It also carries the cross-session console feed (:class:`FeedEvent`,
:class:`FeedCursor`), its counts (:class:`FeedFacetsResponse`,
:class:`FeedHistogramResponse`) and the messaging bodies --
everything about a session that is not one of its records.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Self, override

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trackinizer.wire.json_types import JSON, UtcDatetime


_MAX_MESSAGE_CHARS: Final = 16_384
"""Upper bound on a message body: what one send puts in front of an agent."""

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
    "CANVAS_LABEL_PREFIX",
    "CONSOLE_RELAY_MARK",
    "CONTEXT_TRAILER",
    "FEED_FACETS_PATH",
    "FEED_HISTOGRAM_PATH",
    "FEED_PATH",
    "GIT_LABEL_PREFIX",
    "SEND_MESSAGE_PATH",
    "SESSIONS_PATH",
    "SESSION_API_PATHS",
    "SESSION_AUDIENCE_PATH",
    "SESSION_END_PATH",
    "SESSION_PARTS_PATH",
    "SESSION_PLACE_PATH",
    "SESSION_RECORDS_PATH",
    "SESSION_START_PATH",
    "SESSION_WORKSPACE_PATH",
    "VERSION_PATH",
    "CanvasMark",
    "CanvasRecordContext",
    "CanvasVisibleVisual",
    "ConsoleRelay",
    "ConsoleRelayReceipt",
    "ConsoleTarget",
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
    "MessageKind",
    "MoveSession",
    "PlaceSession",
    "SendMessage",
    "SendMessageResponse",
    "SessionAgent",
    "SessionAudienceChange",
    "SessionAudienceResponse",
    "SessionEnd",
    "SessionEndResponse",
    "SessionPlacement",
    "SessionStart",
    "SessionStartResponse",
    "campaign_asker_label",
    "session_audience_path",
    "session_place_path",
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
    """Initial room membership (the runner's ``--room``); namespaces the session
    can be addressed within. See :attr:`AgentSession.rooms`."""

    idempotency_key: uuid.UUID | None = None
    """Optional client-supplied key; a repeat ``start`` with the same key
    returns the original session id without minting a duplicate."""

    private: bool = False
    """Open the session hidden: ``private``, read only by its owner and the people who
    oversee. A key pinned to another audience than ``open`` or ``private`` is refused
    (403 ``audience_not_held``). A server that predates the field ignores it, which
    :attr:`SessionStartResponse.audience` then shows."""

    keep_audience: bool = False
    """Leave a session this start re-opens in the audience it has, so ``private`` hides
    only a session it mints: what an upload of a finished session asks. A server that
    predates the field hides the ``open`` session a ``private`` start re-opens."""

    _reject_blank_scalars = field_validator(
        "cli",
        "cli_session_id",
        "actor",
        "account",
        mode="after",
    )(staticmethod(_reject_blank))

    # A room name must be a single clean token: non-blank (matched verbatim against
    # ``agentsession_rooms``) and comma-free (the agent runner exports rooms comma-joined
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

    account: str | None = None
    """The account that holds the session, whose ``run`` and ``answer`` messages its run
    acts on; ``None`` from a server that predates the field."""

    audience: str | None = None
    """Who reads the session's transcript, as the server holds it; ``None`` from a
    server that predates the field."""


CANVAS_LABEL_PREFIX: Final = "canvas:"
"""A placed session's label naming its canvas: ``canvas:<canvas uuid>``."""

GIT_LABEL_PREFIX: Final = "git:"
"""A placed session's label naming its checkout: ``git:<origin url>@<branch>``."""


class GitRef(BaseModel):
    """Where a run's checkout stands: its origin URL and branch."""

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=500)
    """The ``origin`` remote URL. The server strips any credentials from it."""

    branch: str = Field(min_length=1, max_length=255)


class PlaceSession(BaseModel):
    """Ask the server to place a session in a workspace and a canvas."""

    model_config = ConfigDict(extra="forbid")

    canvas: uuid.UUID | None = None
    """The canvas the run was started from (``TRAX_CANVAS``), if any."""

    git: GitRef | None = None
    """The run's checkout, when it is on a branch of a git work tree."""


class SessionPlacement(BaseModel):
    """Where a session was placed; both ids are ``None`` for a session not placed."""

    workspace_id: uuid.UUID | None = None
    canvas_id: uuid.UUID | None = None


class MoveSession(BaseModel):
    """The workspace a session is moved to."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID


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
    """The kind the record's row is stored under: its class name, or for a
    tool-call record its tool's legacy class name, such as ``BashResult``.
    Widened from the closed 8-member legacy Literal: the IR has 21 concrete
    kinds and a console that rejected an unknown one would break on every new
    record type."""

    created: datetime
    """Server write clock -- the feed's cross-session order key."""

    timestamp: datetime | None = None
    model: str | None = None
    message: JSON = Field(default_factory=dict)
    """The record's payload in the current record shape, under the legacy field
    name so the console's renderer needs no rewrite for the shape it already
    reads."""

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


class CanvasRecordContext(BaseModel):
    """A graph record the sender sees on screen, with a bounded title preview."""

    id: uuid.UUID
    kind: str
    seq: int
    title: str = Field(max_length=512)


class CanvasVisibleVisual(BaseModel):
    """A visual present on the canvas when a message was sent."""

    id: uuid.UUID
    type: str
    record: CanvasRecordContext | None = None
    """The record the visual shows, when it shows one that exists."""

    params: dict[str, str | int | bool] = Field(default_factory=dict)
    """The visual's parameters as the canvas holds them, such as the narrowing a
    reader set in the pane, so an agent that shows it again can keep them."""


class CanvasPage(BaseModel):
    """A page the sender was on, with the record it shows when it names one."""

    route: str
    """The `#/...` address."""

    record: CanvasRecordContext | None = None


class CanvasMark(BaseModel):
    """A red box drawn on the canvas since the sender's last line, with what it covers."""

    id: uuid.UUID
    """What ``trax canvas ID mark remove`` takes."""

    author: str
    page: str | None = None
    """The `#/...` page it was drawn on."""

    left: float = Field(default=0.0, ge=0, le=1)
    top: float = Field(default=0.0, ge=0, le=1)
    right: float = Field(default=0.0, ge=0, le=1)
    bottom: float = Field(default=0.0, ge=0, le=1)
    """Where it stands, as fractions of the width and height of the ``tile``'s body,
    of the window when ``window``, or else of the stage."""

    window: bool = False
    """Drawn over the whole browser window, Chat and the app's bars included, rather
    than the stage. A picture of the canvas shows the whole window."""

    tile: CanvasVisibleVisual | None = None
    """The visual it was drawn on, when it was drawn on one."""

    instances: list[CanvasVisibleVisual] = Field(default_factory=list)
    """The visuals under it that are still on the canvas."""

    records: list[CanvasRecordContext] = Field(default_factory=list)
    """The records under it that exist."""


class ConsoleTarget(BaseModel):
    """An agent a Console line was sent to, as ``@actor`` or ``@actor:room``."""

    actor: str = Field(min_length=1)
    room: str | None = None

    _validate_scalars = field_validator("actor", "room", mode="after")(
        staticmethod(_reject_blank),
    )


CONSOLE_RELAY_MARK: Final = "→ "
"""What a Console relay line starts with in a canvas's chat: an arrow, then a space.
The web draws a line that starts with it as a relay, neither the person's nor the
agent's."""

CONTEXT_TRAILER: Final = "\nTrackinizer context (verify with trax): "
"""Where a person's line ends, as an agent reads it, and what it was told of where the
line came from begins, as JSON. Whatever shows or summarizes the line cuts it here."""


class ConsoleRelay(BaseModel):
    """What a person sent agents directly from the Console, told to their canvas's chat.

    Posted by the browser after the per-agent sends. No chat takes it: a chat's agent
    answers every input it is sent, and no input is a silent one.
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
    """Whether a Console relay reached the canvas's chat."""

    delivered: bool
    """Always false: no chat takes a relay. The agents already have the message, so
    this is not an error."""


type MessageKind = Literal["ask", "reply", "tell"]
"""How an agent's team booked a message to another agent: a request, owed a reply; a
reply, which settles one; or news, owed nothing. sagent's own ``MessageKind``, which
this package may not import."""


class SessionAgent(BaseModel):
    """One agent of one session, as AgentSend and ``trax send`` name it: ``label#412``.

    The session is its number or its UUID. A label may hold ``#`` itself: the session
    is what follows the last one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    """The agent's label in its session, such as ``Agent`` or ``lead``."""

    session: Annotated[int, Field(ge=1)] | uuid.UUID
    """The session's number, or its UUID."""

    _validate_label = field_validator("label", mode="after")(
        staticmethod(_reject_blank),
    )

    @classmethod
    def parse(cls, named: str) -> Self:
        """Read ``label#session``, the session a number or a UUID.

        Args:
          named: Such as ``lead#412``.

        Returns:
          agent: The agent it names.

        Raises:
          ValueError: It names no label, or no session after its last ``#``.

        """
        label, _, session = named.rpartition("#")
        if session.isascii() and session.isdigit():
            return cls(label=label, session=int(session))
        try:
            return cls(label=label, session=uuid.UUID(session))
        except ValueError as error:
            raise ValueError(
                f"{named!r} names no agent of a session: write label#number, such as "
                "lead#412, or label#UUID",
            ) from error

    @override
    def __str__(self) -> str:
        return f"{self.label}#{self.session}"


class SendMessage(BaseModel):
    """Send a message to one agent of a session, or to the live sessions of a name.

    A body names one target: ``to``, an agent of one session, or ``actor`` (with an
    optional ``room``), a routing name, which may reach several. The server sends the
    text to each such live session through the agent process that serves it. The
    sender is attested by the route, never the body, so a forged ``source`` / sender
    field is rejected (``extra="forbid"``) rather than silently ignored. An agent
    process names the session it sends from, its agent's label and how it booked the
    message; the server checks the session against the calling key and writes the
    sender as ``label#number`` itself.
    """

    model_config = ConfigDict(extra="forbid")

    to: SessionAgent | None = None
    """The agent of one session it is for."""

    actor: str | None = Field(default=None, min_length=1)
    """Target routing name (an ``AgentSession`` owner)."""

    room: str | None = None
    """Optional room scope; when set, only sessions in that room match."""

    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)
    """The message body to inject into each matched session."""

    asker: str | None = Field(default=None, max_length=_MAX_ACCOUNT_CHARS)
    """The account of the user the configured assistant sends this message for.

    The server lets the assistant relay input only for a writer whose account holds
    the session, even if the assistant itself opened or reads it. Any other sender's
    ``asker`` is ignored.
    """

    from_session: uuid.UUID | None = None
    """The session whose agent sends it: only a key that session's agent process holds
    its socket with may name it."""

    from_label: str | None = Field(default=None, min_length=1)
    """The sending agent's label in ``from_session``."""

    booked: MessageKind | None = None
    """How the sending agent's team booked it, so the books are kept across sessions."""

    _validate_scalars = field_validator(
        "actor",
        "room",
        "asker",
        "from_label",
        mode="after",
    )(staticmethod(_reject_blank))

    @model_validator(mode="after")
    def _one_target_and_a_whole_sender(self) -> Self:
        """Refuse a body naming both targets or neither, or part of an agent sender."""
        if (self.to is None) == (self.actor is None):
            raise ValueError("name one target: to, or actor")
        if self.room is not None and self.actor is None:
            raise ValueError("room scopes an actor")
        named = (self.from_session, self.from_label, self.booked)
        if any(part is None for part in named) and any(p is not None for p in named):
            raise ValueError("from_session, from_label and booked go together")
        return self


class SendMessageResponse(BaseModel):
    """Receipt for a ``send``: which live sessions the message went to.

    ``delivered`` lists the session ids sent to: handed to the agent process serving
    it, or held for one still starting. Empty means no agent process serves a live
    session the target names now -- the honest "undelivered" signal.
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

    _reject_blank_scalars = field_validator(
        "cli_session_id",
        "actor",
        mode="after",
    )(staticmethod(_reject_blank))


class SessionEndResponse(BaseModel):
    """Confirmation that a session was closed."""

    id: uuid.UUID
    ended: datetime | None = None


# API route paths -- wire contract shared with the client, not tunables.
SESSIONS_PATH: Final = "/api/sessions"
SESSION_START_PATH: Final = "/api/sessions/start"
SESSION_RECORDS_PATH: Final = "/api/sessions/{session_id}/records"
SESSION_PARTS_PATH: Final = "/api/sessions/{session_id}/parts"
SESSION_END_PATH: Final = "/api/sessions/{session_id}/end"
SESSION_AUDIENCE_PATH: Final = "/api/sessions/{session_id}/audience"
SESSION_PLACE_PATH: Final = "/api/sessions/{session_id}/place"
SESSION_WORKSPACE_PATH: Final = "/api/sessions/{session_id}/workspace"
SEND_MESSAGE_PATH: Final = "/api/messages"
VERSION_PATH: Final = "/api/version"
FEED_PATH: Final = "/api/web/feed"
FEED_FACETS_PATH: Final = "/api/web/feed/facets"
FEED_HISTOGRAM_PATH: Final = "/api/web/feed/histogram"

# Every non-field API path the client, SPA, or deploy probe depends on, as a
# FastAPI route template. These routes are hand-registered (unlike the derived
# inquiry-field table), so this registry is the single source a drift test
# checks against the live app -- the analogue of ``inquiry_field_routes`` for
# the session/messaging/feed/version family. A new route in this family belongs
# here so the same drift guard covers it.
SESSION_API_PATHS: tuple[str, ...] = (
    SESSIONS_PATH,
    SESSION_START_PATH,
    SESSION_RECORDS_PATH,
    SESSION_PARTS_PATH,
    SESSION_END_PATH,
    SESSION_AUDIENCE_PATH,
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
    """Return the path that places one session in a workspace and canvas."""
    return SESSION_PLACE_PATH.format(session_id=session_id)


def session_audience_path(session_id: uuid.UUID) -> str:
    """Return the path that hides one session or shows it again."""
    return SESSION_AUDIENCE_PATH.format(session_id=session_id)


def held_read_timeout(wait_sec: float) -> float | None:
    """Return the read timeout a request the server holds ``wait_sec`` needs.

    Args:
      wait_sec: How long the server may hold the request open.

    Returns:
      timeout: Seconds the client may wait to read the response, or ``None``
        to keep the transport's own timeout when the request does not wait.

    """
    # The server answers empty AT ``wait_sec``, so a read deadline equal to it races
    # that response and turns a normal empty result into a transport error.
    return wait_sec + 10.0 if wait_sec else None
