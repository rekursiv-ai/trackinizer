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

from trackinizer.server.audience import NOBODY, reads_session
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
    assistant_key_may_use,
    attach_partner,
    cli_partner,
    live_chat_session,
    provider_availability,
    shared_partner,
    station_session_key_may_use,
)
from trackinizer.server.visuals.science_chats import (
    conversation_of_session,
    may_post,
    read_first_session,
    read_fork_prefixes,
    read_line_starter,
    read_starter,
    science_chat_exists_at,
)
from trackinizer.server.visuals.screens import (
    read_covered_records,
    read_screen,
    wire_marks,
)
from trackinizer.server.visuals.workspaces import (
    AGENT_AUTHOR,
    AddMark,
    ApplyWorkspaceOperation,
    ChatWindow,
    ChooseProvider,
    ClearMarks,
    Highlight,
    NameChat,
    Navigate,
    RemoveMark,
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
    without_marks,
)
from trackinizer.server.visuals.workstreams import check_workstream, open_scratch
from trackinizer.types.errors import ChatDeletedError, ConflictError
from trackinizer.wire.audience import OPEN
from trackinizer.wire.bodies import SubmitAgentSession, SubmitArtifact
from trackinizer.wire.chat_commands import Command, Line, Refused, classify
from trackinizer.wire.wire_science_chat import (
    CHAT_TITLE,
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    ChatProvider,
    ChatSend,
    ChatSent,
    chat_actor,
    chat_session_id,
    fork_point_label,
)
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
    from trackinizer.wire.wire_science_chat import ChatForkAt


UNNAMED_PREFIX: Final = "unnamed-workstream#"
"""The name a station has until its owner renames it, followed by a number."""

_UNNAMED: Final = re.compile(rf"{re.escape(UNNAMED_PREFIX)}([0-9]+)")

_STATION: Final = "id, revision, state, station"
"""The `visual_workspaces` columns :func:`state_from_row` reads."""

_SELECT_STATION: Final = vetted_sql("SELECT ", _STATION, " FROM visual_workspaces ")
_RETURNING_STATION: Final = vetted_sql(" RETURNING ", _STATION)

_MARKS_PER_LINE: Final = 20
"""Most marks one Chat line carries; the newest, when more were drawn."""

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


class ProviderOfflineError(WorkspaceSessionUnavailableError):
    """The station's provider is a CLI, and no run of it holds the chat."""


class ProviderUnavailableError(Exception):
    """The provider chosen cannot run for the station's owner; the message says why."""


class CommandRefusedError(ValueError):
    """A line is a slash command the chat's provider does not run from Chat."""


