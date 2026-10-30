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

from trackinizer.server.asks import asked_of
from trackinizer.server.audience import NOBODY, reads_session
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.preferences import (
    ASSISTANT_KEYS,
    Preferences,
    stored_preferences,
    write_preferences,
)
from trackinizer.server.store.deleted_chats import (
    chat_was_deleted,
    refuse_a_deleted_chat,
    session_was_purged,
)
from trackinizer.server.values import vetted_sql
from trackinizer.server.visuals.canvases import (
    AGENT_AUTHOR,
    AddMark,
    ApplyCanvasOperation,
    CanvasData,
    CanvasLayout,
    CanvasMetadata,
    CanvasSettingsRefs,
    CanvasState,
    CanvasSummary,
    CanvasView,
    CanvasViewRefused,
    ChatWindow,
    ChooseProvider,
    ClearMarks,
    Highlight,
    NameChat,
    Navigate,
    RemoveMark,
    SetPreferences,
    ShowVisual,
    WorkspaceSuggestion,
    apply_operation,
    initial_data,
    refuse_record,
    restore_page,
    without_marks,
)
from trackinizer.server.visuals.chat_forks import (
    ChatClosedError,
    ConversationTakenError,
    ForeignChatError,
)
from trackinizer.server.visuals.partners import (
    assistant_key_may_use,
    attach_partner,
    canvas_session_key_may_use,
    chat_run_states,
    cli_partner,
    live_chat_session,
    provider_availability,
    shared_partner,
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
    check_workspace,
    open_default_workspace,
    show_workspace,
)
from trackinizer.types.errors import ChatDeletedError, ConflictError
from trackinizer.wire.audience import OPEN
from trackinizer.wire.bodies import SubmitAgentSession, SubmitArtifact
from trackinizer.wire.chat_commands import (
    Command,
    Line,
    Refused,
    Settings,
    classify,
)
from trackinizer.wire.wire_canvas_keys import key_bounds
from trackinizer.wire.wire_science_chat import (
    CHAT_TITLE,
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    AssistantPrompt,
    ChatAnswer,
    ChatProvider,
    ChatSend,
    ChatSent,
    ChatSettings,
    chat_actor,
    chat_session_id,
    fork_point_label,
)
from trackinizer.wire.wire_sessions import (
    CHAT_TAKEN_OVER_MESSAGE,
    CanvasMessageContext,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.auth import Role
    from trackinizer.server.config import Assistant
    from trackinizer.server.store.core import Store
    from trackinizer.server.visuals.canvases import OpensAs
    from trackinizer.server.visuals.catalog import VisualCatalogBody
    from trackinizer.wire.wire_science_chat import ChatForkAt


UNNAMED_PREFIX: Final = "unnamed-canvas#"
"""The name a canvas has until its owner renames it, followed by a number."""

_UNNAMED: Final = re.compile(rf"{re.escape(UNNAMED_PREFIX)}([0-9]+)")

_CANVAS: Final = "id, revision, state, metadata"
"""The `visual_canvases` columns :func:`state_from_row` reads."""

_SELECT_CANVAS: Final = vetted_sql("SELECT ", _CANVAS, " FROM visual_canvases ")
_RETURNING_CANVAS: Final = vetted_sql(" RETURNING ", _CANVAS)

_MARKS_PER_LINE: Final = 20
"""Most marks one Chat line carries; the newest, when more were drawn."""

_NAME_ATTEMPTS: Final = 8
"""Tries at a free name before a canvas gives up: another create took it each time."""

PERSONAL_SETTINGS_LABEL: Final = "personal-settings"


class RevisionConflictError(Exception):
    """The caller based its operation on an older canvas revision."""

    def __init__(self, current: CanvasState) -> None:
        super().__init__("stale canvas revision")
        self.current = current


class ReplayConflictError(Exception):
    """An idempotency key was reused with a different operation."""

    def __init__(self, current: CanvasState) -> None:
        super().__init__("Idempotency-Key already used for another operation")
        self.current = current


class CanvasKeyRefusedError(Exception):
    """The API key has no session authorized to use this canvas."""


class CanvasSessionUnavailableError(Exception):
    """The canvas's partner has no session that can take this browser message."""


class ProviderOfflineError(CanvasSessionUnavailableError):
    """The canvas's provider is a CLI, and no run of it holds the chat."""


class ChatTakenOverError(CanvasSessionUnavailableError):
    """The chat's CLI was opened in a terminal, so its headless run takes no post."""


class ProviderUnavailableError(Exception):
    """The provider chosen cannot run for the canvas's owner; the message says why."""


class CommandRefusedError(ValueError):
    """A line is a slash command the chat's provider does not run from Chat."""


class AnswerNotOwnerError(Exception):
    """An answer to a run's prompt came from someone other than the run's owner."""


class UploadsUnsupportedError(Exception):
    """The chat's provider takes no files: only the assistant's box does, for now."""


class PartnerBusyError(Exception):
    """The partner's inbound queue is full, so a message would evict an unread one."""


class CanvasChatError(Exception):
    """A line would give a canvas a second chat, or one that another canvas holds."""


class CanvasNameTakenError(Exception):
    """The canvas's owner already has a canvas of that name."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliedOperation:
    """The canvas after an operation, and whether this call only replayed it."""

    state: CanvasState
    replayed: bool
    """The key had already been applied: nothing changed, so nothing is published."""

    canvases: tuple[uuid.UUID, ...] = ()
    """For a ``preferences`` change, every canvas of the owner, each to be told."""

    preferences: Preferences | None = None
    """For a ``preferences`` change, all of the owner's preferences after it, for
    their own tabs; the state carries only those its agent may see."""

    restored_page: bool = False
    """A ``navigate`` found the page closed and showed it again, so the canvas
    changed and its new state is to be published."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadTarget:
    """The chat a poster's files go with."""

    conversation_id: uuid.UUID | None
    """The chat; ``None`` before the canvas's first line starts one."""

    workspace_id: uuid.UUID
    """The canvas's workspace, or the poster's default: what names their box."""


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

    The line's conversation is the one it names, else the one its canvas already
    holds, else a new one named by ``key``, so a retry under the same key queues
    nothing again and answers with the same conversation. A canvas without a chat
    takes the line's conversation as its own, in the transaction that queues the
    line; a canvas holds one chat only. A fork starts a new canvas instead: a copy
    of the canvas it was sent from, provider and all, bound to the fork's
    conversation. Its session is opened in the same transaction, holding copies of
    the original's records up to and including the fork point and linked to the
    original, so whichever provider answers the fork finds the conversation it
    continues there. Its line is queued only once that transaction commits, so the
    session is there to resume when the line is heard.

    Nothing of the line is stored here: its provider records it in the
    conversation's session. That is the poster's canvas's provider, except in a chat
    someone else started, whose own provider (:func:`read_chat_provider`) drains it
    whatever the poster's canvas names. For ``assistant``, a conversation whose session the
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
    page, trail or marks, so the run types it bare; ``/model <name>`` and
    ``/effort <level>`` are queued as a ``settings`` post of that text and a
    :class:`~trackinizer.wire.wire_science_chat.ChatSettings` payload, the
    same way; one the provider does not run
    from Chat is refused before anything is queued or bound. An ``answer`` to a
    ``claude`` or ``codex`` run's prompt comes from the chat's starter alone, the
    run's owner: it runs a tool, or grants trust, on their machine. An ``answer`` to a
    card the assistant asked comes from the person the card asked (its starter's when
    it names nobody), queued for the chat's own session, where the card is, with the
    page, trail and marks a line carries: the assistant hears the pick as that
    person's line. An ``interrupt`` to the assistant is queued there too, where its
    turn runs. A line or answer to the assistant also carries the system prompt its
    poster chose (their ``assistant_prompt`` preference), which a conversation's agent
    starts on. A run taken over in a terminal takes no post of any kind.

    Args:
      store: The store, which opens and fills a fork's session.
      user_id: Authenticated browser account.
      body: The line, its canvas, and optional conversation, fork, page and trail.
      key: Required idempotency key.
      source: Attested browser principal email.
      source_role: That principal's role.
      inbound: Session message queue.
      assistant: The configured assistant, if any.
      reader: The poster, as the transcript test reads them: they post into a chat
        they read and started or were shared, and a fork point in a session they may
        not read is no line, as one past the end of a session is none.
      members: Everyone a chat the poster begins is open to besides them, as one
        reader (:func:`~trackinizer.server.audience.members_reader`); a fork
        is theirs to read, so they must read what it copies. ``None`` for no one.

    Returns:
      sent: The conversation, the canvas it went to, and its session when the line
        was queued on it; None for a foreign canvas.

    Raises:
      CanvasSessionUnavailableError: The canvas has no live partner.
      ProviderOfflineError: The canvas's provider is a CLI and no run of it holds
        the chat, or the forker has no key to open a fork's session for it with, or an
        ``answer`` or ``interrupt`` is posted to an assistant chat the assistant does
        not have open.
      ChatTakenOverError: The chat's run was taken over in a terminal.
      PartnerBusyError: The queue the line would join is full.
      CanvasChatError: The canvas's chat is another conversation, or another
        canvas of the poster holds the one named.
      IdempotencyReuseError: ``key`` already posted another line.
      ForeignChatError: The poster neither started the conversation nor was shared
        it, or does not read it: of any kind, the post is refused before anything is
        queued, and a reader forks the chat instead.
      ChatClosedError: The fork point is a line of a ``private`` or ``staff`` chat,
        whose audience a fork's copies would not keep, or of a chat ``members`` do
        not read.
      ConversationTakenError: The fork's key names a conversation someone else began.
      ChatDeletedError: The line's conversation, or the fork's key, names a chat that
        was deleted, or the fork point is a line of one.
      CommandRefusedError: The line is a slash command its provider does not run
        from Chat; the message says why. It is a ``ValueError``.
      AnswerNotOwnerError: An ``answer`` came from someone other than the chat's
        starter.
      UploadsUnsupportedError: The line names attachments and its provider is not
        the assistant, whose box alone takes files for now.
      ValueError: The fork names no line of a science chat, or an ``assistant``
        canvas is sent a post that is not a line, an answer or an interrupt.

    """
    async with store.engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                _SELECT_CANVAS,
                "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            ),
            body.canvas_id,
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
        if body.attachments and provider != "assistant":
            raise UploadsUnsupportedError(f"A {provider} chat takes no files yet")
        if body.kind == "line":
            match classify(body.text, provider):
                case Command(text=text):
                    body = body.model_copy(update={"kind": "command", "text": text})
                case Settings(text=text, model=model, effort=effort):
                    body = body.model_copy(
                        update={
                            "kind": "settings",
                            "text": text,
                            "payload": ChatSettings(
                                model=model,
                                effort=effort,
                            ).model_dump(),
                        },
                    )
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
        # Last, so a refused line leaves the canvas as it was.
        canvas_id = (
            await _fork_canvas(
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
                canvas_id=canvas_id,
                conversation_id=conversation_id,
                reader=reader,
                assistant_prompt=await _chosen_prompt(conn, user_id=user_id)
                if provider == "assistant"
                else None,
            )
            # An answer to the assistant's card is heard as the person's line.
            if body.kind == "line"
            or (body.kind == "answer" and provider == "assistant")
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
                    str(body.canvas_id),
                    body.text,
                    str(body.fork),
                    body.kind,
                    json.dumps(body.payload, sort_keys=True),
                    json.dumps(body.attachments),
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
                "UPDATE visual_canvases SET state = jsonb_set(state, "
                "'{marks_sent}', to_jsonb($2::int)) WHERE id = $1",
                body.canvas_id,
                state.marks_seq,
            )
    if body.fork is not None:
        # Whoever hears the line resumes the fork's session at once: before the commit
        # it would find none and open a second one, holding none of the copies.
        _ = inbound.send_once(key, [line], fingerprint=fingerprint)
    return ChatSent(
        conversation_id=conversation_id,
        session_id=chat,
        canvas_id=canvas_id,
    )


async def check_chat_upload(
    store: Store,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    source: str,
    assistant: Assistant | None,
    inbound: InboundQueue,
    reader: SessionReader,
) -> UploadTarget | None:
    """Say which chat files go with, if the poster may post a line there.

    The rule is :func:`send_chat_line`'s, so whoever may post a line may upload for
    it and no one else: the canvas is the poster's own; a chat it names, or the one
    the canvas holds, is not deleted and is the poster's to post into; and its
    provider is the assistant, whose box alone takes files for now. A canvas with no
    chat yet uploads for the chat its first line will start.

    Args:
      store: The store.
      user_id: Authenticated browser account.
      canvas_id: The canvas the line would be sent from.
      conversation_id: The chat the page names, if any.
      source: Attested browser principal email.
      assistant: The configured assistant, if any.
      inbound: Session message queue, which knows the sender of a first line.
      reader: The poster, as the transcript test reads them.

    Returns:
      target: The chat the files go with and the workspace that names the poster's
        box, as a chat's settings name it; ``None`` for a foreign canvas.

    Raises:
      CanvasChatError: The canvas holds another chat than the one named.
      ChatDeletedError: The chat was deleted.
      ForeignChatError: The poster may not post into the chat.
      UploadsUnsupportedError: The chat's provider is not the assistant.

    """
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            vetted_sql(_SELECT_CANVAS, "WHERE id = $1 AND user_id = $2"),
            canvas_id,
            user_id,
        )
        if row is None:
            return None
        state = state_from_row(cast("Mapping[str, object]", row))
        if None not in {conversation_id, state.conversation_id} and (
            conversation_id != state.conversation_id
        ):
            raise CanvasChatError("This canvas's chat is another conversation")
        chat = conversation_id or state.conversation_id
        provider = state.provider
        if chat is not None:
            await refuse_a_deleted_chat(conn, conversation_id=chat, forked_from=None)
            await _check_poster(
                conn,
                conversation_id=chat,
                email=source,
                assistant=assistant,
                inbound=inbound,
                reader=reader,
            )
            provider = await chat_answerer(
                conn,
                state=state,
                conversation_id=chat,
                poster=source,
                assistant=assistant,
            )
    if provider != "assistant":
        raise UploadsUnsupportedError(f"A {provider} chat takes no files yet")
    return UploadTarget(
        conversation_id=chat,
        workspace_id=await _workspace_or_default(
            store,
            user_id=user_id,
            email=source,
            workspace_id=state.workspace_id,
        ),
    )


async def chat_line_target(
    conn: Conn,
    *,
    inbound: InboundQueue,
    assistant: Assistant | None,
    provider: ChatProvider,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Say which session a canvas's line for a chat goes to.

    A Chat post and a Console relay both ask this. For ``assistant`` only the
    assistant drains a chat's own session, so a line goes there when the assistant
    has it open. For a CLI it goes to the chat's session its run drains.

    Args:
      conn: A database connection.
      inbound: Session message queue, which knows who polls each session.
      assistant: The configured assistant, if any.
      provider: The canvas's provider.
      conversation_id: The chat.

    Returns:
      session: The chat's own session its provider has open; None, for
        ``assistant`` alone, sends the line to the partner's service session.

    Raises:
      ProviderOfflineError: The provider is a CLI and no run of it holds the chat.
      ChatTakenOverError: The run's CLI was opened in a terminal, which the run
        reported; its headless run is stopped, so a post would reach no one.

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
        if partner.state == "taken_over":
            raise ChatTakenOverError(CHAT_TAKEN_OVER_MESSAGE)
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
    canvas_id: uuid.UUID,
    assistant: Assistant | None,
) -> uuid.UUID | None:
    """Give a CLI canvas's chat a session a run can attach to, before any line.

    A canvas without a chat is bound to a new conversation first. A chat that has a
    session keeps it: the run attaches to that one. A new session opens with the
    canvas's ``opens_as``. An ``assistant`` canvas is left as it is, since the
    assistant opens its chat's session when it hears the chat.

    Args:
      store: The store.
      user_id: The canvas's owner.
      canvas_id: The canvas.
      assistant: The configured assistant, whose session of the chat is one too.

    Returns:
      session_id: The chat's session; None for a canvas that is not the owner's,
        an ``assistant`` one, or an owner with no API key to open it with.

    """
    async with store.engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(_SELECT_CANVAS, "WHERE id = $1 AND user_id = $2 FOR UPDATE"),
            canvas_id,
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
            audience=state.opens_as,
            assistant=assistant,
        )


async def read_chat_provider(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
) -> ChatProvider:
    """Say who answers a chat: the provider of the canvas its starter holds it in.

    Each person's canvas names its own provider, and the chat's queue has one
    drainer, so the starter's canvas decides; a chat no canvas of its starter
    holds is the assistant's.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The chat.

    Returns:
      provider: The starter's canvas's, else ``assistant``.

    """
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if starter is None:
        return "assistant"
    stored = await conn.fetchval(
        "SELECT canvas.state FROM visual_canvases AS canvas "
        "JOIN users AS member ON member.id = canvas.user_id "
        "WHERE canvas.metadata ->> 'conversation_id' = $1 "
        "AND lower(member.email) = lower($2)",
        str(conversation_id),
        starter,
    )
    if stored is None:
        return "assistant"
    return CanvasData.model_validate(stored).provider


# Before its first session no session names a chat's starter, so every canvas that holds
# it is asked: in practice the one its first line was posted from, which bound it. Any
# of them choosing ``private`` is enough, since a choice can only narrow who reads it.
async def read_opens_as(conn: Conn, *, conversation_id: uuid.UUID) -> OpensAs:
    """Say the access a chat with no first session yet opens with: its canvas's choice.

    Args:
      conn: A database connection.
      conversation_id: The chat.

    Returns:
      opens_as: ``private`` when a canvas that holds the chat chose it, else ``open``.

    """
    private = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM visual_canvases "
        "WHERE metadata ->> 'conversation_id' = $1 AND state ->> 'opens_as' = $2)",
        str(conversation_id),
        "private",
    )
    return "private" if private else "open"


async def open_latest_canvas(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState:
    """Return the canvas the user changed last, creating the first once.

    Hidden canvases do not reopen from the canvas list. If none remain visible, a
    new canvas is made in the user's default workspace, which is made only then.
    Two first requests that race read the visible winner.

    Args:
      engine: Database connection source.
      store: Store that makes the default workspace if a canvas lands in it.
      user_id: Owner of the canvases.
      email: The owner's account, which owns the default workspace.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The existing or newly created canvas, with its partner.

    """
    latest = vetted_sql(
        _SELECT_CANVAS,
        "WHERE user_id = $1 AND coalesce((metadata->>'hidden')::boolean, FALSE) "
        "= FALSE AND NOT metadata ? 'session_id' "
        "ORDER BY modified_at DESC, id DESC LIMIT 1",
    )
    async with engine.acquire() as conn:
        row = await conn.fetchrow(latest, user_id)
    workspace_id = None
    if row is None:
        workspace_id = await open_default_workspace(
            engine,
            store=store,
            user_id=user_id,
            email=email,
        )
    async with engine.acquire() as conn:
        if row is None:
            names = await conn.fetch(
                "SELECT metadata->>'name' AS name FROM visual_canvases "
                "WHERE user_id = $1 AND metadata->>'name' LIKE $2",
                user_id,
                f"{UNNAMED_PREFIX}%",
            )
            # Another first-open may have inserted a visible canvas during the
            # names query; reuse it instead of creating a second canvas.
            row = await conn.fetchrow(latest, user_id)
            if row is None:
                _ = await _insert_canvas(
                    conn,
                    user_id=user_id,
                    name=next_canvas_name(from_plain(r["name"], str) for r in names),
                    data=initial_data(
                        catalog,
                        provider=await _preferred_provider(conn, user_id=user_id),
                    ),
                    workspace_id=workspace_id,
                )
                row = await conn.fetchrow(latest, user_id)
        if row is None:
            raise RuntimeError("first canvas insert was not visible")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            owner_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def new_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    workspace_id: uuid.UUID | None = None,
    provider: ChatProvider | None = None,
) -> CanvasState:
    """Create a canvas named with the lowest free ``unnamed-canvas#N``.

    Args:
      engine: Database connection source.
      user_id: Owner of the new canvas.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      workspace_id: Workspace assigned to the new canvas, if set.
      provider: Who answers its Chat; None for the owner's ``chat_provider``.

    Returns:
      state: The new canvas, with its partner.

    """
    async with engine.acquire() as conn:
        row = await _new_canvas(
            conn,
            user_id=user_id,
            data=initial_data(
                catalog,
                provider=provider or await _preferred_provider(conn, user_id=user_id),
            ),
            workspace_id=workspace_id,
        )
        if row is None:
            raise RuntimeError("no free canvas name")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            owner_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def new_session_canvas(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    name: str,
    catalog: VisualCatalogBody,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
) -> uuid.UUID:
    """Make a canvas for a placed session, named ``name`` or the first free variant.

    Args:
      conn: A database connection, inside the caller's transaction.
      user_id: Owner of the new canvas.
      name: The wanted name, such as the session's actor and CLI.
      catalog: Trusted visual definitions and the initial selection.
      workspace_id: The workspace the canvas sits in.
      session_id: The session the canvas is made for.

    Returns:
      canvas_id: The new canvas.

    """
    for _ in range(_NAME_ATTEMPTS):
        names = await conn.fetch(
            "SELECT metadata->>'name' AS name FROM visual_canvases WHERE user_id = $1",
            user_id,
        )
        row = await _insert_canvas(
            conn,
            user_id=user_id,
            name=_unique_canvas_name(
                name,
                names=(from_plain(each["name"], str) for each in names),
            ),
            data=initial_data(
                catalog,
                provider=await _preferred_provider(conn, user_id=user_id),
            ),
            workspace_id=workspace_id,
            session_id=session_id,
        )
        if row is not None:
            return cast(uuid.UUID, row["id"])
    raise RuntimeError("no free canvas name")


async def clear_chat_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    key: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Make or return one cleared copy of an owned canvas.

    The source row lock serializes clears of the same canvas. A retry checks its
    owner-scoped key only after acquiring that lock, so it returns the first copy.

    Args:
      engine: Database connection source.
      user_id: Owner of the source canvas.
      canvas_id: Canvas whose canvas is copied.
      key: Retry-safe idempotency key.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The copied canvas, or None when the source is absent or foreign.

    """
    async with engine.acquire() as conn, tx(conn):
        source = await conn.fetchrow(
            vetted_sql(
                _SELECT_CANVAS,
                "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            ),
            canvas_id,
            user_id,
        )
        if source is None:
            return None
        existing = await conn.fetchrow(
            vetted_sql(
                _SELECT_CANVAS,
                "WHERE user_id = $1 AND metadata->>'clear_key' = $2 LIMIT 1",
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
        row = await _new_canvas(
            conn,
            user_id=user_id,
            data=without_marks(CanvasData.model_validate(source["state"])),
            page=source_state.page,
            layout=source_state.layout,
            clear_key=key,
            workspace_id=source_state.workspace_id,
        )
        if row is None:
            raise RuntimeError("clear canvas insert was not visible")
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def canvas_for_chat(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    conversation_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState:
    """Find the user's canvas that holds a conversation, or make one bound to it.

    A canvas made for it lands in the user's default workspace, made only then.

    Args:
      engine: Database connection source.
      store: Store that makes the default workspace if a canvas lands in it.
      user_id: Owner of the canvas.
      email: The owner's account, which owns the default workspace.
      conversation_id: The science chat to continue.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The canvas that holds the conversation, with its partner.

    Raises:
      ChatDeletedError: The conversation was deleted.

    """
    async with engine.acquire() as conn:
        held = await _chat_canvas_row(
            conn,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        # A deleted chat is refused below, with nothing landed for it.
        lands = held is None and not await chat_was_deleted(
            conn,
            conversation_id=conversation_id,
        )
    workspace_id = None
    if lands:
        workspace_id = await open_default_workspace(
            engine,
            store=store,
            user_id=user_id,
            email=email,
        )
    async with engine.acquire() as conn, tx(conn):
        await refuse_a_deleted_chat(conn, conversation_id=conversation_id)
        row = await _chat_canvas_row(
            conn,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if row is None:
            # The chat's, not the opener's preference: its queue has one drainer, and
            # only its starter's run may attach to it.
            row = await _new_canvas(
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
                workspace_id=workspace_id,
            )
        if row is None:
            # A racing request bound the conversation to a canvas first.
            row = await _chat_canvas_row(
                conn,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        if row is None:
            raise RuntimeError("no free canvas name")
        if CanvasMetadata.model_validate(row["metadata"]).hidden:
            row = await conn.fetchrow(
                vetted_sql(
                    "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || ",
                    "jsonb_build_object('hidden', FALSE), "
                    "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                    _RETURNING_CANVAS,
                ),
                row["id"],
                user_id,
            )
            if row is None:
                raise RuntimeError("chat canvas disappeared while it was unhidden")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            owner_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def list_canvases(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> list[CanvasSummary]:
    """List the user's canvases, the one changed last first.

    Args:
      engine: Database connection source.
      user_id: Owner of the canvases.
      inbound: In-process poller leases, which say what each chat's run is doing.
      assistant: The configured assistant, whose chats' open asks show too.

    Returns:
      canvases: Every canvas of the user, each with what its chat needs of them.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, metadata, created_at, modified_at FROM visual_canvases "
            "WHERE user_id = $1 ORDER BY modified_at DESC, id DESC",
            user_id,
        )
        canvases = [CanvasMetadata.model_validate(row["metadata"]) for row in rows]
        owner = from_plain(
            await conn.fetchval("SELECT email FROM users WHERE id = $1", user_id),
            str,
            default="",
        )
        states = await chat_run_states(
            conn,
            inbound=inbound,
            openers=[owner, *([] if assistant is None else [assistant.email])],
            conversation_ids=[
                each.conversation_id
                for each in canvases
                if each.conversation_id is not None
            ],
        )
    return [
        CanvasSummary.of(
            data,
            canvas_id=cast("uuid.UUID", row["id"]),
            created_at=cast("datetime", row["created_at"]),
            modified_at=cast("datetime", row["modified_at"]),
            state=states.get(data.conversation_id, "idle")
            if data.conversation_id is not None
            else "idle",
        )
        for row, data in zip(rows, canvases, strict=True)
    ]


async def rename_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    name: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Rename one of the user's canvases.

    Args:
      engine: Database connection source.
      user_id: Owner of the canvas.
      canvas_id: The canvas.
      name: The new name, already trimmed and within 1 to 120 characters.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The renamed canvas with its partner, or None when the user has no
        such canvas.

    Raises:
      CanvasNameTakenError: The user has another canvas of that name.

    """
    async with engine.acquire() as conn:
        if await conn.fetchval(
            "SELECT 1 FROM visual_canvases "
            "WHERE user_id = $1 AND metadata->>'name' = $2 AND id <> $3",
            user_id,
            name,
            canvas_id,
        ):
            raise CanvasNameTakenError("You already have a canvas with that name")
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || ",
                "jsonb_build_object('name', $3::text), modified_at = clock_timestamp() ",
                "WHERE id = $1 AND user_id = $2",
                _RETURNING_CANVAS,
            ),
            canvas_id,
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


async def name_unnamed_canvas(
    engine: DatabaseEngine,
    *,
    email: str,
    conversation_id: uuid.UUID,
    name: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Name the asker's canvas for a chat, only while its placeholder remains.

    Existing names are returned unchanged. A conflicting requested name receives
    the first available numeric suffix, so the assistant never displaces a canvas.

    Args:
      engine: Database connection source.
      email: The canvas owner, as resolved for the assistant.
      conversation_id: The chat held by the canvas.
      name: Suggested canvas name.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The named or already-named canvas, or None when the asker has no
        canvas for this chat.

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
            "SELECT vw.id, vw.revision, vw.state, vw.metadata "
            "FROM visual_canvases vw WHERE vw.user_id = $1 "
            "AND vw.metadata->>'conversation_id' = $2 FOR UPDATE",
            user_id,
            str(conversation_id),
        )
        if row is None:
            return None
        canvas = CanvasMetadata.model_validate(row["metadata"])
        if _UNNAMED.fullmatch(canvas.name) is None:
            return await _with_partner(
                conn,
                row=row,
                user_id=user_id,
                inbound=inbound,
                assistant=assistant,
            )
        rows = await conn.fetch(
            "SELECT metadata->>'name' AS name FROM visual_canvases "
            "WHERE user_id = $1 AND id <> $2",
            user_id,
            from_plain(row["id"], uuid.UUID),
        )
        unique_name = _unique_canvas_name(
            checked.name,
            names=[from_plain(item["name"], str) for item in rows],
        )
        updated = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || ",
                "jsonb_build_object('name', $3::text), modified_at = clock_timestamp() ",
                "WHERE id = $1 AND user_id = $2 AND metadata->>'name' ~ $4",
                _RETURNING_CANVAS,
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


async def suggest_workspace_for_chat(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    conversation_id: uuid.UUID,
    suggestion: WorkspaceSuggestion,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Store a workspace suggestion on the asker's canvas for a chat.

    This writes canvas JSON only; it never creates a workspace Issue.

    Args:
      engine: Database connection source.
      user_id: The asker whose canvas holds the chat.
      email: The asker's Issue owner.
      conversation_id: The chat held by the canvas.
      suggestion: Suggested title and optional owned workspace.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated canvas, or None when the canvas or target is unavailable.

    """
    if suggestion.workspace_id is not None and not await check_workspace(
        engine,
        email=email,
        workspace_id=suggestion.workspace_id,
    ):
        return None
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "SELECT ",
                _CANVAS,
                " FROM visual_canvases WHERE user_id = $1 "
                "AND metadata->>'conversation_id' = $2 FOR UPDATE",
            ),
            user_id,
            str(conversation_id),
        )
        if row is None:
            return None
        updated = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || ",
                "jsonb_build_object('suggestion', $3::jsonb), ",
                "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                _RETURNING_CANVAS,
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


async def set_canvas_flags(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    hidden: bool | None = None,
    pinned: bool | None = None,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Set supplied canvas flags and return the updated state.

    Only supplied flags change. The canvas row lock and write share one transaction
    so a concurrent canvas operation cannot erase the flags.

    Args:
      engine: Database connection source.
      user_id: Owner of the canvas.
      canvas_id: The canvas to update.
      hidden: Whether to hide the canvas, or None to leave it unchanged.
      pinned: Whether to pin the canvas, or None to leave it unchanged.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated canvas with its partner, or None when not owned.

    """
    if hidden is None and pinned is None:
        raise ValueError("At least one canvas flag is required.")
    changes = {
        name: value
        for name, value in (("hidden", hidden), ("pinned", pinned))
        if value is not None
    }
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || ",
                "$3::jsonb, modified_at = clock_timestamp() ",
                "WHERE id = $1 AND user_id = $2",
                _RETURNING_CANVAS,
            ),
            canvas_id,
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


async def set_canvas_opens_as(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    opens_as: OpensAs,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Set the access an owned canvas's next new chat opens with.

    No revision changes, as for a flag. The statement merges the one key, and an
    operation rewrites the state only under the row lock it read it with, so neither
    undoes the other. A chat that already has its first session keeps its access.

    Args:
      engine: Database connection source.
      user_id: Owner of the canvas.
      canvas_id: The canvas.
      opens_as: ``open`` or ``private``.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated canvas with its partner, or None when not owned.

    """
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_canvases SET state = state || ",
                "jsonb_build_object('opens_as', $3::text), ",
                "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                _RETURNING_CANVAS,
            ),
            canvas_id,
            user_id,
            opens_as,
        )
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def set_canvas_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    email: str,
    canvas_id: uuid.UUID,
    workspace_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState | None:
    """Move an owned canvas after checking its destination workspace.

    Args:
      engine: Database connection source.
      user_id: Canvas owner.
      email: The owner's Issue identity.
      canvas_id: Canvas to move.
      workspace_id: Owned workspace Issue to attach.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The updated canvas, or None when either id is unavailable.

    """
    if not await check_workspace(engine, email=email, workspace_id=workspace_id):
        return None
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "UPDATE visual_canvases SET metadata = ",
                "(coalesce(metadata, '{}'::jsonb) - 'suggestion') || ",
                "jsonb_build_object('workspace_id', $3::text), ",
                "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2",
                _RETURNING_CANVAS,
            ),
            canvas_id,
            user_id,
            str(workspace_id),
        )
        return await _with_partner(
            conn,
            row=row,
            user_id=user_id,
            inbound=inbound,
            assistant=assistant,
        )


async def set_canvas_page(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    route: str,
) -> bool:
    """Remember the page a canvas is on. No revision changes and nothing is published.

    The canvas is then the one changed last, which the app opens.

    Args:
      engine: Database connection source.
      user_id: Owner of the canvas.
      canvas_id: The canvas.
      route: The `#/...` hash, already validated.

    Returns:
      found: Whether the user has such a canvas.

    """
    async with engine.acquire() as conn:
        return (
            await conn.fetchval(
                "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || "
                "jsonb_build_object('page', $3::text), modified_at = clock_timestamp() "
                "WHERE id = $1 AND user_id = $2 RETURNING 1",
                canvas_id,
                user_id,
                route,
            )
            is not None
        )


async def set_canvas_layout(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    layout: CanvasLayout,
) -> bool:
    """Remember how a canvas's columns and windows were left.

    Each key the layout names replaces the stored one and the rest stay. No
    revision changes and nothing is published: a layout write never races an
    operation.

    Args:
      engine: Database connection source.
      user_id: Owner of the canvas.
      canvas_id: The canvas.
      layout: The keys to store, already validated.

    Returns:
      found: Whether the user has such a canvas.

    """
    async with engine.acquire() as conn:
        return (
            await conn.fetchval(
                "UPDATE visual_canvases SET metadata = jsonb_set("
                "coalesce(metadata, '{}'), '{layout}', "
                "coalesce(metadata->'layout', '{}') || $3::jsonb), "
                "modified_at = clock_timestamp() "
                "WHERE id = $1 AND user_id = $2 RETURNING 1",
                canvas_id,
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
) -> CanvasSettingsRefs:
    """Resolve a chat's canvas workspace and campaign settings rows.

    Args:
      store: The server store.
      user_id: The asker's user id.
      email: The asker's email.
      conversation_id: The science chat.
      session_id: Its head AgentSession, if it has one.

    Returns:
      refs: The workspace (the canvas's if the asker owns it, else their default),
        session, personal Artifact and canvas metadata.

    """
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, metadata FROM visual_canvases "
            "WHERE user_id = $1 AND metadata->>'conversation_id' = $2",
            user_id,
            str(conversation_id),
        )
    if row is None:
        return CanvasSettingsRefs(
            session_id=session_id,
            workspace_issue_id=await _workspace_or_default(
                store,
                user_id=user_id,
                email=email,
                workspace_id=None,
            ),
            personal_artifact_id=await open_personal_settings(store, email=email),
        )
    stored = from_plain(row["metadata"], dict[str, object])
    # The canvas model reads every key, the suggestion included: treekle builds
    # dataclasses only, so ``from_plain`` refuses any stored pydantic value.
    canvas = CanvasMetadata.model_validate(stored)
    legacy = from_plain(stored.get("campaign"), dict[str, object], default=None)
    canvas_id = from_plain(row["id"], uuid.UUID)
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
                    "UPDATE visual_canvases SET metadata = metadata - 'campaign' "
                    "WHERE id = $1 AND user_id = $2 AND metadata ? 'campaign'",
                    canvas_id,
                    user_id,
                )
    return CanvasSettingsRefs(
        canvas_id=canvas_id,
        workspace_issue_id=await _workspace_or_default(
            store,
            user_id=user_id,
            email=email,
            workspace_id=canvas.workspace_id,
        ),
        session_id=session_id,
        personal_artifact_id=await open_personal_settings(store, email=email),
        name=canvas.name,
        suggestion=canvas.suggestion,
    )


def next_canvas_name(names: Iterable[str]) -> str:
    """Name a canvas with the lowest ``unnamed-canvas#N`` not in ``names``.

    Args:
      names: The names the user's canvases have.

    Returns:
      name: ``unnamed-canvas#N`` for the smallest N of 1 or more that is free.

    """
    taken = {int(m[1]) for name in names if (m := _UNNAMED.fullmatch(name))}
    return f"{UNNAMED_PREFIX}{next(n for n in count(1) if n not in taken)}"


async def read_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
    agent_api_key_id: uuid.UUID | None = None,
    reader: SessionReader = NOBODY,
) -> CanvasState | None:
    """Read one canvas for its owner, or for an agent key allowed to use it.

    Args:
      engine: Database connection source.
      user_id: Authenticated principal.
      canvas_id: Requested canvas.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      agent_api_key_id: The calling agent key, if any; see
        :func:`_canvas_for_principal` for what it may read.
      reader: The caller, as the transcript test reads them: a session a mark covers
        is named only when they read it.

    Returns:
      state: Canvas with its partner, or None when the principal may not see it.

    """
    async with engine.acquire() as conn:
        found = await _canvas_for_principal(
            conn,
            canvas_id=canvas_id,
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


async def view_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
    reader: SessionReader,
) -> CanvasView | CanvasViewRefused | None:
    """Show one canvas to its owner, or to whom its chat lets in, read only.

    Who may see a canvas is who reads its chat, as the chat's own head says
    (:func:`~trackinizer.server.audience.reads_session` of the chat's first
    session); a canvas that holds no chat yet is its owner's alone. A refusal names
    the owner, to ask for access, and holds nothing of the canvas.

    Args:
      engine: Database connection source.
      user_id: The person asking.
      canvas_id: The canvas.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      reader: The person asking, as the transcript test reads them.

    Returns:
      view: The canvas and its owner; a refusal naming the owner; None when there is
        no such canvas.

    """
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            vetted_sql(
                "SELECT user_id, ",
                _CANVAS,
                " FROM visual_canvases WHERE id = $1",
            ),
            canvas_id,
        )
        if row is None:
            return None
        owner_id = cast(uuid.UUID, row["user_id"])
        owner = from_plain(
            await conn.fetchval("SELECT email FROM users WHERE id = $1", owner_id),
            str,
        )
        state = state_from_row(cast("Mapping[str, object]", row))
        if owner_id != user_id:
            first = (
                None
                if state.conversation_id is None
                else await read_first_session(
                    conn,
                    assistant=assistant,
                    conversation_id=state.conversation_id,
                )
            )
            if first is None or not await reads_session(
                conn,
                reader=reader,
                session_id=first,
            ):
                return CanvasViewRefused(
                    detail=f"Ask {owner} for access to this canvas.",
                    owner=owner,
                )
        shown = await _with_labels(
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
    # The preferences a canvas carries are its owner's, for their assistant to see.
    return CanvasView(
        canvas=shown.model_copy(update={"preferences": None}),
        owner=owner,
    )


async def agent_may_operate(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    agent_api_key_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> bool:
    """Say whether an agent key may use a canvas: the check every operation makes.

    Args:
      engine: Database connection source.
      user_id: The key's account.
      canvas_id: The canvas.
      agent_api_key_id: The calling agent key.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      allowed: False when the canvas does not exist or the key has no authorized
        session associated with it.

    Raises:
      CanvasKeyRefusedError: The account owns the canvas but this key has no
        authorized session associated with it.

    """
    async with engine.acquire() as conn:
        return (
            await _canvas_for_principal(
                conn,
                canvas_id=canvas_id,
                user_id=user_id,
                agent_api_key_id=agent_api_key_id,
                inbound=inbound,
                assistant=assistant,
            )
            is not None
        )


async def owns_canvas(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
) -> bool:
    """Say whether a canvas is one of ``user_id``'s own canvases.

    Args:
      engine: Database connection source.
      user_id: The signed-in browser account.
      canvas_id: The canvas.

    Returns:
      owned: True when the account owns the canvas.

    """
    async with engine.acquire() as conn:
        return (
            await conn.fetchval(
                "SELECT 1 FROM visual_canvases WHERE id = $1 AND user_id = $2",
                canvas_id,
                user_id,
            )
            is not None
        )


async def canvas_ids(conn: Conn, *, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Return every canvas of ``user_id``: where a change of theirs is published.

    Args:
      conn: The connection to read on.
      user_id: Whose canvases.

    Returns:
      ids: Their canvases' ids, in no order.

    """
    rows = await conn.fetch(
        "SELECT id FROM visual_canvases WHERE user_id = $1",
        user_id,
    )
    return [cast(uuid.UUID, row["id"]) for row in rows]


async def canvases_of_key(conn: Conn, *, key: str) -> list[uuid.UUID]:
    """Return the canvases whose id starts with ``key``, at most two.

    Two are enough to tell a key that names one canvas from one that names several.

    Args:
      conn: The connection to read on.
      key: A canvas key (:mod:`~trackinizer.wire.wire_canvas_keys`).

    Returns:
      ids: None, the one, or two of the canvases the key names.

    """
    low, high = key_bounds(key)
    rows = await conn.fetch(
        "SELECT id FROM visual_canvases WHERE id BETWEEN $1 AND $2 ORDER BY id LIMIT 2",
        low,
        high,
    )
    return [cast(uuid.UUID, row["id"]) for row in rows]


async def apply_canvas_operation(
    store: Store,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    key: uuid.UUID,
    body: ApplyCanvasOperation,
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
    shows the page again in the next revision. ``preferences`` changes the canvas
    owner's preferences. A box, its removal or the clearing of all commutes with
    every other write, so it takes the next revision whatever revision it expects.
    A show names
    a record the visual accepts: its catalog lists the kinds, and one that takes no
    record refuses any.

    Args:
      store: Persistence and session lifecycle operations.
      user_id: Authenticated principal.
      canvas_id: Target canvas.
      key: Idempotency key for this operation.
      body: Expected revision and validated operation.
      catalog: Trusted descriptor set the operation is validated against.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      agent_api_key_id: Calling API key, or None for browser cookie auth; see
        :func:`_canvas_for_principal` for what it may operate.
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
    async with notify_after_commit(), store.engine.acquire() as conn, tx(conn):
        found = await _canvas_for_principal(
            conn,
            canvas_id=canvas_id,
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
            "SELECT request_hash, response FROM visual_canvas_operations "
            "WHERE canvas_id = $1 AND key = $2",
            canvas_id,
            key,
        )
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise ReplayConflictError(await with_partner(state=current))
            # The receipt keeps the provider it was applied under; the partner is
            # computed from the provider now. ``opens_as`` is written with no
            # revision, so the receipt's may predate the person's last choice.
            replay = (
                current
                if isinstance(
                    body.operation,
                    Navigate | Highlight | ChatWindow | SetPreferences,
                )
                else CanvasState.model_validate(
                    from_plain(receipt["response"], dict[str, object])
                    | {
                        "name": current.name,
                        "conversation_id": current.conversation_id,
                        "page": current.page,
                        "layout": current.layout,
                        "hidden": current.hidden,
                        "pinned": current.pinned,
                        "workspace_id": current.workspace_id,
                        "session_id": current.session_id,
                    },
                ).model_copy(
                    update={"provider": current.provider, "opens_as": current.opens_as},
                )
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
            canvases: tuple[uuid.UUID, ...] = ()
            preferences: Preferences | None = None
            if isinstance(body.operation, SetPreferences):
                # The owner's, not the agent's: the canvas's agent works for them,
                # and on the UI's preferences alone.
                beyond = sorted(set(body.operation.change) - ASSISTANT_KEYS)
                if beyond:
                    raise ValueError(
                        f"A canvas's agent may not set: {', '.join(beyond)}",
                    )
                preferences = await write_preferences(
                    conn,
                    user_id=owner_id,
                    change=body.operation.change,
                )
                canvases = tuple(await canvas_ids(conn, user_id=owner_id))
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
                canvas_id=canvas_id,
                key=key,
                request_hash=request_hash,
                response={},
            )
            await trim_operation_receipts(conn, canvas_id=canvas_id)
            return AppliedOperation(
                state=await with_partner(state=shown),
                replayed=False,
                canvases=canvases,
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
        if (
            isinstance(body.operation, ChooseProvider)
            and updated.provider != current.provider
            and current.conversation_id is not None
            and (
                starter := await read_starter(
                    conn,
                    conversation_id=current.conversation_id,
                    assistant=assistant,
                )
            )
            is not None
            and starter.lower() == author.lower()
        ):
            session = await read_first_session(
                conn,
                conversation_id=current.conversation_id,
                assistant=assistant,
            )
            if session is not None:
                # Resume the session the loader attaches to, preserving its parts.
                # The receipt shares this transaction: a replay cannot undo Close.
                await store.reopen_session(session, conn=conn, actor=author)
        await _remember(
            conn,
            canvas_id=canvas_id,
            key=key,
            request_hash=request_hash,
            response=updated.model_dump(mode="json"),
        )
        if updated.revision % 16 == 0:
            # Old retries still carry a stale revision and receive 409 after
            # their receipt expires. Keep the recent 64 exact replay results.
            await trim_operation_receipts(conn, canvas_id=canvas_id)
        return AppliedOperation(
            state=await _with_labels(
                conn,
                state=await with_partner(state=updated),
                reader=reader,
            ),
            replayed=False,
        )


async def trim_operation_receipts(conn: Conn, *, canvas_id: uuid.UUID) -> None:
    """Keep a canvas's 64 newest operation receipts.

    Args:
      conn: Connection inside the operation's transaction.
      canvas_id: The canvas.

    """
    await conn.execute(
        "DELETE FROM visual_canvas_operations WHERE canvas_id = $1 "
        "AND key IN (SELECT key FROM visual_canvas_operations "
        "WHERE canvas_id = $1 ORDER BY created_at DESC, key DESC "
        "OFFSET 64)",
        canvas_id,
    )


def state_from_row(row: Mapping[str, object]) -> CanvasState:
    """Validate stored JSON before returning it as API state.

    Args:
      row: A `visual_canvases` row with the `_CANVAS` columns.

    Returns:
      state: The validated canvas state.

    """
    data = CanvasData.model_validate(row["state"])
    stored_canvas = dict(from_plain(row["metadata"], dict[str, object]))
    stored_canvas.pop("campaign", None)
    canvas = CanvasMetadata.model_validate(stored_canvas)
    return CanvasState(
        id=cast(uuid.UUID, row["id"]),
        revision=from_plain(row.get("revision"), int),
        name=canvas.name,
        conversation_id=canvas.conversation_id,
        page=canvas.page,
        layout=canvas.layout,
        hidden=canvas.hidden,
        pinned=canvas.pinned,
        workspace_id=canvas.workspace_id,
        suggestion=canvas.suggestion,
        session_id=canvas.session_id,
        visuals=data.visuals,
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
        provider=data.provider,
        marks=data.marks,
        marks_seq=data.marks_seq,
        marks_sent=data.marks_sent,
        opens_as=data.opens_as,
    )


# The chat's queue has one drainer, its starter's canvas's provider, as the drain route
# enforces: a sharer's post by their own canvas's provider would queue where that
# provider does not drain. A new chat, and the starter's own post, go by the canvas.
async def chat_answerer(
    conn: Conn,
    *,
    state: CanvasState,
    conversation_id: uuid.UUID,
    poster: str,
    assistant: Assistant | None,
) -> ChatProvider:
    """Say which provider answers a post into a chat from a canvas.

    The poster's own canvas decides in a chat they started; in one someone else
    started, the starter's canvas does (:func:`read_chat_provider`), since the
    chat's queue has one drainer.

    Args:
      conn: A database connection.
      state: The canvas the post comes from.
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


# Both are None for a fork of a CLI canvas: its conversation is new, so its session is
# opened only once its canvas is made.
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
    # A permission answer runs a tool on the run's machine, and a trust answer trusts
    # a folder there for good, so only the run's owner gives either; a sharer reads and
    # posts but never answers. A chat no session carries has no run and no prompt:
    # ``chat_line_target`` refuses it as offline.
    if body.kind == "answer" and provider != "assistant":
        await _check_starter_answers(
            conn,
            assistant=assistant,
            conversation_id=conversation_id,
            poster=poster,
            words="can answer prompts from their machine",
        )
    if provider != "assistant":
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
    if body.kind in {"answer", "interrupt"}:
        # A card the assistant asks in a chat is answered in that chat's own session:
        # the cards are released when it closes the chat, so no other session has one.
        # A turn runs only in a chat the assistant has open, so it is stopped there too.
        chat = await chat_line_target(
            conn,
            inbound=inbound,
            assistant=assistant,
            provider=provider,
            conversation_id=conversation_id,
        )
        if chat is None:
            raise ProviderOfflineError("The assistant does not have this chat open")
        if body.kind == "answer":
            await _check_asker_answers(
                conn,
                body=body,
                session_id=chat,
                assistant=assistant,
                conversation_id=conversation_id,
                poster=poster,
            )
        return chat, chat
    if body.kind != "line":
        raise ValueError(
            "The assistant takes a person's line, an answer or an interrupt, "
            f"not a {body.kind}",
        )
    partner = await shared_partner(conn, inbound=inbound, assistant=assistant)
    if partner is None or partner.session_id is None or partner.status != "live":
        raise CanvasSessionUnavailableError("The assistant is not running")
    chat = await chat_line_target(
        conn,
        inbound=inbound,
        assistant=assistant,
        provider=provider,
        conversation_id=conversation_id,
    )
    return chat or partner.session_id, chat


# The assistant's card asks the person whose line asked, and lists that person's own
# choices, such as their machines; a click is heard as the clicker's line, so another
# member's pick would act for the wrong person. A card that names nobody is the
# starter's, as a run's is.
async def _check_asker_answers(
    conn: Conn,
    *,
    body: ChatSend,
    session_id: uuid.UUID,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
    poster: str,
) -> None:
    """Refuse an answer to the assistant's card from anyone but the person it asked."""
    answer = ChatAnswer.model_validate(body.payload)
    asker = await asked_of(conn, session_id=session_id, ask_id=answer.prompt_id)
    if not asker:
        await _check_starter_answers(
            conn,
            assistant=assistant,
            conversation_id=conversation_id,
            poster=poster,
            words="can answer this chat's questions",
        )
    elif asker.lower() != poster.lower():
        raise AnswerNotOwnerError(f"Only {asker} can answer this question.")


async def _check_starter_answers(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
    poster: str,
    words: str,
) -> None:
    """Refuse an answer from anyone but the chat's starter, saying who ``words``."""
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if starter is not None and starter.lower() != poster.lower():
        raise AnswerNotOwnerError(f"Only {starter} {words}.")


# ``assistant_prompt`` is the system prompt the poster chose for the assistant, for a
# line to it; ``None`` for a line to a run.
async def _line_context(
    conn: Conn,
    *,
    body: ChatSend,
    state: CanvasState,
    canvas_id: uuid.UUID,
    conversation_id: uuid.UUID,
    reader: SessionReader,
    assistant_prompt: AssistantPrompt | None,
) -> CanvasMessageContext:
    """Describe what the poster saw as they sent a line: page, trail and new marks."""
    screen = await read_screen(
        conn,
        page=body.page,
        trail=body.trail,
        visuals=state.visuals,
    )
    unsent = [m for m in state.marks if m.seq > state.marks_sent][-_MARKS_PER_LINE:]
    return CanvasMessageContext(
        canvas_id=canvas_id,
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
        attachments=body.attachments,
        assistant_prompt=assistant_prompt,
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
    audience: OpensAs,
    assistant: Assistant | None,
) -> uuid.UUID | None:
    """Return the chat's first session, opening one into ``audience`` if it has none."""
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
        audience=audience,
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


async def _chosen_prompt(conn: Conn, *, user_id: uuid.UUID) -> AssistantPrompt:
    """Return the system prompt ``user_id`` chose for the assistant."""
    return stored_preferences(
        await conn.fetchval("SELECT preferences FROM users WHERE id = $1", user_id),
    ).assistant_prompt


async def _bind_chat(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    state: CanvasState,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Give a canvas without a chat the conversation of its first line."""
    if state.conversation_id == conversation_id:
        return state.id
    if state.conversation_id is not None:
        raise CanvasChatError("This canvas's chat is another conversation")
    if await _canvas_of_chat(conn, user_id=user_id, conversation_id=conversation_id):
        raise CanvasChatError("Another canvas of yours holds this chat")
    await conn.execute(
        "UPDATE visual_canvases SET metadata = coalesce(metadata, '{}') || "
        "jsonb_build_object('conversation_id', $2::text), "
        "modified_at = clock_timestamp() WHERE id = $1",
        state.id,
        str(conversation_id),
    )
    return state.id


# The new canvas copies the source's state and page; a retry of the fork finds the
# canvas the first try made.
async def _fork_canvas(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    source: Record,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Return the canvas of a fork's conversation, making it from its source once."""
    made = await _canvas_of_chat(
        conn,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if made is None:
        source_state = state_from_row(cast("Mapping[str, object]", source))
        row = await _new_canvas(
            conn,
            user_id=user_id,
            data=CanvasData.model_validate(source["state"]),
            conversation_id=conversation_id,
            page=source_state.page,
            layout=source_state.layout,
            workspace_id=source_state.workspace_id,
        )
        made = (
            await _canvas_of_chat(
                conn,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if row is None
            else cast(uuid.UUID, row["id"])
        )
    if made is None:
        raise RuntimeError("fork canvas insert was not visible")
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


async def _canvas_of_chat(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the user's canvas that holds a conversation, if they have one."""
    return cast(
        uuid.UUID | None,
        await conn.fetchval(
            "SELECT id FROM visual_canvases "
            "WHERE user_id = $1 AND metadata->>'conversation_id' = $2",
            user_id,
            str(conversation_id),
        ),
    )


async def _chat_canvas_row(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Record | None:
    """Return the row of the user's canvas that holds a conversation, if any."""
    return await conn.fetchrow(
        vetted_sql(
            _SELECT_CANVAS,
            "WHERE user_id = $1 AND metadata->>'conversation_id' = $2",
        ),
        user_id,
        str(conversation_id),
    )


async def _remember(
    conn: Conn,
    *,
    canvas_id: uuid.UUID,
    key: uuid.UUID,
    request_hash: str,
    response: Mapping[str, object],
) -> None:
    """Record an applied operation's result, so a retry replays it."""
    await conn.execute(
        "INSERT INTO visual_canvas_operations "
        "(canvas_id, key, request_hash, response) VALUES ($1, $2, $3, $4)",
        canvas_id,
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


# A browser sees only its own canvases. The shared assistant's key needs its live
# service session and the canvas's science chat, whose owner is a member. An owner's
# key instead needs its own placed session or its starter's live CLI chat on this
# canvas. An unrelated key learns nothing of the canvas; an owner's unrelated
# key is told why it was refused, so a terminal `trax canvas` explains the failure.
async def _canvas_for_principal(
    conn: Conn,
    *,
    canvas_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_api_key_id: uuid.UUID | None,
    inbound: InboundQueue,
    assistant: Assistant | None,
    for_update: bool = False,
) -> tuple[CanvasState, uuid.UUID] | None:
    """Find a canvas the principal may read and operate, with its owner."""
    row = await conn.fetchrow(
        vetted_sql(
            "SELECT user_id, ",
            _CANVAS,
            " FROM visual_canvases WHERE id = $1 AND ($3::boolean OR user_id = $2)",
            " FOR UPDATE" if for_update else "",
        ),
        canvas_id,
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
    if (
        await assistant_key_may_use(
            conn,
            owner_id=owner_id,
            partner=partner,
            conversation_id=state.conversation_id,
            api_key_id=agent_api_key_id,
        )
        or await canvas_session_key_may_use(
            conn,
            owner_id=owner_id,
            canvas_id=canvas_id,
            api_key_id=agent_api_key_id,
        )
        or await _chat_run_key_may_use(
            conn,
            state=state,
            owner_id=owner_id,
            api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
        )
    ):
        return state, owner_id
    if owner_id == user_id:
        raise CanvasKeyRefusedError(
            "Only the canvas's Chat partner or an associated live session "
            "may use it with an API key",
        )
    return None


async def _chat_run_key_may_use(
    conn: Conn,
    *,
    state: CanvasState,
    owner_id: uuid.UUID,
    api_key_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> bool:
    """Allow the chat's starter to use its canvas through any live key of their own."""
    if state.provider == "assistant" or state.conversation_id is None:
        return False
    email = from_plain(
        await conn.fetchval(
            """
            SELECT member.email FROM api_keys AS credential
            JOIN users AS member ON member.id = credential.user_id
            JOIN inquiries AS sess ON sess.agentsession_cli_session_id = $3
            WHERE credential.id = $1 AND credential.user_id = $2
            AND credential.revoked_at IS NULL AND sess.kind = 'AgentSession'
            AND sess.status = 'active' AND sess.agentsession_ended IS NULL
            AND $4 = ANY(sess.labels) LIMIT 1
            """,
            api_key_id,
            owner_id,
            chat_session_id(state.conversation_id),
            SCIENCE_CHAT_LABEL,
        ),
        str,
        default=None,
    )
    if email is None:
        return False
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=state.conversation_id,
    )
    if starter is None or email.lower() != starter.lower():
        return False
    partner = await cli_partner(
        conn,
        inbound=inbound,
        assistant=assistant,
        provider=state.provider,
        conversation_id=state.conversation_id,
    )
    return partner.status == "live"


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
    """Refuse a deleted chat, a fork of no line or a closed chat, a post not its own."""
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
        await _check_poster(
            conn,
            conversation_id=conversation_id,
            email=email,
            assistant=assistant,
            inbound=inbound,
            reader=reader,
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


async def _check_poster(
    conn: Conn,
    *,
    conversation_id: uuid.UUID,
    email: str,
    assistant: Assistant | None,
    inbound: InboundQueue,
    reader: SessionReader,
) -> None:
    """Refuse a poster the chat is not theirs to post into, by :func:`may_post`."""
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


def _data_of(state: CanvasState) -> CanvasData:
    """Return the stored part of a canvas's state."""
    return CanvasData(
        visuals=state.visuals,
        focused_instance=state.focused_instance,
        agent_instructions=state.agent_instructions,
        continuation_record_id=state.continuation_record_id,
        provider=state.provider,
        marks=state.marks,
        marks_seq=state.marks_seq,
        marks_sent=state.marks_sent,
        opens_as=state.opens_as,
    )


async def _with_labels(
    conn: Conn,
    *,
    state: CanvasState,
    reader: SessionReader,
) -> CanvasState:
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
    current: CanvasState,
    data: CanvasData,
) -> CanvasState:
    """Store ``data`` as the canvas's next revision and return the new state."""
    updated = CanvasState(
        id=current.id,
        revision=current.revision + 1,
        name=current.name,
        conversation_id=current.conversation_id,
        page=current.page,
        layout=current.layout,
        hidden=current.hidden,
        pinned=current.pinned,
        workspace_id=current.workspace_id,
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
        opens_as=data.opens_as,
    )
    await conn.execute(
        "UPDATE visual_canvases SET revision = $2, state = $3, "
        "modified_at = clock_timestamp() WHERE id = $1",
        current.id,
        updated.revision,
        data.model_dump(mode="json"),
    )
    return updated


def _unique_canvas_name(name: str, *, names: Iterable[str]) -> str:
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
    """Move a legacy canvas campaign to the session if it has no value yet."""
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
                reason="Move legacy canvas campaign to its chat session.",
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


async def _insert_canvas(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    name: str,
    data: CanvasData,
    conversation_id: uuid.UUID | None = None,
    page: str | None = None,
    layout: CanvasLayout | None = None,
    clear_key: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> Record | None:
    """Insert a canvas, or return None when its name or chat is taken."""
    canvas = CanvasMetadata(
        name=name,
        conversation_id=conversation_id,
        page=page,
        layout=layout,
        workspace_id=workspace_id,
        session_id=session_id,
        clear_key=clear_key,
    )
    row = await conn.fetchrow(
        vetted_sql(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
            _RETURNING_CANVAS,
        ),
        uuid.uuid4(),
        user_id,
        canvas.model_dump(mode="json", exclude_none=True),
        data.model_dump(mode="json"),
    )
    # Nothing lands out of sight: a session that follows its git branch, or anything
    # with no other home, can land in a workspace its owner hid.
    if row is not None and workspace_id is not None:
        await show_workspace(conn, user_id=user_id, workspace_id=workspace_id)
    return row


# A racing create can take the name between the read and the insert, so the name is
# chosen again a few times; a racing bind of the same chat ends the tries at once, and
# the canvas is then None.
async def _new_canvas(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    data: CanvasData,
    conversation_id: uuid.UUID | None = None,
    page: str | None = None,
    layout: CanvasLayout | None = None,
    clear_key: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> Record | None:
    """Insert a canvas under the lowest free unnamed name, or None if its chat is taken."""
    for _ in range(_NAME_ATTEMPTS):
        names = await conn.fetch(
            "SELECT metadata->>'name' AS name FROM visual_canvases "
            "WHERE user_id = $1 AND metadata->>'name' LIKE $2",
            user_id,
            f"{UNNAMED_PREFIX}%",
        )
        row = await _insert_canvas(
            conn,
            user_id=user_id,
            name=next_canvas_name(from_plain(r["name"], str) for r in names),
            data=data,
            conversation_id=conversation_id,
            page=page,
            layout=layout,
            clear_key=clear_key,
            workspace_id=workspace_id,
        )
        if row is not None:
            return row
        if conversation_id is not None and await _canvas_of_chat(
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
) -> CanvasState | None:
    """Return a canvas row as state with its partner, or None for no row."""
    if row is None:
        return None
    return await attach_partner(
        conn,
        state=state_from_row(cast("Mapping[str, object]", row)),
        owner_id=user_id,
        inbound=inbound,
        assistant=assistant,
    )


# The assistant keys a writer's box by the workspace a chat's settings name, so a chat's
# settings and an upload ask name the same box only while both resolve it here.
async def _workspace_or_default(
    store: Store,
    *,
    user_id: uuid.UUID,
    email: str,
    workspace_id: uuid.UUID | None,
) -> uuid.UUID:
    """Return the canvas's workspace if the user owns it, else their default."""
    # The assistant keys the user's box by this workspace. A canvas placed before
    # placement checked ownership can hold another person's, whose box the user's
    # line, or the upload links signed for it, would then reach.
    if workspace_id is not None and await check_workspace(
        store.engine,
        email=email,
        workspace_id=workspace_id,
    ):
        return workspace_id
    return await open_default_workspace(
        store.engine,
        store=store,
        user_id=user_id,
        email=email,
    )
