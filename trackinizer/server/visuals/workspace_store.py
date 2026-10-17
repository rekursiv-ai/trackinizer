"""Atomic, per-user persistence for the visual canvas."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from itertools import count
from typing import TYPE_CHECKING, Final, cast

import hashlib
import json
import re
import uuid

from treekle import from_plain

from trackinizer.server.audience import reads_session
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.notify import tx
from trackinizer.server.preferences import (
    ASSISTANT_KEYS,
    Preferences,
    stored_preferences,
    write_preferences,
)
from trackinizer.server.store.deleted_chats import (
    refuse_a_deleted_chat,
    session_was_purged,
)
from trackinizer.server.values import vetted_sql
from trackinizer.server.visuals.chat_forks import (
    ChatClosedError,
    ConversationTakenError,
    ForeignChatError,
)
from trackinizer.server.visuals.partners import (
    assistant_holds_chat,
    assistant_key_may_use,
    attach_partner,
    live_chat_session,
    resolve_partner,
    station_session_key_may_use,
)
from trackinizer.server.visuals.science_chats import (
    conversation_of_session,
    helper_began_chat,
    may_post,
    read_line_starter,
    science_chat_exists_at,
)
from trackinizer.server.visuals.screens import read_screen
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ChatWindow,
    ChoosePartner,
    Highlight,
    NameChat,
    Navigate,
    SetPreferences,
    ShowVisual,
    StationData,
    StationLayout,
    WorkspaceData,
    WorkspaceSettingsRefs,
    WorkspaceState,
    WorkspaceSummary,
    WorkstreamSuggestion,
    apply_operation,
    initial_data,
    refuse_record,
    restore_page,
)
from trackinizer.server.visuals.workstreams import check_workstream, open_scratch
from trackinizer.types.errors import ChatDeletedError, ConflictError
from trackinizer.wire.audience import OPEN
from trackinizer.wire.bodies import SubmitArtifact
from trackinizer.wire.wire_science_chat import ChatSend, ChatSent
from trackinizer.wire.wire_sessions import WorkspaceMessageContext


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.auth import Role
    from trackinizer.server.config import Assistant
    from trackinizer.server.store.core import Store
    from trackinizer.server.visuals.catalog import VisualCatalogBody
    from trackinizer.server.visuals.workspaces import PartnerChoice


UNNAMED_PREFIX: Final = "unnamed-workstream#"
"""The name a station has until its owner renames it, followed by a number."""

_UNNAMED: Final = re.compile(rf"{re.escape(UNNAMED_PREFIX)}([0-9]+)")

_STATION: Final = "id, revision, state, station"
"""The `visual_workspaces` columns :func:`state_from_row` reads."""

_SELECT_STATION: Final = vetted_sql("SELECT ", _STATION, " FROM visual_workspaces ")
_RETURNING_STATION: Final = vetted_sql(" RETURNING ", _STATION)

_NAME_ATTEMPTS: Final = 8
"""Tries at a free name before a station gives up: another create took it each time."""

PERSONAL_SETTINGS_LABEL: Final = "personal-settings"


class RevisionConflictError(Exception):
    """The caller based its operation on an older workspace revision."""

    def __init__(self, current: WorkspaceState) -> None:
        super().__init__("stale workspace revision")
        self.current = current


class ReplayConflictError(Exception):
    """An idempotency key was reused with a different operation."""

    def __init__(self, current: WorkspaceState) -> None:
        super().__init__("Idempotency-Key already used for another operation")
        self.current = current


class WorkspaceDisabledError(Exception):
    """The account cannot access a visual canvas."""


class WorkspaceKeyRefusedError(Exception):
    """The API key is not the canvas's Chat partner, which alone may use it."""


class WorkspaceSessionUnavailableError(Exception):
    """The canvas's partner has no session that can take this browser message."""


class PartnerBusyError(Exception):
    """The partner's inbound queue is full, so a message would evict an unread one."""


class StationChatError(Exception):
    """A line would give a station a second chat, or one that another station holds."""


