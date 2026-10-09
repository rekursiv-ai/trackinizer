"""The sessions ``trax helper`` records its science chats in.

A science chat is an AgentSession whose ``cli_session_id`` is ``chat:<conversation
id>`` and whose labels hold ``science-chat`` and ``poster:<email>`` for each person
who posted (see :mod:`trackinizer.wire.wire_science_chat`). Its records are the
conversation: a person's line is an ``AgentToAgentMessage`` from its poster, and an
answer is an ``AssistantMessage``. This is what a hosted assistant records through its
session bridge; a helper has no transcript to follow, so it appends each record
itself, one batch at a time, to a file name that no earlier process used (the server
gives each name its own part, and keys a part's records by their position in it).

A session is opened by the key that records in it, and only that key may write to
it, so a restart resumes the sessions it opened and no one else's.

A fork opens with copies of the lines of another chat up to the one it starts from, and
is linked to that chat (see :mod:`trackinizer.client.chat_forks`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

import logging

from trackinizer.client.chat_forks import (
    holds_records,
    link_fork,
    read_fork_lines,
)
from trackinizer.client.errors import ClientError
from trackinizer.lib.agent.types.sessions import AgentToAgentMessage, AssistantMessage
from trackinizer.lib.codec import from_plain
from trackinizer.trax.run.redact import redact_body
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    SCIENCE_CHAT_LABEL,
    chat_actor,
    chat_session_id,
    poster_label,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import SessionEnd, SessionStart


if TYPE_CHECKING:
    from trackinizer.client.chat_forks import ForkLine
    from trackinizer.client.client import Client
    from trackinizer.lib.codec import PlainTree
    from trackinizer.trax.run.redact import Redactor
    from trackinizer.wire.wire_science_chat import ChatForkAt


__all__ = ["ChatSessions", "Owed"]


_logger = logging.getLogger(__name__)


_TITLE_CHARS: Final = 120
"""The longest title a science chat's session is opened with."""

_OWED_WINDOW: Final = timedelta(days=7)
"""How far back a restart looks for a line left unanswered."""

_OWED_PAGE: Final = 100
"""How many chats one listing page holds."""

_TAIL_WINDOW: Final = 64
"""How many records of a part are read first to find a conversation's last line."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Owed:
    """A conversation whose last line is a person's, and has no answer after it."""

    conversation_id: UUID
    author: str
    """The poster of that line."""

    text: str


@dataclass(slots=True, kw_only=True)
class _Chat:
    """A conversation's session as this process writes it."""

    session: UUID
    part: str = field(default_factory=lambda: f"{uuid4().hex}.jsonl")
    """The file name this process records under, new to the session."""

    ir_id: UUID = field(default_factory=uuid4)
    written: int = 0
    labelled: set[str] = field(default_factory=set[str])


@dataclass(frozen=True, slots=True, kw_only=True)
class _Line:
    """The last line of a stored conversation."""

    role: Literal["user", "assistant"]
    author: str
    text: str


