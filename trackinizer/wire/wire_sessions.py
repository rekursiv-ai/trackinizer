"""Wire contract for agent-session lifecycle, messaging, and the feed.

Sessions captured by ``trax run`` flow to trackinizer through ``start``
(mint the ``AgentSession`` row) and ``end`` (mark it closed); the records
themselves ride the separate :mod:`wire.wire_session_ir` contract. This
module is the single source for these request/response shapes and their
path templates; the server registers handlers against them and the client
builds requests from them, so neither can drift.

It also carries the cross-session console feed (:class:`FeedEvent`,
:class:`FeedCursor`) and the inbound/routed messaging bodies -- everything
about a session that is not one of its records.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trackinizer.lib.custom_json import JSON


_MAX_MESSAGE_CHARS: Final = 16_384
"""Upper bound on an inbound/routed message body. Caps the bytes a single
``inject`` writes to the PTY under the pump lock (each write stalls human
keystrokes for its duration) and the memory a process-local queue holds."""


def _reject_blank(value: str | None) -> str | None:
    """Reject an empty-or-whitespace scalar string; pass ``None`` through.

    Every textual identity/scope field on these bodies (``cli``, ``actor``,
    ``cli_session_id``, ``room``) is matched or stored verbatim, so a
    whitespace-only value can never match and is almost certainly a client bug;
    reject it at the boundary rather than silently never-deliver or store junk.
    ``Field(min_length=1)`` alone admits ``"   "``, so each such field binds
    this validator. The one rule for every scalar string field, mirroring the
    list-element ``_reject_blank_strings`` rule in ``bodies.py``.
    """
    if value is not None and not value.strip():
        raise ValueError("value must be non-empty")
    return value


__all__ = [
    "FEED_PATH",
    "SEND_MESSAGE_PATH",
    "SESSION_API_PATHS",
    "SESSION_END_PATH",
    "SESSION_INBOUND_PATH",
    "SESSION_PARTS_PATH",
    "SESSION_RECORDS_PATH",
    "SESSION_START_PATH",
    "VERSION_PATH",
    "DrainInboundResponse",
    "FeedCursor",
    "FeedEvent",
    "FeedResponse",
    "InboundDrainItem",
    "InboundEnqueueRequest",
    "InboundEnqueueResponse",
    "SendMessage",
    "SendMessageResponse",
    "SessionEnd",
    "SessionEndResponse",
    "SessionStart",
    "SessionStartResponse",
]


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

    title: str | None = None
    started: datetime | None = None
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

    _reject_blank_scalars = field_validator(
        "cli", "cli_session_id", "actor", "account", mode="after"
    )(staticmethod(_reject_blank))

    @field_validator("rooms", mode="after")
    @classmethod
    def _validate_rooms(cls, value: list[str] | None) -> list[str] | None:
        """Reject blank or comma-bearing room names.

        A room name must be a single clean token: non-blank (matched verbatim
        against ``agentsession_rooms``) and comma-free (``trax run`` exports
        rooms comma-joined into ``TRAX_ROOMS``, so a room ``'a,b'`` is
        indistinguishable from two rooms ``'a'`` and ``'b'``). Mirrors the
        ``SubmitAgentSession.rooms`` rule so both create paths agree.
        """
        if value is not None:
            for room in value:
                if not room.strip():
                    raise ValueError("rooms must be non-empty")
                if "," in room:
                    raise ValueError(f"room name must not contain a comma: {room!r}")
        return value


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
    """A keyset cursor into the feed's ``(created, session_id, part, seq)``
    order.

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

    ``events`` is ordered by ``(created, session_id, seq)`` so the console can
    append in order and resume from ``next_after`` on its next poll. The cursor
    is composite (see :class:`FeedCursor`) so a same-``created`` tie split
    across a page boundary is not skipped. ``next_after`` is ``None`` only when
    the page is empty and no prior cursor was supplied (a poll that drained the
    tail echoes back the cursor it was given).
    """

    events: list[FeedEvent]
    next_after: FeedCursor | None = None


class InboundEnqueueRequest(BaseModel):
    """A message a client asks trackinizer to inject into a live session.

    The enqueue **request** carries only ``text`` -- the one field the route
    uses. ``source`` is attested by the route from the authenticated principal
    (a client cannot forge another sender), and ``room`` has no enqueue
    semantics (the direct-session enqueue route discards it; routed delivery is
    the separate ``@actor:room`` send-message path). ``extra="forbid"`` makes a
    client-sent ``source`` or ``room`` a 422 rather than a silently-ignored
    field -- the request and response shapes are deliberately distinct (the
    response :class:`InboundDrainItem` carries both ``source`` and ``room``), so
    neither can be confused for the other.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)


class InboundDrainItem(BaseModel):
    """One message drained for a session, with its attested sender.

    The drain **response** item: unlike the enqueue request, it carries
    ``source`` -- the producer's attested ``--as`` / principal -- so the
    poller can render the ``[room] sender:`` injection context. Inbound-only
    and transient: it is **not** an ``agent_session_events`` row; when the
    agent consumes it, the capture path logs it as a normal turn.
    """

    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)
    source: str | None = None
    room: str | None = None
    """The room a routed message was scoped to, for the ``[room] sender:``
    injection prefix; ``None`` for a direct (session-id) enqueue."""

    _validate_room = field_validator("room", mode="after")(staticmethod(_reject_blank))


class InboundEnqueueResponse(BaseModel):
    """Receipt for an enqueued inbound message.

    ``queued`` is the count pending for the session after this enqueue. It is
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

    _validate_scalars = field_validator("actor", "room", mode="after")(
        staticmethod(_reject_blank)
    )


class SendMessageResponse(BaseModel):
    """Receipt for a ``send``: which live sessions the message was queued to.

    ``delivered`` lists the session ids enqueued (drop-if-absent: a session
    whose ``trax run`` is gone may never drain it). Empty means no live
    session matched the target -- the honest "undelivered" signal.
    """

    delivered: list[uuid.UUID]


class SessionEnd(BaseModel):
    """Mark a session closed, optionally backfilling late-known fields."""

    ended: datetime | None = None
    cli_session_id: str | None = Field(default=None, min_length=1)
    """Set when the CLI only revealed its session id mid-run."""

    actor: str | None = None

    _reject_blank_scalars = field_validator("cli_session_id", "actor", mode="after")(
        staticmethod(_reject_blank)
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
SESSION_INBOUND_PATH: Final = "/api/sessions/{session_id}/inbound"
SEND_MESSAGE_PATH: Final = "/api/messages"
VERSION_PATH: Final = "/api/version"
FEED_PATH: Final = "/api/web/feed"


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
    SESSION_INBOUND_PATH,
    SEND_MESSAGE_PATH,
    VERSION_PATH,
    FEED_PATH,
)


def session_records_path(session_id: uuid.UUID) -> str:
    """The record append/read path for one session."""
    return SESSION_RECORDS_PATH.format(session_id=session_id)


def session_end_path(session_id: uuid.UUID) -> str:
    """The end path for one session."""
    return SESSION_END_PATH.format(session_id=session_id)


def session_inbound_path(session_id: uuid.UUID) -> str:
    """The inbound-message path for one session (POST enqueue, GET drain)."""
    return SESSION_INBOUND_PATH.format(session_id=session_id)