class StationNameTakenError(Exception):
    """The station's owner already has a station of that name."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliedOperation:
    """The canvas after an operation, and whether this call only replayed it."""

    state: WorkspaceState
    replayed: bool
    """The key had already been applied: nothing changed, so nothing is published."""

    stations: tuple[uuid.UUID, ...] = ()
    """For a ``preferences`` change, every station of the owner, each to be told."""

    preferences: Preferences | None = None
    """For a ``preferences`` change, all of the owner's preferences after it, for
    their own tabs; the state carries only those its agent may see."""

    restored_page: bool = False
    """A ``navigate`` found the page closed and showed it again, so the canvas
    changed and its new state is to be published."""


async def send_chat_line(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    body: ChatSend,
    key: uuid.UUID,
    source: str,
    source_role: Role,
    inbound: InboundQueue,
    assistant: Assistant | None,
    reader: SessionReader,
    members: SessionReader | None,
) -> ChatSent | None:
    """Queue a browser line for a science chat's assistant, with its page and trail.

    The line's conversation is the one it names, else the one its station already
    holds, else a new one named by ``key``, so a retry under the same key queues
    nothing again and answers with the same conversation. A station without a chat
    takes the line's conversation as its own, in the transaction that queues the
    line; a station holds one chat only. A fork starts a new station instead: a copy
    of the station it was sent from, bound to the fork's conversation.

    Nothing of the line is stored here: the assistant records it in the
    conversation's session. A conversation whose session the assistant has open takes
    the line on that session's own queue; any other, and a new one, goes to the
    assistant's service session, which opens or resumes the conversation's session
    before it answers.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      body: The line, its station, and optional conversation, fork, page and trail.
      key: Required idempotency key.
      source: Attested browser principal email.
      source_role: That principal's role.
      inbound: Session message queue.
      assistant: The configured assistant, if any.
      reader: The poster, as the transcript test reads them: they post into a chat
        they read, and a fork point in a session they may not read is no line, as
        one past the end of a session is none.
      members: Everyone a chat the poster begins is open to besides them, as one
        reader (:func:`~trackinizer.server.audience.members_reader`); a fork
        is theirs to read, so they must read what it copies. ``None`` for no one.

    Returns:
      sent: The conversation, the station it went to, and its session when the
        assistant has it open; None for a foreign station.

    Raises:
      WorkspaceSessionUnavailableError: The station has no live partner, its local
        helper was asked into a conversation the shared assistant holds, or the
        assistant into one a person's own helper began.
      PartnerBusyError: The queue the line would join is full.
      StationChatError: The station's chat is another conversation, or another
        station of the poster holds the one named.
      IdempotencyReuseError: ``key`` already posted another line.
      ForeignChatError: The poster does not read the conversation.
      ChatClosedError: The fork point is a line of a ``private`` or ``staff`` chat,
        whose audience a fork's copies would not keep, or of a chat ``members`` do
        not read.
      ConversationTakenError: The fork's key names a conversation someone else began.
      ChatDeletedError: The line's conversation, or the fork's key, names a chat that
        was deleted, or the fork point is a line of one.
      ValueError: The fork names no line of a science chat.

    """
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                _SELECT_STATION,
                "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            ),
            body.workspace_id,
            user_id,
        )
        if row is None:
            return None
        state = state_from_row(cast("Mapping[str, object]", row))
        conversation_id = (
            key
            if body.fork is not None
            else (body.conversation_id or state.conversation_id or key)
        )
        await _check_fork_or_membership(
            conn,
            body=body,
            conversation_id=conversation_id,
            email=source,
            assistant=assistant,
            inbound=inbound,
            reader=reader,
            members=members,
        )
        partner = await resolve_partner(
            conn,
            inbound=inbound,
            assistant=assistant,
            choice=state.partner_choice,
            owner_id=user_id,
        )
        if partner is None or partner.session_id is None or partner.status != "live":
            raise WorkspaceSessionUnavailableError(
                "Your local helper is not running; start it with `trax helper claude`"
                if state.partner_choice == "local"
                else "The assistant is not running",
            )
        chat = await chat_line_target(
            conn,
            inbound=inbound,
            assistant=assistant,
            choice=state.partner_choice,
            conversation_id=conversation_id,
        )
        target = chat or partner.session_id
        if inbound.is_full(target):
            raise PartnerBusyError("Partner busy: too many unread messages; try again.")
        screen = await read_screen(
            conn,
            page=body.page,
            trail=body.trail,
            visuals=state.visuals,
        )
        # Last, so a refused line leaves the station as it was.
        station_id = (
            await _fork_station(
                conn,
                user_id=user_id,
                source=row,
                conversation_id=conversation_id,
            )
            if body.fork is not None
            else await _bind_chat(
                conn,
                user_id=user_id,
                state=state,
                conversation_id=conversation_id,
            )
        )
        _ = inbound.send_once(
            key,
            [
                (
                    target,
                    Inbound(
                        text=body.text,
                        source=source,
                        source_role=source_role,
                        context=WorkspaceMessageContext(
                            workspace_id=station_id,
                            visible_visuals=screen.visuals,
                            agent_instructions=state.agent_instructions,
                            continuation_record_id=state.continuation_record_id,
                            conversation_id=conversation_id,
                            fork=body.fork,
                            page=screen.page,
                            trail=screen.trail,
                        ),
                    ),
                ),
            ],
            fingerprint=hashlib.sha256(
                "\0".join(
                    (
                        str(conversation_id),
                        str(body.workspace_id),
                        body.text,
                        str(body.fork),
                    ),
                ).encode(),
            ).hexdigest(),
        )
    return ChatSent(
        conversation_id=conversation_id,
        session_id=chat,
        workspace_id=station_id,
    )


async def chat_line_target(
    conn: Conn,
    *,
    inbound: InboundQueue,
    assistant: Assistant | None,
    choice: PartnerChoice,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Say where a station's line for a chat goes, refusing one nobody would read.

    A Chat post and a Console relay both ask this. Only the assistant drains a
    chat's own session. A local helper opens its chats but drains its service session
    alone, so it is sent no line of a chat the shared assistant holds; and only that
    helper reads a chat a person's own helper began, so the assistant is sent none
    of those.

    Args:
      conn: A database connection.
      inbound: Session message queue, which knows who polls each session.
      assistant: The configured assistant, if any.
      choice: The station's partner: the shared assistant or the owner's helper.
      conversation_id: The chat.

    Returns:
      session: The chat's own session the assistant has open; None sends the line to
        the partner's service session.

    Raises:
      WorkspaceSessionUnavailableError: The line would go where no one reads it.

    """
    if choice == "local":
        if await assistant_holds_chat(
            conn,
            assistant=assistant,
            conversation_id=conversation_id,
        ):
            raise WorkspaceSessionUnavailableError(
                "The shared assistant holds this conversation; start a new one "
                "to use your local helper",
            )
        return None
    if assistant is None:
        return None
    if await helper_began_chat(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    ):
        raise WorkspaceSessionUnavailableError(
            "A local helper holds this conversation; only it can answer here",
        )
    return await live_chat_session(
        conn,
        inbound=inbound,
        opener=assistant.email,
        conversation_id=conversation_id,
    )


async def open_latest_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    workstream_id: uuid.UUID | None = None,
) -> WorkspaceState:
    """Return the station the user changed last, creating the first once.

    Hidden stations do not reopen from the station list. If none remain visible, a
    new station is made. Two first requests that race read the visible winner.

    Args:
      engine: Database connection source.
      user_id: Owner of the stations.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      workstream_id: Workstream for a newly created first station, if set.

    Returns:
      state: The existing or newly created station, with its partner.

    """
    async with engine.acquire() as conn:
        latest = vetted_sql(
            _SELECT_STATION,
            "WHERE user_id = $1 AND coalesce((station->>'hidden')::boolean, FALSE) "
            "= FALSE AND NOT station ? 'session_id' "
            "ORDER BY modified_at DESC, id DESC LIMIT 1",
        )
        row = await conn.fetchrow(latest, user_id)
        if row is None:
            names = await conn.fetch(
                "SELECT station->>'name' AS name FROM visual_workspaces "
                "WHERE user_id = $1 AND station->>'name' LIKE $2",
                user_id,
                f"{UNNAMED_PREFIX}%",
            )
            # Another first-open may have inserted a visible station during the
            # names query; reuse it instead of creating a second station.
            row = await conn.fetchrow(latest, user_id)
            if row is None:
                _ = await _insert_station(
                    conn,
                    user_id=user_id,
                    name=next_station_name(from_plain(r["name"], str) for r in names),
                    data=initial_data(catalog),
                    workstream_id=workstream_id,
                )
                row = await conn.fetchrow(latest, user_id)
        if row is None:
            raise RuntimeError("first workspace insert was not visible")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            owner_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def new_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    workstream_id: uuid.UUID | None = None,
) -> WorkspaceState:
    """Create a station named with the lowest free ``unnamed-workstream#N``.

    Args:
      engine: Database connection source.
      user_id: Owner of the new station.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      workstream_id: Workstream assigned to the new station, if set.

    Returns:
      state: The new station, with its partner.

    """
    async with engine.acquire() as conn:
        row = await _new_station(
            conn,
            user_id=user_id,
            data=initial_data(catalog),
            workstream_id=workstream_id,
        )
        if row is None:
            raise RuntimeError("no free station name")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            owner_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def new_session_station(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    name: str,
    catalog: VisualCatalogBody,
    workstream_id: uuid.UUID,
    session_id: uuid.UUID,
) -> uuid.UUID:
    """Make a station for a placed session, named ``name`` or the first free variant.

    Args:
      conn: A database connection, inside the caller's transaction.
      user_id: Owner of the new station.
      name: The wanted name, such as the session's actor and CLI.
      catalog: Trusted visual definitions and the initial selection.
      workstream_id: The workstream the station sits in.
      session_id: The session the station is made for.

    Returns:
      workspace_id: The new station.

    """
    for _ in range(_NAME_ATTEMPTS):
        names = await conn.fetch(
            "SELECT station->>'name' AS name FROM visual_workspaces WHERE user_id = $1",
            user_id,
        )
        row = await _insert_station(
            conn,
            user_id=user_id,
            name=_unique_station_name(
                name,
                names=(from_plain(each["name"], str) for each in names),
            ),
            data=initial_data(catalog),
            workstream_id=workstream_id,
            session_id=session_id,
        )
        if row is not None:
            return cast(uuid.UUID, row["id"])
    raise RuntimeError("no free station name")