class AnswerNotOwnerError(Exception):
    """An answer to a run's prompt came from someone other than the run's owner."""


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
    store: Store,
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
    """Queue a browser post for a science chat's provider, a line with its page and trail.

    The line's conversation is the one it names, else the one its station already
    holds, else a new one named by ``key``, so a retry under the same key queues
    nothing again and answers with the same conversation. A station without a chat
    takes the line's conversation as its own, in the transaction that queues the
    line; a station holds one chat only. A fork starts a new station instead: a copy
    of the station it was sent from, provider and all, bound to the fork's
    conversation. Its session is opened in the same transaction, holding copies of
    the original's records up to and including the fork point and linked to the
    original, so whichever provider answers the fork finds the conversation it
    continues there. Its line is queued only once that transaction commits, so the
    session is there to resume when the line is heard.

    Nothing of the line is stored here: its provider records it in the
    conversation's session. That is the poster's station's provider, except in a chat
    someone else started, whose own provider (:func:`read_chat_provider`) drains it
    whatever the poster's station names. For ``assistant``, a conversation whose session the
    assistant has open takes the line on that session's own queue; any other, and a
    new one, goes to the assistant's service session, which opens or resumes the
    conversation's session before it answers. For ``claude`` or ``codex``, the post
    goes on the queue of the chat's session that the CLI's run drains, with its kind
    and payload; only a ``line`` carries the page, trail and marks. A fork's
    conversation is new, so no run holds it: its line waits on its session's queue
    for the run the person starts.

    A ``line`` that is a known slash command (by
    :func:`~trackinizer.wire.chat_commands.classify`, for the chat's provider)
    is queued as a ``command`` of its normalized text, with no
    page, trail or marks, so the run types it bare; one the provider does not run
    from Chat is refused before anything is queued or bound. An ``answer`` to a
    ``claude`` or ``codex`` run's prompt comes from the chat's starter alone, the
    run's owner: it runs a tool, or grants trust, on their machine.

    Args:
      store: The store, which opens and fills a fork's session.
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
      sent: The conversation, the station it went to, and its session when the line
        was queued on it; None for a foreign station.

    Raises:
      WorkspaceSessionUnavailableError: The station has no live partner.
      ProviderOfflineError: The station's provider is a CLI and no run of it holds
        the chat, or the forker has no key to open a fork's session for it with.
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
      CommandRefusedError: The line is a slash command its provider does not run
        from Chat; the message says why. It is a ``ValueError``.
      AnswerNotOwnerError: An ``answer`` to a ``claude`` or ``codex`` chat came from
        someone other than its starter.
      ValueError: The fork names no line of a science chat, or an ``assistant``
        station is sent a post that is not a line.

    """
    async with store.engine.acquire() as conn, tx(conn):
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
        provider = await chat_answerer(
            conn,
            state=state,
            conversation_id=conversation_id,
            poster=source,
            assistant=assistant,
        )
        if body.kind == "line":
            match classify(body.text, provider):
                case Command(text=text):
                    body = body.model_copy(update={"kind": "command", "text": text})
                case Refused(reason=reason):
                    raise CommandRefusedError(reason)
                case Line():
                    pass
        queue, chat = await _post_target(
            conn,
            body=body,
            provider=provider,
            conversation_id=conversation_id,
            poster=source,
            inbound=inbound,
            assistant=assistant,
        )
        if queue is not None and inbound.is_full(queue):
            raise PartnerBusyError("Partner busy: too many unread messages; try again.")
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
        if body.fork is not None:
            forked = await _seed_fork(
                conn,
                store=store,
                user_id=user_id,
                forker=source,
                conversation_id=conversation_id,
                fork=body.fork,
                provider=provider,
                service=queue,
                assistant=assistant,
            )
            if queue is None:
                queue = chat = forked
        if queue is None:
            raise ProviderOfflineError(
                f"The fork has no session for a {provider} run to attach to",
            )
        context = (
            await _line_context(
                conn,
                body=body,
                state=state,
                station_id=station_id,
                conversation_id=conversation_id,
                reader=reader,
            )
            if body.kind == "line"
            else None
        )
        line = (
            queue,
            Inbound(
                text=body.text,
                kind=body.kind,
                payload=body.payload,
                source=source,
                source_role=source_role,
                context=context,
            ),
        )
        fingerprint = hashlib.sha256(
            "\0".join(
                (
                    str(conversation_id),
                    str(body.workspace_id),
                    body.text,
                    str(body.fork),
                    body.kind,
                    json.dumps(body.payload, sort_keys=True),
                ),
            ).encode(),
        ).hexdigest()
        # A fork's line is queued once its session is committed (below); sending none
        # here still refuses a reused key before the fork is written, and records nothing.
        _ = inbound.send_once(
            key,
            [line] if body.fork is None else [],
            fingerprint=fingerprint,
        )
        if context is not None and state.marks_seq > state.marks_sent:
            # Not a canvas change: no revision, as a page or layout write has none.
            await conn.execute(
                "UPDATE visual_workspaces SET state = jsonb_set(state, "
                "'{marks_sent}', to_jsonb($2::int)) WHERE id = $1",
                body.workspace_id,
                state.marks_seq,
            )
    if body.fork is not None:
        # Whoever hears the line resumes the fork's session at once: before the commit
        # it would find none and open a second one, holding none of the copies.
        _ = inbound.send_once(key, [line], fingerprint=fingerprint)
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
    provider: ChatProvider,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Say which session a station's line for a chat goes to.

    A Chat post and a Console relay both ask this. For ``assistant`` only the
    assistant drains a chat's own session, so a line goes there when the assistant
    has it open. For a CLI it goes to the chat's session its run drains.

    Args:
      conn: A database connection.
      inbound: Session message queue, which knows who polls each session.
      assistant: The configured assistant, if any.
      provider: The station's provider.
      conversation_id: The chat.

    Returns:
      session: The chat's own session its provider has open; None, for
        ``assistant`` alone, sends the line to the partner's service session.

    Raises:
      ProviderOfflineError: The provider is a CLI and no run of it holds the chat.

    """
    if provider != "assistant":
        partner = await cli_partner(
            conn,
            inbound=inbound,
            assistant=assistant,
            provider=provider,
            conversation_id=conversation_id,
        )
        if partner.status != "live" or partner.session_id is None:
            raise ProviderOfflineError(
                f"No {provider} run holds this chat; pick a machine to start one",
            )
        return partner.session_id
    if assistant is None:
        return None
    return await live_chat_session(
        conn,
        inbound=inbound,
        opener=assistant.email,
        conversation_id=conversation_id,
    )


async def open_chat_session(
    store: Store,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    assistant: Assistant | None,
) -> uuid.UUID | None:
    """Give a CLI station's chat a session a run can attach to, before any line.

    A station without a chat is bound to a new conversation first. A chat that has a
    session keeps it: the run attaches to that one. An ``assistant`` station is left
    as it is, since the assistant opens its chat's session when it hears the chat.

    Args:
      store: The store.
      user_id: The station's owner.
      workspace_id: The station.
      assistant: The configured assistant, whose session of the chat is one too.

    Returns:
      session_id: The chat's session; None for a station that is not the owner's,
        an ``assistant`` one, or an owner with no API key to open it with.

    """
    async with store.engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(_SELECT_STATION, "WHERE id = $1 AND user_id = $2 FOR UPDATE"),
            workspace_id,
            user_id,
        )
        if row is None:
            return None
        state = state_from_row(cast("Mapping[str, object]", row))
        if state.provider == "assistant":
            return None
        conversation_id = state.conversation_id or uuid.uuid4()
        _ = await _bind_chat(
            conn,
            user_id=user_id,
            state=state,
            conversation_id=conversation_id,
        )
        return await _open_chat_session(
            conn,
            store=store,
            user_id=user_id,
            conversation_id=conversation_id,
            provider=state.provider,
            assistant=assistant,
        )


async def read_chat_provider(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
) -> ChatProvider:
    """Say who answers a chat: the provider of the station its starter holds it in.

    Each person's station names its own provider, and the chat's queue has one
    drainer, so the starter's station decides; a chat no station of its starter
    holds is the assistant's.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The chat.

    Returns:
      provider: The starter's station's, else ``assistant``.

    """
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if starter is None:
        return "assistant"
    stored = await conn.fetchval(
        "SELECT station.state FROM visual_workspaces AS station "
        "JOIN users AS member ON member.id = station.user_id "
        "WHERE station.station ->> 'conversation_id' = $1 "
        "AND lower(member.email) = lower($2)",
        str(conversation_id),
        starter,
    )
    if stored is None:
        return "assistant"
    return WorkspaceData.model_validate(stored).provider


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
                    data=initial_data(
                        catalog,
                        provider=await _preferred_provider(conn, user_id=user_id),
                    ),
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
    provider: ChatProvider | None = None,
) -> WorkspaceState:
    """Create a station named with the lowest free ``unnamed-workstream#N``.

    Args:
      engine: Database connection source.
      user_id: Owner of the new station.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      workstream_id: Workstream assigned to the new station, if set.
      provider: Who answers its Chat; None for the owner's ``chat_provider``.

    Returns:
      state: The new station, with its partner.

    """
    async with engine.acquire() as conn:
        row = await _new_station(
            conn,
            user_id=user_id,
            data=initial_data(
                catalog,
                provider=provider or await _preferred_provider(conn, user_id=user_id),
            ),
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
            data=initial_data(
                catalog,
                provider=await _preferred_provider(conn, user_id=user_id),
            ),
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
            data=without_marks(WorkspaceData.model_validate(source["state"])),
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
            # The chat's, not the opener's preference: its queue has one drainer, and
            # only its starter's run may attach to it.
            row = await _new_station(
                conn,
                user_id=user_id,
                data=initial_data(
                    catalog,
                    provider=await read_chat_provider(
                        conn,
                        assistant=assistant,
                        conversation_id=conversation_id,
                    ),
                ),
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
        row = await conn.fetchrow(
            "SELECT id, station FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'conversation_id' = $2",
            user_id,
            str(conversation_id),
        )
    if row is None:
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
    stored = from_plain(row["station"], dict[str, object])
    # The station model reads every key, the suggestion included: treekle builds
    # dataclasses only, so ``from_plain`` refuses any stored pydantic value.
    station = StationData.model_validate(stored)
    legacy = from_plain(stored.get("campaign"), dict[str, object], default=None)
    station_id = from_plain(row["id"], uuid.UUID)
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
    workstream_id = station.workstream_id
    if workstream_id is None:
        workstream_id = await open_scratch(
            store.engine,
            store=store,
            user_id=user_id,
            email=email,
        )
    return WorkspaceSettingsRefs(
        station_id=station_id,
        workstream_issue_id=workstream_id,
        session_id=session_id,
        personal_artifact_id=await open_personal_settings(store, email=email),
        name=station.name,
        suggestion=station.suggestion,
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
    reader: SessionReader = NOBODY,
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
      reader: The caller, as the transcript test reads them: a session a mark covers
        is named only when they read it.

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
        return await _with_labels(
            conn,
            state=await attach_partner(
                conn,
                state=state,
                owner_id=owner_id,
                inbound=inbound,
                assistant=assistant,
            ),
            reader=reader,
        )


async def agent_may_operate(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent_api_key_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> bool:
    """Say whether an agent key may use a canvas: the check every operation makes.

    Args:
      engine: Database connection source.
      user_id: The key's account.
      workspace_id: The canvas.
      agent_api_key_id: The calling agent key.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      allowed: False when the canvas does not exist or the key is not its partner.

    Raises:
      WorkspaceKeyRefusedError: The account owns the canvas but this key is not
        its partner.

    """
    async with engine.acquire() as conn:
        return (
            await _workspace_for_principal(
                conn,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_api_key_id=agent_api_key_id,
                inbound=inbound,
                assistant=assistant,
            )
            is not None
        )


async def owns_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> bool:
    """Say whether a canvas is one of ``user_id``'s own stations.

    Args:
      engine: Database connection source.
      user_id: The signed-in browser account.
      workspace_id: The canvas.

    Returns:
      owned: True when the account owns the canvas.

    """
    async with engine.acquire() as conn:
        return (
            await conn.fetchval(
                "SELECT 1 FROM visual_workspaces WHERE id = $1 AND user_id = $2",
                workspace_id,
                user_id,
            )
            is not None
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
    placed_by: str | None = None,
    author: str = "",
    reader: SessionReader = NOBODY,
) -> AppliedOperation | None:
    """Apply one compare-and-swap operation and persist its replay receipt.

    A ``navigate``, ``highlight``, ``chat`` or ``preferences`` operation moves no visual
    and no revision: it is only recorded, for replay, and an agent key alone may send
    it. The one exception is a ``navigate`` on a canvas that has closed the page, which
    shows the page again in the next revision. ``preferences`` changes the station
    owner's preferences. A box, its removal or the clearing of all commutes with
    every other write, so it takes the next revision whatever revision it expects.
    A show names
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
      placed_by: Who a visual this operation adds is recorded as placed by.
      author: The caller's email, which a mark carries when drawn by a browser; a
        mark drawn by an agent key carries ``agent``.
      reader: The caller, as the transcript test reads them: a session a mark covers
        is named in the answer only when they read it.

    Returns:
      applied: The updated or replayed state with its partner, or None when the
        principal may not see the canvas.

    Raises:
      ProviderUnavailableError: A ``provider`` operation chose a provider the owner
        cannot run now; the message says why.

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
            # The receipt keeps the provider it was applied under; the partner is
            # computed from the provider now.
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
                ).model_copy(update={"provider": current.provider})
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
        if isinstance(body.operation, ChooseProvider):
            if agent_api_key_id is not None:
                raise ValueError("Only the canvas's owner chooses its Chat provider.")
            _, reasons = await provider_availability(conn, owner_id=owner_id)
            if reason := reasons.of(body.operation.provider):
                raise ProviderUnavailableError(reason)
        drawing = isinstance(body.operation, AddMark | RemoveMark | ClearMarks)
        if body.revision != current.revision and not drawing:
            raise RevisionConflictError(await with_partner(state=current))
        if isinstance(body.operation, ShowVisual):
            await _check_record(conn, operation=body.operation, catalog=catalog)
        updated = await _write_data(
            conn,
            current=current,
            data=apply_operation(
                _data_of(current),
                body.operation,
                catalog,
                placed_by=placed_by,
                author=AGENT_AUTHOR if agent_api_key_id else author,
                page=current.page,
            ),
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
        return AppliedOperation(
            state=await _with_labels(
                conn,
                state=await with_partner(state=updated),
                reader=reader,
            ),
            replayed=False,
        )


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
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
        provider=data.provider,
        marks=data.marks,
        marks_seq=data.marks_seq,
        marks_sent=data.marks_sent,
    )