class ChatSessions:
    """Records each science chat a helper serves as the records of its own session.

    Args:
      client: The Trackinizer client, as the account that runs the helper.
      actor: Who the labels written on a session are attributed to.
      redactor: Masks secret values in every record before it is uploaded; ``None``
        uploads records as they are.

    """

    def __init__(
        self,
        client: Client,
        *,
        actor: str,
        redactor: Redactor | None = None,
    ) -> None:
        self._client = client
        self._actor = actor
        self._redactor = redactor
        self._chats: dict[UUID, _Chat] = {}
        self._unseeded: dict[UUID, ChatForkAt] = {}

    def hear(self, conversation_id: UUID, *, poster: str | None, text: str) -> None:
        """Record a person's line in its conversation's session.

        The first line opens the session, with its poster as the account and the
        line as the title; a session an earlier process opened is resumed. The
        poster is labelled on it the first time they are heard.

        Args:
          conversation_id: The conversation the line was posted to.
          poster: The server-attested email of the person who posted, if known.
          text: The line.

        """
        chat = self._open(conversation_id, poster=poster, title=text)
        self._append(
            chat,
            AgentToAgentMessage(
                sender=poster or "unknown",
                content=text,
                timestamp=_now(),
            ),
        )

    def fork(
        self,
        conversation_id: UUID,
        *,
        fork: ChatForkAt | None,
        poster: str | None,
        title: str,
    ) -> list[ForkLine]:
        """Open a conversation as a fork: its session holds the original's lines.

        The original's lines are read first, so a fork that cannot be read leaves no
        session behind, and its fork point is kept for the conversation's next line
        (pass ``fork=None`` for it). The session is then opened as :meth:`hear` would
        open it, the lines up to the fork point are recorded in it as they were, and
        it is linked to the original. A conversation that already has lines is never
        given a fork point: nothing is recorded or linked, and no lines are returned.
        Call it before :meth:`hear` records the fork's first line.

        Args:
          conversation_id: The new conversation.
          fork: The original's session and the last line the fork holds; ``None``
            to open with the fork point an earlier call could not read.
          poster: The server-attested email of the person who forked.
          title: The line they posted, which names the session.

        Returns:
          lines: The original's lines the fork opened with, for the model to read.

        Raises:
          ClientError: The original cannot be read or the fork cannot be recorded.

        """
        fork = fork or self._unseeded.get(conversation_id)
        if fork is None:
            return []
        known = self._chats.get(conversation_id)
        if known is not None and known.written:
            _logger.warning(
                "trax helper: chat %s ignored the fork point of a line",
                conversation_id,
            )
            _ = self._unseeded.pop(conversation_id, None)
            return []
        self._unseeded[conversation_id] = fork
        lines = read_fork_lines(self._client, fork=fork)
        chat = self._open(conversation_id, poster=poster, title=title)
        if holds_records(self._client, session=chat.session):
            _logger.warning(
                "trax helper: chat %s ignored the fork point of a line",
                conversation_id,
            )
            _ = self._unseeded.pop(conversation_id, None)
            return []
        link_fork(self._client, session=chat.session, fork=fork, actor=self._actor)
        for line in lines:
            when = (line.created or datetime.now(UTC)).isoformat()
            self._append(
                chat,
                AgentToAgentMessage(
                    sender=line.author,
                    content=line.text,
                    timestamp=when,
                )
                if line.role == "user"
                else AssistantMessage(content=line.text, timestamp=when),
            )
        _ = self._unseeded.pop(conversation_id, None)
        return lines

    def answer(self, conversation_id: UUID, *, text: str) -> None:
        """Record the helper's answer in its conversation's session.

        Args:
          conversation_id: The conversation answered.
          text: The answer.

        """
        chat = self._open(conversation_id, poster=None, title="")
        self._append(chat, AssistantMessage(content=text, timestamp=_now()))

    def owed(self) -> list[Owed]:
        """List the conversations whose last line is a person's, oldest listed first.

        A line taken off the server's queue before a restart is queued nowhere any
        more, but its session still ends in it. Only the sessions this process's own
        key opened are looked at: only that key can write a science chat and so name
        a poster, and a session another account opened under a ``chat:`` id is
        nobody's chat. A listing or a chat the server fails to give is logged and
        owes nothing: the line stays last in its session, and the next start finds
        it.

        Returns:
          owed: One entry per conversation to answer.

        """
        try:
            rows = self._recent()
        except ClientError:
            _logger.warning("trax helper: could not list its chats", exc_info=True)
            return []
        owed: list[Owed] = []
        for row in rows:
            conversation = _conversation_of(row)
            if conversation is None:
                continue
            try:
                last = self._last_line(UUID(from_plain(row.get("id"), str)))
            except ClientError:
                _logger.warning(
                    "trax helper: could not read chat %s",
                    conversation,
                    exc_info=True,
                )
                continue
            if last is not None and last.role == "user":
                owed.append(
                    Owed(
                        conversation_id=conversation,
                        author=last.author,
                        text=last.text,
                    ),
                )
        return owed

    def close(self) -> None:
        """End every session this process opened or resumed; a failure is logged."""
        for conversation_id, chat in self._chats.items():
            try:
                _ = self._client.session_end(
                    chat.session,
                    SessionEnd(ended=datetime.now(UTC)),
                )
            except ClientError:
                _logger.warning(
                    "trax helper: could not end chat %s",
                    conversation_id,
                    exc_info=True,
                )
        self._chats.clear()

    # A label that cannot be added is tried again with the next record: History lists a
    # chat by its labels, but the line is not worth losing for it.
    def _open(self, conversation_id: UUID, *, poster: str | None, title: str) -> _Chat:
        """Return the conversation's session, opening or resuming it first."""
        chat = self._chats.get(conversation_id)
        if chat is None:
            masked = " ".join(title.split())
            started = self._client.session_start(
                SessionStart(
                    cli="sagent",
                    cli_session_id=chat_session_id(conversation_id),
                    actor=chat_actor(conversation_id),
                    title=_clip(
                        masked
                        if self._redactor is None
                        else self._redactor.redact(masked),
                        limit=_TITLE_CHARS,
                    )
                    or None,
                    account=poster,
                    started=datetime.now(UTC),
                ),
            )
            chat = _Chat(session=started.id)
            self._chats[conversation_id] = chat
        for label in (
            SCIENCE_CHAT_LABEL,
            *([] if poster is None else [poster_label(poster)]),
        ):
            if label in chat.labelled:
                continue
            try:
                self._client.add_label(chat.session, label, actor=self._actor)
            except ClientError:
                _logger.warning(
                    "trax helper: could not label chat %s %s",
                    conversation_id,
                    label,
                    exc_info=True,
                )
                continue
            chat.labelled.add(label)
        return chat

    def _append(
        self,
        chat: _Chat,
        record: AgentToAgentMessage | AssistantMessage,
    ) -> None:
        """Append one record at the next position of this process's part."""
        body = redact_body(
            RecordBody.of(
                SessionRecordRow.of(
                    session_id=UUID(int=0),
                    part=0,
                    idx=chat.written,
                    record=record,
                ),
            ),
            redactor=self._redactor,
        )
        _ = self._client.append_records(
            chat.session,
            name=chat.part,
            manifest=ManifestBody(
                name=chat.part,
                ir_id=chat.ir_id,
                format="sagent",
                records=chat.written + 1,
            ),
            records=[body],
        )
        chat.written += 1

    def _recent(self) -> list[dict[str, PlainTree]]:
        """List the recent science chats this key opened, every page."""
        profile = from_plain(self._client.get("/api/me/profile"), dict[str, object])
        opener = from_plain(profile.get("api_key_id"), str, default="")
        filters = [
            Filter(field="cli_session_id", op="re", value=f"^{CHAT_SESSION_PREFIX}"),
            Filter(field="labels", op="is", value=SCIENCE_CHAT_LABEL),
            Filter(
                field="modified",
                op="ge",
                value=str(datetime.now(UTC) - _OWED_WINDOW),
            ),
            # A Bearer key always names itself here, so only a server that checks no
            # key (``--no-auth``) names none. It stamps no opener on any session and
            # has one tenant, and then every session is this one's.
            *(
                [Filter(field="opened_by_api_key_id", op="is", value=opener)]
                if opener
                else []
            ),
        ]
        rows: list[dict[str, PlainTree]] = []
        while True:
            page = self._client.list_kind(
                "AgentSession",
                limit=_OWED_PAGE,
                offset=len(rows),
                filters=filters,
            )
            rows.extend(page)
            if len(page) < _OWED_PAGE:
                return rows

    def _last_line(self, session: UUID) -> _Line | None:
        """Read the last line of a conversation: a person's, or an answer."""
        for part in reversed(self._client.read_session_parts(session)):
            tail = max(-1, part.records - 1 - _TAIL_WINDOW)
            for after in dict.fromkeys((tail, -1)):
                lines = _lines_of(
                    self._client.read_session_records(
                        session,
                        part=part.part,
                        after_idx=after,
                        plaintext_only=True,
                    ),
                )
                if lines:
                    return lines[-1]
        return None


def _lines_of(records: list[RecordBody]) -> list[_Line]:
    """Read the lines out of a session's records: people's lines and answers."""
    lines: list[_Line] = []
    for record in records:
        content = from_plain(record.payload.get("content"), str, default="")
        if record.kind == "AgentToAgentMessage":
            sender = from_plain(record.payload.get("sender"), str, default="unknown")
            lines.append(_Line(role="user", author=sender, text=content))
        elif record.kind == "AssistantMessage" and content:
            lines.append(_Line(role="assistant", author="", text=content))
    return lines


def _conversation_of(row: dict[str, PlainTree]) -> UUID | None:
    """Read the conversation id out of a listed session's ``cli_session_id``."""
    named = from_plain(row.get("cli_session_id"), str, default="")
    try:
        return UUID(named.removeprefix(CHAT_SESSION_PREFIX))
    except ValueError:
        return None


def _clip(text: str, *, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, ending in an ellipsis."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _now() -> str:
    return datetime.now(UTC).isoformat()