async def clear_chat_station(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Make or return one cleared copy of an owned station.

    The source row lock serializes clears of the same station. A retry checks its
    owner-scoped key only after acquiring that lock, so it returns the first copy.

    Args:
      engine: Database connection source.
      user_id: Owner of the source station.
      workspace_id: Station whose canvas is copied.
      key: Retry-safe idempotency key.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The copied station, or None when the source is absent or foreign.

    """
    async with engine.acquire() as conn, tx(conn):
        source = await conn.fetchrow(
            vetted_sql(
                _SELECT_STATION,
                "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            ),
            workspace_id,
            user_id,
        )
        if source is None:
            return None
        existing = await conn.fetchrow(
            vetted_sql(
                _SELECT_STATION,
                "WHERE user_id = $1 AND station->>'clear_key' = $2 LIMIT 1",
            ),
            user_id,
            str(key),
        )
        if existing is not None:
            return await _with_partner(
                conn,
                row=existing,
                user_id=user_id,
                inbound=inbound,
                assistant=assistant,
            )
        source_state = state_from_row(cast("Mapping[str, object]", source))
        row = await _new_station(
            conn,
            user_id=user_id,
            data=WorkspaceData.model_validate(source["state"]),
            page=source_state.page,
            layout=source_state.layout,
            clear_key=key,
            workstream_id=source_state.workstream_id,
        )
        if row is None:
            raise RuntimeError("clear station insert was not visible")
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def workspace_for_chat(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    workstream_id: uuid.UUID | None = None,
) -> WorkspaceState:
    """Find the user's station that holds a conversation, or make one bound to it.

    Args:
      engine: Database connection source.
      user_id: Owner of the station.
      conversation_id: The science chat to continue.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      workstream_id: Workstream for a newly created chat station, if set.

    Returns:
      state: The station that holds the conversation, with its partner.

    Raises:
      ChatDeletedError: The conversation was deleted.

    """
    async with engine.acquire() as conn, tx(conn):
        await refuse_a_deleted_chat(conn, conversation_id=conversation_id)
        row = await _chat_station_row(
            conn,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if row is None:
            row = await _new_station(
                conn,
                user_id=user_id,
                data=initial_data(catalog),
                conversation_id=conversation_id,
                workstream_id=workstream_id,
            )
        if row is None:
            # A racing request bound the conversation to a station first.
            row = await _chat_station_row(
                conn,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        if row is None:
            raise RuntimeError("no free station name")
        if StationData.model_validate(row["station"]).hidden:
            row = await conn.fetchrow(
                vetted_sql(
                    "UPDATE visual_workspaces SET station = coalesce(station, '{}') || ",
                    "jsonb_build_object('hidden', FALSE), "
                    "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                    _RETURNING_STATION,
                ),
                row["id"],
                user_id,
            )
            if row is None:
                raise RuntimeError("chat station disappeared while it was unhidden")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            owner_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def list_workspaces(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
) -> list[WorkspaceSummary]:
    """List the user's stations, the one changed last first.

    Args:
      engine: Database connection source.
      user_id: Owner of the stations.

    Returns:
      stations: Every station of the user.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, station, created_at, modified_at FROM visual_workspaces "
            "WHERE user_id = $1 ORDER BY modified_at DESC, id DESC",
            user_id,
        )
    return [
        WorkspaceSummary.of(
            StationData.model_validate(row["station"]),
            workspace_id=cast("uuid.UUID", row["id"]),
            created_at=cast("datetime", row["created_at"]),
            modified_at=cast("datetime", row["modified_at"]),
        )
        for row in rows
    ]