# The chat's queue has one drainer, its starter's station's provider, as the drain route
# enforces: a sharer's post by their own station's provider would queue where that
# provider does not drain. A new chat, and the starter's own post, go by the station.
async def chat_answerer(
    conn: Conn,
    *,
    state: WorkspaceState,
    conversation_id: uuid.UUID,
    poster: str,
    assistant: Assistant | None,
) -> ChatProvider:
    """Say which provider answers a post into a chat from a station.

    The poster's own station decides in a chat they started; in one someone else
    started, the starter's station does (:func:`read_chat_provider`), since the
    chat's queue has one drainer.

    Args:
      conn: A database connection.
      state: The station the post comes from.
      conversation_id: The chat.
      poster: The poster's attested email.
      assistant: The configured assistant, if any.

    Returns:
      provider: The provider whose queue the post joins.

    """
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if starter is None or starter.lower() == poster.lower():
        return state.provider
    return await read_chat_provider(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )


# Both are None for a fork of a CLI station: its conversation is new, so its session is
# opened only once its station is made.
async def _post_target(
    conn: Conn,
    *,
    body: ChatSend,
    provider: ChatProvider,
    conversation_id: uuid.UUID,
    poster: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return the queue a post joins and the chat's session it names, if either is known."""
    if provider != "assistant":
        # A permission answer runs a tool on the run's machine, and a trust answer
        # trusts a folder there for good, so only the run's owner gives either; a
        # sharer reads and posts but never answers. A chat no session carries has no
        # run and no prompt: ``chat_line_target`` refuses it as offline.
        starter = (
            await read_starter(
                conn,
                assistant=assistant,
                conversation_id=conversation_id,
            )
            if body.kind == "answer"
            else None
        )
        if starter is not None and starter.lower() != poster.lower():
            raise AnswerNotOwnerError(
                f"Only {starter} can answer prompts from their machine.",
            )
        if body.fork is not None:
            return None, None
        chat = await chat_line_target(
            conn,
            inbound=inbound,
            assistant=assistant,
            provider=provider,
            conversation_id=conversation_id,
        )
        return chat, chat
    if body.kind != "line":
        raise ValueError(f"The assistant takes a person's line, not a {body.kind}")
    partner = await shared_partner(conn, inbound=inbound, assistant=assistant)
    if partner is None or partner.session_id is None or partner.status != "live":
        raise WorkspaceSessionUnavailableError("The assistant is not running")
    chat = await chat_line_target(
        conn,
        inbound=inbound,
        assistant=assistant,
        provider=provider,
        conversation_id=conversation_id,
    )
    return chat or partner.session_id, chat


async def _line_context(
    conn: Conn,
    *,
    body: ChatSend,
    state: WorkspaceState,
    station_id: uuid.UUID,
    conversation_id: uuid.UUID,
    reader: SessionReader,
) -> WorkspaceMessageContext:
    """Describe what the poster saw as they sent a line: page, trail and new marks."""
    screen = await read_screen(
        conn,
        page=body.page,
        trail=body.trail,
        visuals=state.visuals,
    )
    unsent = [m for m in state.marks if m.seq > state.marks_sent][-_MARKS_PER_LINE:]
    return WorkspaceMessageContext(
        workspace_id=station_id,
        visible_visuals=screen.visuals,
        agent_instructions=state.agent_instructions,
        continuation_record_id=state.continuation_record_id,
        conversation_id=conversation_id,
        fork=body.fork,
        page=screen.page,
        trail=screen.trail,
        marks=wire_marks(
            unsent,
            records=await read_covered_records(conn, marks=unsent, reader=reader),
            visuals=screen.visuals,
        ),
    )


# The session is opened under a key of the owner's because a science chat's starter is
# the account whose key opened its first session (``chat_beginnings_sql``): a session
# with no key would leave the chat without a starter, so no run of theirs could attach.
# A browser has no key, so the owner's newest live one stands in.
async def _open_chat_session(
    conn: Conn,
    *,
    store: Store,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    provider: ChatProvider,
    assistant: Assistant | None,
) -> uuid.UUID | None:
    """Return the chat's first session, opening one for its owner when it has none."""
    began = await read_first_session(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if began is not None:
        return began
    opener = await _owner_key(conn, user_id=user_id)
    if opener is None:
        return None
    email = from_plain(opener["email"], str)
    return await store.submit_agentsession(
        SubmitAgentSession(
            title=CHAT_TITLE,
            owner=chat_actor(conversation_id),
            account=email,
            cli=provider,
            cli_session_id=chat_session_id(conversation_id),
            labels=[SCIENCE_CHAT_LABEL],
        ),
        audience=OPEN,
        api_key_id=from_plain(opener["key"], uuid.UUID),
        actor=email,
        conn=conn,
    )


async def _owner_key(conn: Conn, *, user_id: uuid.UUID) -> Record | None:
    """Return the email and newest live key (``key``) of ``user_id``, if they have one."""
    return await conn.fetchrow(
        "SELECT member.email, credential.id AS key FROM users AS member "
        "JOIN api_keys AS credential ON credential.user_id = member.id "
        "WHERE member.id = $1 AND credential.revoked_at IS NULL "
        "ORDER BY credential.created_at DESC, credential.id DESC LIMIT 1",
        user_id,
    )


async def _preferred_provider(conn: Conn, *, user_id: uuid.UUID) -> ChatProvider:
    """Return the provider ``user_id`` starts a new chat on."""
    return stored_preferences(
        await conn.fetchval("SELECT preferences FROM users WHERE id = $1", user_id),
    ).chat_provider


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


# The session is opened under the key that writes the fork's next part, which must find
# it. A CLI run of the forker's attaches to it by its id. The assistant resumes a chat's
# session by its ``chat:`` id, which matches only a session its own key opened: under
# the forker's key it would open a second session, which would then be the chat's head
# and hold none of the copies. The edge is added under the same key, since only an edge
# its session's opener added counts as a fork (``science_chats._FORK_EDGES``). The
# copied parts are the forker's, whichever key opened the session.
async def _seed_fork(
    conn: Conn,
    *,
    store: Store,
    user_id: uuid.UUID,
    forker: str,
    conversation_id: uuid.UUID,
    fork: ChatForkAt,
    provider: ChatProvider,
    service: uuid.UUID | None,
    assistant: Assistant | None,
) -> uuid.UUID | None:
    """Return a fork's session, opening it once with the original's records copied."""
    began = await read_first_session(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if began is not None:
        return began
    own = await _owner_key(conn, user_id=user_id)
    own_key = None if own is None else from_plain(own["key"], uuid.UUID)
    if provider == "assistant":
        opener = from_plain(
            await conn.fetchval(
                "SELECT agentsession_opened_by_api_key_id FROM inquiries WHERE id = $1",
                service,
            ),
            uuid.UUID,
            default=None,
        )
    elif own_key is None:
        return None
    else:
        opener = own_key
    session = await store.submit_agentsession(
        SubmitAgentSession(
            title=CHAT_TITLE,
            owner=chat_actor(conversation_id),
            account=forker,
            cli="sagent" if provider == "assistant" else provider,
            cli_session_id=chat_session_id(conversation_id),
            labels=[SCIENCE_CHAT_LABEL],
        ),
        audience=OPEN,
        api_key_id=opener,
        actor=forker,
        conn=conn,
    )
    await store.copy_session_parts(
        conn,
        source=fork.session_id,
        target=session,
        prefixes=await read_fork_prefixes(conn, fork=fork),
        api_key_id=own_key,
    )
    _ = await store.add_edge(
        from_id=session,
        to_id=fork.session_id,
        edge_kind="produced_by",
        note=f"Forked after record {fork.idx} of part {fork.part}",
        labels=[FORK_EDGE_LABEL, fork_point_label(part=fork.part, idx=fork.idx)],
        api_key_id=opener,
        actor=forker,
        conn=conn,
    )
    return session


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
# without a chat has no partner to be, so it refuses every agent key. That partner is
# the shared assistant whatever the station's provider: a claude or codex run on the
# chat is reached through Chat, not given the canvas. The owner's own key is refused by
# name, so a terminal `trax workspace` learns why. Any other is told nothing.
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
    partner = await shared_partner(conn, inbound=inbound, assistant=assistant)
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
    """Return the stored part of a canvas's state."""
    return WorkspaceData(
        visuals=state.visuals,
        focused_instance=state.focused_instance,
        agent_instructions=state.agent_instructions,
        continuation_record_id=state.continuation_record_id,
        provider=state.provider,
        marks=state.marks,
        marks_seq=state.marks_seq,
        marks_sent=state.marks_sent,
    )


async def _with_labels(
    conn: Conn,
    *,
    state: WorkspaceState,
    reader: SessionReader,
) -> WorkspaceState:
    """Return ``state`` naming the records its marks cover that ``reader`` may see."""
    records = await read_covered_records(conn, marks=state.marks, reader=reader)
    return state.model_copy(
        update={
            "record_labels": {
                id_: f"{record.kind}#{record.seq} {record.title}"
                for id_, record in records.items()
            },
        },
    )


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
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
        provider=data.provider,
        marks=data.marks,
        marks_seq=data.marks_seq,
        marks_sent=data.marks_sent,
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