async def rename_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Rename one of the user's stations.

    Args:
      engine: Database connection source.
      user_id: Owner of the station.
      workspace_id: The station.
      name: The new name, already trimmed and within 1 to 120 characters.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The renamed station with its partner, or None when the user has no
        such station.

    Raises:
      StationNameTakenError: The user has another station of that name.

    """
    async with engine.acquire() as conn:
        if await conn.fetchval(
            "SELECT 1 FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'name' = $2 AND id <> $3",
            user_id,
            name,
            workspace_id,
        ):
            raise StationNameTakenError("You already have a station with that name")
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_workspaces SET station = coalesce(station, '{}') || ",
                "jsonb_build_object('name', $3::text), modified_at = clock_timestamp() ",
                "WHERE id = $1 AND user_id = $2",
                _RETURNING_STATION,
            ),
            workspace_id,
            user_id,
            name,
        )
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def name_unnamed_station(
    engine: DatabaseEngine,
    *,
    email: str,
    conversation_id: uuid.UUID,
    name: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Name the asker's station for a chat, only while its placeholder remains.

    Existing names are returned unchanged. A conflicting requested name receives
    the first available numeric suffix, so the assistant never displaces a station.

    Args:
      engine: Database connection source.
      email: The station owner, as resolved for the assistant.
      conversation_id: The chat held by the station.
      name: Suggested station name.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The named or already-named station, or None when the asker has no
        station for this chat.

    """
    checked = NameChat(name=name)
    async with engine.acquire() as conn, tx(conn):
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1 FOR UPDATE",
            email.lower(),
        )
        if user is None:
            return None
        user_id = from_plain(user["id"], uuid.UUID)
        row = await conn.fetchrow(
            "SELECT vw.id, vw.revision, vw.state, vw.station "
            "FROM visual_workspaces vw WHERE vw.user_id = $1 "
            "AND vw.station->>'conversation_id' = $2 FOR UPDATE",
            user_id,
            str(conversation_id),
        )
        if row is None:
            return None
        station = StationData.model_validate(row["station"])
        if _UNNAMED.fullmatch(station.name) is None:
            return await _with_partner(
                conn,
                row=row,
                user_id=user_id,
                inbound=inbound,
                assistant=assistant,
            )
        rows = await conn.fetch(
            "SELECT station->>'name' AS name FROM visual_workspaces "
            "WHERE user_id = $1 AND id <> $2",
            user_id,
            from_plain(row["id"], uuid.UUID),
        )
        unique_name = _unique_station_name(
            checked.name,
            names=[from_plain(item["name"], str) for item in rows],
        )
        updated = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_workspaces SET station = coalesce(station, '{}') || ",
                "jsonb_build_object('name', $3::text), modified_at = clock_timestamp() ",
                "WHERE id = $1 AND user_id = $2 AND station->>'name' ~ $4",
                _RETURNING_STATION,
            ),
            from_plain(row["id"], uuid.UUID),
            user_id,
            unique_name,
            _UNNAMED.pattern,
        )
        return await _with_partner(
            conn,
            row=updated,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def suggest_workstream_for_chat(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    conversation_id: uuid.UUID,
    suggestion: WorkstreamSuggestion,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Store a workstream suggestion on the asker's station for a chat.

    This writes station JSON only; it never creates a workstream Issue.

    Args:
      engine: Database connection source.
      user_id: The asker whose station holds the chat.
      email: The asker's Issue owner.
      conversation_id: The chat held by the station.
      suggestion: Suggested title and optional owned workstream.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated station, or None when the station or target is unavailable.

    """
    if suggestion.workstream_id is not None and not await check_workstream(
        engine,
        email=email,
        workstream_id=suggestion.workstream_id,
    ):
        return None
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "SELECT ",
                _STATION,
                " FROM visual_workspaces WHERE user_id = $1 "
                "AND station->>'conversation_id' = $2 FOR UPDATE",
            ),
            user_id,
            str(conversation_id),
        )
        if row is None:
            return None
        updated = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_workspaces SET station = coalesce(station, '{}') || ",
                "jsonb_build_object('suggestion', $3::jsonb), ",
                "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                _RETURNING_STATION,
            ),
            from_plain(row["id"], uuid.UUID),
            user_id,
            suggestion.model_dump(mode="json"),
        )
        return await _with_partner(
            conn,
            row=updated,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def set_station_flags(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    hidden: bool | None = None,
    pinned: bool | None = None,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Set supplied station flags and return the updated state.

    Only supplied flags change. The station row lock and write share one transaction
    so a concurrent canvas operation cannot erase the flags.

    Args:
      engine: Database connection source.
      user_id: Owner of the station.
      workspace_id: The station to update.
      hidden: Whether to hide the station, or None to leave it unchanged.
      pinned: Whether to pin the station, or None to leave it unchanged.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated station with its partner, or None when not owned.

    """
    if hidden is None and pinned is None:
        raise ValueError("At least one station flag is required.")
    changes = {
        name: value
        for name, value in (("hidden", hidden), ("pinned", pinned))
        if value is not None
    }
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_workspaces SET station = coalesce(station, '{}') || ",
                "$3::jsonb, modified_at = clock_timestamp() ",
                "WHERE id = $1 AND user_id = $2",
                _RETURNING_STATION,
            ),
            workspace_id,
            user_id,
            changes,
        )
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def set_station_workstream(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    workspace_id: uuid.UUID,
    workstream_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Move an owned station after checking its destination workstream.

    Args:
      engine: Database connection source.
      user_id: Station owner.
      email: The owner's Issue identity.
      workspace_id: Station to move.
      workstream_id: Owned workstream Issue to attach.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated station, or None when either id is unavailable.

    """
    if not await check_workstream(engine, email=email, workstream_id=workstream_id):
        return None
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_workspaces SET station = ",
                "(coalesce(station, '{}'::jsonb) - 'suggestion') || ",
                "jsonb_build_object('workstream_id', $3::text), ",
                "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                _RETURNING_STATION,
            ),
            workspace_id,
            user_id,
            str(workstream_id),
        )
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def set_workspace_page(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    route: str,
) -> bool:
    """Remember the page a station is on. No revision changes and nothing is published.

    The station is then the one changed last, which the app opens.

    Args:
      engine: Database connection source.
      user_id: Owner of the station.
      workspace_id: The station.
      route: The `#/...` hash, already validated.

    Returns:
      found: Whether the user has such a station.

    """
    async with engine.acquire() as conn:
        return (
            await conn.fetchval(
                "UPDATE visual_workspaces SET station = coalesce(station, '{}') || "
                "jsonb_build_object('page', $3::text), modified_at = clock_timestamp() "
                "WHERE id = $1 AND user_id = $2 RETURNING 1",
                workspace_id,
                user_id,
                route,
            )
            is not None
        )


async def set_workspace_layout(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    layout: StationLayout,
) -> bool:
    """Remember how a station's columns and windows were left.

    Each key the layout names replaces the stored one and the rest stay. No
    revision changes and nothing is published: a layout write never races an
    operation.

    Args:
      engine: Database connection source.
      user_id: Owner of the station.
      workspace_id: The station.
      layout: The keys to store, already validated.

    Returns:
      found: Whether the user has such a station.

    """
    async with engine.acquire() as conn:
        return (
            await conn.fetchval(
                "UPDATE visual_workspaces SET station = jsonb_set("
                "coalesce(station, '{}'), '{layout}', "
                "coalesce(station->'layout', '{}') || $3::jsonb), "
                "modified_at = clock_timestamp() "
                "WHERE id = $1 AND user_id = $2 RETURNING 1",
                workspace_id,
                user_id,
                layout.model_dump(mode="json", exclude_none=True),
            )
            is not None
        )


async def open_personal_settings(store: Store, *, email: str) -> uuid.UUID | None:
    """Return the user's pinned settings Artifact, creating it when missing.

    Args:
      store: The server store.
      email: The active user's email.

    Returns:
      artifact_id: The pinned Artifact id, or None when the user is not active.

    """
    async with store.engine.acquire() as conn, tx(conn):
        user = await conn.fetchrow(
            "SELECT id, preferences FROM users "
            "WHERE email = $1 AND status = 'active' FOR UPDATE",
            email.lower(),
        )
        if user is None:
            return None
        pinned = stored_preferences(user["preferences"]).settings_artifact_id
        if pinned is not None:
            return uuid.UUID(pinned)
        artifact_id = await _make_settings_artifact(
            store,
            conn=conn,
            owner=email.lower(),
            title=f"Settings: {email.lower()}",
            description="Your personal campaign defaults, edited here.",
            app=None,
        )
        _ = await write_preferences(
            conn,
            user_id=from_plain(user["id"], uuid.UUID),
            change={"settings_artifact_id": str(artifact_id)},
        )
        return artifact_id


async def set_session_campaign(
    store: Store,
    *,
    session_id: uuid.UUID,
    email: str,
    campaign: dict[str, object] | None,
) -> bool:
    """Compare-and-set the campaign on a head session, preserving its other app.

    Args:
      store: The server store.
      session_id: The AgentSession to update.
      email: The person attributed to the change.
      campaign: The campaign value, or None to remove it.

    Returns:
      written: Whether the update landed before the retry limit.

    """
    for _ in range(4):
        async with store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT app FROM inquiries WHERE id = $1 AND kind = 'AgentSession'",
                session_id,
            )
        if row is None:
            return False
        current = from_plain(row["app"], dict[str, object], default=None)
        updated = {} if current is None else dict(current)
        settings = from_plain(updated.get("settings"), dict[str, object], default={})
        if campaign is None:
            settings.pop("campaign", None)
        else:
            settings["campaign"] = campaign
        if settings:
            updated["settings"] = settings
        else:
            updated.pop("settings", None)
        target = updated or None
        try:
            await store.transition_app(
                session_id,
                expected_from=current,
                to=target,
                actor=email.lower(),
                reason="Update chat campaign settings.",
            )
        except ConflictError:
            continue
        return True
    return False


async def settings_refs_for_chat(
    store: Store,
    *,
    user_id: uuid.UUID,
    email: str,
    conversation_id: uuid.UUID,
    session_id: uuid.UUID | None,
) -> WorkspaceSettingsRefs:
    """Resolve a chat's station workstream and campaign settings rows.

    Args:
      store: The server store.
      user_id: The asker's user id.
      email: The asker's email.
      conversation_id: The science chat.
      session_id: Its head AgentSession, if it has one.

    Returns:
      refs: The workstream, session, personal Artifact and station metadata.

    """
    async with store.engine.acquire() as conn:
        station = await conn.fetchrow(
            "SELECT id, station->>'name' AS name, station->>'workstream_id' "
            "AS workstream_id, station->'suggestion' AS suggestion, "
            "station->'campaign' AS campaign FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'conversation_id' = $2",
            user_id,
            str(conversation_id),
        )
    if station is None:
        workstream_id = await open_scratch(
            store.engine,
            store=store,
            user_id=user_id,
            email=email,
        )
        return WorkspaceSettingsRefs(
            session_id=session_id,
            workstream_issue_id=workstream_id,
            personal_artifact_id=await open_personal_settings(store, email=email),
        )
    legacy = from_plain(station["campaign"], dict[str, object], default=None)
    station_id = from_plain(station["id"], uuid.UUID)
    if legacy is not None and session_id is not None:
        moved = await _seed_legacy_campaign(
            store,
            session_id=session_id,
            email=email,
            campaign=legacy,
        )
        if moved:
            async with store.engine.acquire() as conn:
                await conn.execute(
                    "UPDATE visual_workspaces SET station = station - 'campaign' "
                    "WHERE id = $1 AND user_id = $2 AND station ? 'campaign'",
                    station_id,
                    user_id,
                )
    workstream_id = from_plain(station["workstream_id"], uuid.UUID, default=None)
    if workstream_id is None:
        workstream_id = await open_scratch(
            store.engine,
            store=store,
            user_id=user_id,
            email=email,
        )
    suggestion = from_plain(
        station["suggestion"],
        WorkstreamSuggestion,
        default=None,
    )
    return WorkspaceSettingsRefs(
        workstream_issue_id=workstream_id,
        session_id=session_id,
        personal_artifact_id=await open_personal_settings(store, email=email),
        name=from_plain(station["name"], str, default=None),
        suggestion=suggestion,
    )


def next_station_name(names: Iterable[str]) -> str:
    """Name a station with the lowest ``unnamed-workstream#N`` not in ``names``.

    Args:
      names: The names the user's stations have.

    Returns:
      name: ``unnamed-workstream#N`` for the smallest N of 1 or more that is free.

    """
    taken = {int(m[1]) for name in names if (m := _UNNAMED.fullmatch(name))}
    return f"{UNNAMED_PREFIX}{next(n for n in count(1) if n not in taken)}"


async def read_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
    agent_api_key_id: uuid.UUID | None = None,
) -> WorkspaceState | None:
    """Read one canvas for its owner, or for an agent key allowed to use it.

    Args:
      engine: Database connection source.
      user_id: Authenticated principal.
      workspace_id: Requested workspace.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      agent_api_key_id: The calling agent key, if any; see
        :func:`_workspace_for_principal` for what it may read.

    Returns:
      state: Workspace with its partner, or None when the principal may not see it.

    """
    async with engine.acquire() as conn:
        found = await _workspace_for_principal(
            conn,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
        )
        if found is None:
            return None
        state, owner_id = found
        return await attach_partner(
            conn,
            state=state,
            owner_id=owner_id,
            inbound=inbound,
            assistant=assistant,
        )


async def station_ids(conn: Conn, *, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Return every station of ``user_id``: where a change of theirs is published.

    Args:
      conn: The connection to read on.
      user_id: Whose stations.

    Returns:
      ids: Their stations' ids, in no order.

    """
    rows = await conn.fetch(
        "SELECT id FROM visual_workspaces WHERE user_id = $1",
        user_id,
    )
    return [cast(uuid.UUID, row["id"]) for row in rows]


async def apply_workspace_operation(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: uuid.UUID,
    body: ApplyWorkspaceOperation,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    agent_api_key_id: uuid.UUID | None = None,
) -> AppliedOperation | None:
    """Apply one compare-and-swap operation and persist its replay receipt.

    A ``navigate``, ``highlight``, ``chat`` or ``preferences`` operation moves no visual
    and no revision: it is only recorded, for replay, and an agent key alone may send
    it. The one exception is a ``navigate`` on a canvas that has closed the page, which
    shows the page again in the next revision. ``preferences`` changes the station
    owner's preferences. A show names
    a record the visual accepts: its catalog lists the kinds, and one that takes no
    record refuses any.

    Args:
      engine: Database connection source.
      user_id: Authenticated principal.
      workspace_id: Target workspace.
      key: Idempotency key for this operation.
      body: Expected revision and validated operation.
      catalog: Trusted descriptor set the operation is validated against.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      agent_api_key_id: Calling API key, or None for browser cookie auth; see
        :func:`_workspace_for_principal` for what it may operate.

    Returns:
      applied: The updated or replayed state with its partner, or None when the
        principal may not see the canvas.

    """
    canonical = json.dumps(
        body.model_dump(mode="json", exclude_unset=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    async with engine.acquire() as conn, tx(conn):
        found = await _workspace_for_principal(
            conn,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
            for_update=True,
        )
        if found is None:
            return None
        current, owner_id = found
        with_partner = partial(
            attach_partner,
            conn,
            owner_id=owner_id,
            inbound=inbound,
            assistant=assistant,
        )
        receipt = await conn.fetchrow(
            "SELECT request_hash, response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            workspace_id,
            key,
        )
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise ReplayConflictError(await with_partner(state=current))
            # The receipt keeps the choice it was applied under; the partner is
            # computed from the choice now.
            replay = (
                current
                if isinstance(
                    body.operation,
                    Navigate | Highlight | ChatWindow | SetPreferences,
                )
                else WorkspaceState.model_validate(
                    from_plain(receipt["response"], dict[str, object])
                    | {
                        "name": current.name,
                        "conversation_id": current.conversation_id,
                        "page": current.page,
                        "layout": current.layout,
                        "hidden": current.hidden,
                        "pinned": current.pinned,
                        "workstream_id": current.workstream_id,
                        "session_id": current.session_id,
                    },
                ).model_copy(update={"partner_choice": current.partner_choice})
            )
            return AppliedOperation(
                state=await with_partner(state=replay),
                replayed=True,
            )
        if isinstance(
            body.operation,
            Navigate | Highlight | ChatWindow | SetPreferences,
        ):
            # No visual changes, so only the key is kept: a retry publishes nothing.
            if agent_api_key_id is None:
                raise ValueError(
                    "Only an agent navigates, highlights, hides Chat or sets preferences here.",
                )
            stations: tuple[uuid.UUID, ...] = ()
            preferences: Preferences | None = None
            if isinstance(body.operation, SetPreferences):
                # The owner's, not the agent's: the station's agent works for them,
                # and on the UI's preferences alone.
                beyond = sorted(set(body.operation.change) - ASSISTANT_KEYS)
                if beyond:
                    raise ValueError(
                        f"A station's agent may not set: {', '.join(beyond)}",
                    )
                preferences = await write_preferences(
                    conn,
                    user_id=owner_id,
                    change=body.operation.change,
                )
                stations = tuple(await station_ids(conn, user_id=owner_id))
            shown = current
            if isinstance(body.operation, Navigate):
                # An agent's navigation never lands on a canvas with no page: the
                # page returns in the same revision that carries the route.
                current_data = _data_of(current)
                restored = restore_page(current_data, catalog=catalog)
                if restored is not current_data:
                    shown = await _write_data(conn, current=current, data=restored)
            await _remember(
                conn,
                workspace_id=workspace_id,
                key=key,
                request_hash=request_hash,
                response={},
            )
            await trim_operation_receipts(conn, workspace_id=workspace_id)
            return AppliedOperation(
                state=await with_partner(state=shown),
                replayed=False,
                stations=stations,
                preferences=preferences,
                restored_page=shown is not current,
            )
        if isinstance(body.operation, ChoosePartner) and agent_api_key_id is not None:
            raise ValueError("Only the canvas's owner chooses its Chat partner.")
        if body.revision != current.revision:
            raise RevisionConflictError(await with_partner(state=current))
        if isinstance(body.operation, ShowVisual):
            await _check_record(conn, operation=body.operation, catalog=catalog)
        updated = await _write_data(
            conn,
            current=current,
            data=apply_operation(_data_of(current), body.operation, catalog),
        )
        await _remember(
            conn,
            workspace_id=workspace_id,
            key=key,
            request_hash=request_hash,
            response=updated.model_dump(mode="json"),
        )
        if updated.revision % 16 == 0:
            # Old retries still carry a stale revision and receive 409 after
            # their receipt expires. Keep the recent 64 exact replay results.
            await trim_operation_receipts(conn, workspace_id=workspace_id)
        return AppliedOperation(state=await with_partner(state=updated), replayed=False)


async def trim_operation_receipts(conn: Conn, *, workspace_id: uuid.UUID) -> None:
    """Keep a canvas's 64 newest operation receipts.

    Args:
      conn: Connection inside the operation's transaction.
      workspace_id: The canvas.

    """
    await conn.execute(
        "DELETE FROM visual_workspace_operations WHERE workspace_id = $1 "
        "AND key IN (SELECT key FROM visual_workspace_operations "
        "WHERE workspace_id = $1 ORDER BY created_at DESC, key DESC "
        "OFFSET 64)",
        workspace_id,
    )


def state_from_row(row: Mapping[str, object]) -> WorkspaceState:
    """Validate stored JSON before returning it as API state.

    Args:
      row: A `visual_workspaces` row with the `_STATION` columns.

    Returns:
      state: The validated workspace state.

    """
    data = WorkspaceData.model_validate(row["state"])
    stored_station = dict(from_plain(row["station"], dict[str, object]))
    stored_station.pop("campaign", None)
    station = StationData.model_validate(stored_station)
    return WorkspaceState(
        id=cast(uuid.UUID, row["id"]),
        revision=from_plain(row.get("revision"), int),
        name=station.name,
        conversation_id=station.conversation_id,
        page=station.page,
        layout=station.layout,
        hidden=station.hidden,
        pinned=station.pinned,
        workstream_id=station.workstream_id,
        suggestion=station.suggestion,
        session_id=station.session_id,
        visuals=data.visuals,
        worked=data.worked,
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
        partner_choice=data.partner_choice,
    )


async def _insert_station(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    name: str,
    data: WorkspaceData,
    conversation_id: uuid.UUID | None = None,
    page: str | None = None,
    layout: StationLayout | None = None,
    clear_key: uuid.UUID | None = None,
    workstream_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> Record | None:
    """Insert a station, or return None when its name or chat is taken."""
    station = StationData(
        name=name,
        conversation_id=conversation_id,
        page=page,
        layout=layout,
        workstream_id=workstream_id,
        session_id=session_id,
        clear_key=clear_key,
    )
    return await conn.fetchrow(
        vetted_sql(
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
            _RETURNING_STATION,
        ),
        uuid.uuid4(),
        user_id,
        station.model_dump(mode="json", exclude_none=True),
        data.model_dump(mode="json"),
    )


# A racing create can take the name between the read and the insert, so the name is
# chosen again a few times; a racing bind of the same chat ends the tries at once, and
# the station is then None.
async def _new_station(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    data: WorkspaceData,
    conversation_id: uuid.UUID | None = None,
    page: str | None = None,
    layout: StationLayout | None = None,
    clear_key: uuid.UUID | None = None,
    workstream_id: uuid.UUID | None = None,
) -> Record | None:
    """Insert a station under the lowest free unnamed name, or None if its chat is taken."""
    for _ in range(_NAME_ATTEMPTS):
        names = await conn.fetch(
            "SELECT station->>'name' AS name FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'name' LIKE $2",
            user_id,
            f"{UNNAMED_PREFIX}%",
        )
        row = await _insert_station(
            conn,
            user_id=user_id,
            name=next_station_name(from_plain(r["name"], str) for r in names),
            data=data,
            conversation_id=conversation_id,
            page=page,
            layout=layout,
            clear_key=clear_key,
            workstream_id=workstream_id,
        )
        if row is not None:
            return row
        if conversation_id is not None and await _station_of_chat(
            conn,
            user_id=user_id,
            conversation_id=conversation_id,
        ):
            return None
    return None


async def _with_partner(
    conn: Conn,
    *,
    row: Record | None,
    user_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState | None:
    """Return a station row as state with its partner, or None for no row."""
    if row is None:
        return None
    return await attach_partner(
        conn,
        state=state_from_row(cast("Mapping[str, object]", row)),
        owner_id=user_id,
        inbound=inbound,
        assistant=assistant,
    )


async def _bind_chat(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    state: WorkspaceState,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Give a station without a chat the conversation of its first line."""
    if state.conversation_id == conversation_id:
        return state.id
    if state.conversation_id is not None:
        raise StationChatError("This station's chat is another conversation")
    if await _station_of_chat(conn, user_id=user_id, conversation_id=conversation_id):
        raise StationChatError("Another station of yours holds this chat")
    await conn.execute(
        "UPDATE visual_workspaces SET station = coalesce(station, '{}') || "
        "jsonb_build_object('conversation_id', $2::text), "
        "modified_at = clock_timestamp() WHERE id = $1",
        state.id,
        str(conversation_id),
    )
    return state.id


# The new station copies the source's state and page; a retry of the fork finds the
# station the first try made.
async def _fork_station(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    source: Record,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Return the station of a fork's conversation, making it from its source once."""
    made = await _station_of_chat(
        conn,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if made is None:
        source_state = state_from_row(cast("Mapping[str, object]", source))
        row = await _new_station(
            conn,
            user_id=user_id,
            data=WorkspaceData.model_validate(source["state"]),
            conversation_id=conversation_id,
            page=source_state.page,
            layout=source_state.layout,
            workstream_id=source_state.workstream_id,
        )
        made = (
            await _station_of_chat(
                conn,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if row is None
            else cast(uuid.UUID, row["id"])
        )
    if made is None:
        raise RuntimeError("fork station insert was not visible")
    return made


async def _station_of_chat(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the user's station that holds a conversation, if they have one."""
    return cast(
        uuid.UUID | None,
        await conn.fetchval(
            "SELECT id FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'conversation_id' = $2",
            user_id,
            str(conversation_id),
        ),
    )


async def _chat_station_row(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Record | None:
    """Return the row of the user's station that holds a conversation, if any."""
    return await conn.fetchrow(
        vetted_sql(
            _SELECT_STATION,
            "WHERE user_id = $1 AND station->>'conversation_id' = $2",
        ),
        user_id,
        str(conversation_id),
    )


async def _remember(
    conn: Conn,
    *,
    workspace_id: uuid.UUID,
    key: uuid.UUID,
    request_hash: str,
    response: Mapping[str, object],
) -> None:
    """Record an applied operation's result, so a retry replays it."""
    await conn.execute(
        "INSERT INTO visual_workspace_operations "
        "(workspace_id, key, request_hash, response) VALUES ($1, $2, $3, $4)",
        workspace_id,
        key,
        request_hash,
        response,
    )


async def _check_record(
    conn: Conn,
    *,
    operation: ShowVisual,
    catalog: VisualCatalogBody,
) -> None:
    """Refuse a show whose record its visual cannot draw, or has no content."""
    descriptor = next(
        (v for v in catalog.visuals if v.type == operation.visual_type),
        None,
    )
    if descriptor is None or operation.record_id is None:
        return
    kind = None
    if descriptor.record_kinds is not None:
        kind = from_plain(
            await conn.fetchval(
                "SELECT kind FROM inquiries WHERE id = $1",
                operation.record_id,
            ),
            str,
            default=None,
        )
    if reason := refuse_record(operation, descriptor=descriptor, kind=kind) or "":
        raise ValueError(reason)
    if operation.visual_type == "trax.artifact" and (
        await conn.fetchval(
            "SELECT 1 FROM visual_report_revisions WHERE artifact_id = $1",
            operation.record_id,
        )
        is None
    ):
        raise ValueError("Artifact content not found.")


# A browser sees only its own stations. An agent key may use a station only as its Chat
# partner: the key opened the partner's live session and the live science chat the
# station holds, in which the station's owner is the account or a poster. A station
# without a chat has no partner to be, so it refuses every agent key. With a local
# partner that is the owner's own key; with the shared assistant, the owner's own key
# is refused by name, so a terminal `trax workspace` learns why. Any other is told
# nothing.
async def _workspace_for_principal(
    conn: Conn,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_api_key_id: uuid.UUID | None,
    inbound: InboundQueue,
    assistant: Assistant | None,
    for_update: bool = False,
) -> tuple[WorkspaceState, uuid.UUID] | None:
    """Find a canvas the principal may read and operate, with its owner."""
    row = await conn.fetchrow(
        vetted_sql(
            "SELECT user_id, ",
            _STATION,
            " FROM visual_workspaces WHERE id = $1 AND ($3::boolean OR user_id = $2)",
            " FOR UPDATE" if for_update else "",
        ),
        workspace_id,
        user_id,
        agent_api_key_id is not None,
    )
    if row is None:
        return None
    owner_id = cast(uuid.UUID, row["user_id"])
    state = state_from_row(cast("Mapping[str, object]", row))
    if agent_api_key_id is None:
        return state, owner_id
    partner = await resolve_partner(
        conn,
        inbound=inbound,
        assistant=assistant,
        choice=state.partner_choice,
        owner_id=owner_id,
    )
    if await assistant_key_may_use(
        conn,
        owner_id=owner_id,
        partner=partner,
        conversation_id=state.conversation_id,
        api_key_id=agent_api_key_id,
    ) or await station_session_key_may_use(
        conn,
        owner_id=owner_id,
        workspace_id=workspace_id,
        api_key_id=agent_api_key_id,
    ):
        return state, owner_id
    if owner_id == user_id:
        raise WorkspaceKeyRefusedError(
            "Only the canvas's Chat partner may use it with an API key",
        )
    return None


# The starter asked about is the conversation the line will be queued for, which the
# idempotency key can name as well as the body can.
async def _check_fork_or_membership(
    conn: Conn,
    *,
    body: ChatSend,
    conversation_id: uuid.UUID,
    email: str,
    assistant: Assistant | None,
    inbound: InboundQueue,
    reader: SessionReader,
    members: SessionReader | None,
) -> None:
    """Refuse a deleted chat, a fork of no line or a closed chat, a post unread."""
    await refuse_a_deleted_chat(
        conn,
        conversation_id=conversation_id,
        forked_from=None
        if body.fork is None
        else await conversation_of_session(conn, session_id=body.fork.session_id),
    )
    if body.fork is not None and not await science_chat_exists_at(
        conn,
        assistant=assistant,
        email=email,
        fork=body.fork,
        reader=reader,
    ):
        if await session_was_purged(conn, session_id=body.fork.session_id):
            raise ChatDeletedError("The chat this line was in was deleted")
        raise ValueError("The fork point is not a line of a science chat")
    if body.fork is not None and await conn.fetchval(
        "SELECT agentsession_audience <> $2 FROM inquiries WHERE id = $1",
        body.fork.session_id,
        OPEN,
    ):
        raise ChatClosedError("chat_fork_closed")
    if (
        body.fork is not None
        and members is not None
        and not await reads_session(
            conn,
            reader=members,
            session_id=body.fork.session_id,
        )
    ):
        raise ChatClosedError("chat_fork_wider")
    if body.fork is None:
        if not await may_post(
            conn,
            poster=email,
            reader=reader,
            conversation_id=conversation_id,
            assistant=assistant,
            inbound=inbound,
        ):
            raise ForeignChatError(
                "This chat is not shared with you; fork it to continue",
            )
        return
    starter = await read_line_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
        inbound=inbound,
    )
    if starter is not None and starter.lower() != email.lower():
        raise ConversationTakenError(
            "This conversation already exists; fork under a new key",
        )


def _data_of(state: WorkspaceState) -> WorkspaceData:
    """Return the stored part of a canvas's state: every field ``WorkspaceData`` has."""
    return WorkspaceData.model_validate(state, from_attributes=True)


async def _write_data(
    conn: Conn,
    *,
    current: WorkspaceState,
    data: WorkspaceData,
) -> WorkspaceState:
    """Store ``data`` as the canvas's next revision and return the new state."""
    updated = WorkspaceState(
        id=current.id,
        revision=current.revision + 1,
        name=current.name,
        conversation_id=current.conversation_id,
        page=current.page,
        layout=current.layout,
        hidden=current.hidden,
        pinned=current.pinned,
        workstream_id=current.workstream_id,
        suggestion=current.suggestion,
        session_id=current.session_id,
        visuals=data.visuals,
        worked=data.worked,
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
        partner_choice=data.partner_choice,
    )
    await conn.execute(
        "UPDATE visual_workspaces SET revision = $2, state = $3, "
        "modified_at = clock_timestamp() WHERE id = $1",
        current.id,
        updated.revision,
        data.model_dump(mode="json"),
    )
    return updated


def _unique_station_name(name: str, *, names: Iterable[str]) -> str:
    """Append the first free numeric suffix when ``name`` is already used."""
    taken = set(names)
    if name not in taken:
        return name
    suffix = 2
    candidate = name
    while candidate in taken:
        suffix_text = f" ({suffix})"
        candidate = f"{name[: 120 - len(suffix_text)].rstrip()}{suffix_text}"
        suffix += 1
    return candidate


async def _seed_legacy_campaign(
    store: Store,
    *,
    session_id: uuid.UUID,
    email: str,
    campaign: dict[str, object],
) -> bool:
    """Move a legacy station campaign to the session if it has no value yet."""
    for _ in range(4):
        async with store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT app FROM inquiries WHERE id = $1 AND kind = 'AgentSession'",
                session_id,
            )
        if row is None:
            return False
        current = from_plain(row["app"], dict[str, object], default=None)
        updated = {} if current is None else dict(current)
        settings = from_plain(updated.get("settings"), dict[str, object], default={})
        if "campaign" in settings:
            return True
        settings["campaign"] = campaign
        updated["settings"] = settings
        try:
            await store.transition_app(
                session_id,
                expected_from=current,
                to=updated,
                actor=email,
                reason="Move legacy station campaign to its chat session.",
            )
        except ConflictError:
            continue
        return True
    return False


async def _make_settings_artifact(
    store: Store,
    *,
    conn: Conn,
    owner: str,
    title: str,
    description: str,
    app: dict[str, object] | None,
) -> uuid.UUID:
    """Create an owner-pinned settings Artifact inside ``conn``."""
    return await store.submit_artifact(
        SubmitArtifact(
            title=title,
            description=description,
            owner=owner,
            account=owner,
            labels=[PERSONAL_SETTINGS_LABEL],
            app=app,
        ),
        actor=owner,
        conn=conn,
    )
