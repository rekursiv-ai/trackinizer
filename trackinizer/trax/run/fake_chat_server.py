"""A server for ``trax helper`` tests: the calls it makes, answered in memory.

It keeps what the helper's science chats need of Trackinizer: sessions opened and
resumed by ``cli_session_id``, their labels, accounts and records, the service
session's queue, and the listings a restart reads. No network, no model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import threading

from trackinizer.lib.codec import PlainTree, from_plain
from trackinizer.wire.wire_science_chat import CHAT_HELPER_CLI
from trackinizer.wire.wire_session_ir import ManifestBody, PartBody, RecordBody
from trackinizer.wire.wire_sessions import (
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
    WorkspaceMessageContext,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.wire.filters import Filter


__all__ = ["FakeChatServer", "Held", "Queued"]


type Queued = tuple[str, str | None, str | None, WorkspaceMessageContext | None]
"""One line as the drain hands it over: text, sender, room and canvas context."""


@dataclass(slots=True, kw_only=True)
class Held:
    """One session the server holds."""

    id: UUID
    start: SessionStart
    labels: list[str] = field(default_factory=list[str])
    parts: dict[str, list[RecordBody]] = field(
        default_factory=dict[str, list[RecordBody]],
    )
    ended: int = 0
    """How many times it was ended."""


class FakeChatServer:
    """The calls ``trax helper`` makes, answered from what a test gives it.

    Args:
      batches: What each drain of the service session returns, in turn; once they
        are all taken the next drain sets :attr:`stop` and returns nothing.
      key: The id of the key the helper runs under, as ``/api/me/profile`` says; none
        for a server that does not say.

    """

    def __init__(
        self,
        *,
        batches: list[list[Queued]] | None = None,
        key: str | None = "k1",
    ) -> None:
        self.batches = batches or []
        self.key = key
        self.stop = threading.Event()
        self.sessions: dict[UUID, Held] = {}
        self.label_actors: list[str] = []
        self.listings: list[list[Filter]] = []
        self.edges: list[tuple[UUID, UUID, str, str, list[str]]] = []
        """Every edge added, as (from, to, kind, actor, labels)."""

    def service(self) -> Held:
        """Return the helper's own newest session: the one the drain is of."""
        return [
            held for held in self.sessions.values() if held.start.cli == CHAT_HELPER_CLI
        ][-1]

    def chat(self, conversation_id: UUID) -> Held:
        """Return the session that holds a conversation."""
        return next(
            held
            for held in self.sessions.values()
            if held.start.cli_session_id == f"chat:{conversation_id}"
        )

    def records(self, conversation_id: UUID) -> list[RecordBody]:
        """Return a conversation's records in the order they were appended."""
        return [
            record
            for records in self.chat(conversation_id).parts.values()
            for record in records
        ]

    def lines(self, conversation_id: UUID) -> list[tuple[str, str]]:
        """Return a conversation as (speaker, text): a poster's email or ``answer``.

        Args:
          conversation_id: The conversation.

        Returns:
          lines: Each line and answer in the order appended.

        """
        return [
            (
                from_plain(record.payload.get("sender"), str, default="answer"),
                from_plain(record.payload.get("content"), str, default=""),
            )
            for record in self.records(conversation_id)
        ]

    def seed(
        self,
        conversation_id: UUID,
        *,
        records: list[RecordBody],
    ) -> UUID:
        """Leave a science chat behind, as an earlier process does.

        Args:
          conversation_id: The conversation.
          records: What the chat holds, in one file of an earlier process.

        Returns:
          session_id: The chat's session.

        """
        started = self.session_start(
            SessionStart(
                cli="sagent",
                cli_session_id=f"chat:{conversation_id}",
                actor=f"chat-{conversation_id.hex[:12]}",
                started=datetime.now(UTC),
            ),
        )
        held = self.sessions[started.id]
        held.labels.append("science-chat")
        held.parts["earlier.jsonl"] = records
        return started.id

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        """Open a session, or resume the one that has this ``cli_session_id``.

        Args:
          body: The session to open.

        Returns:
          started: The session's id and granted actor.

        """
        if body.cli_session_id is not None:
            for held in self.sessions.values():
                if held.start.cli_session_id == body.cli_session_id:
                    return SessionStartResponse(id=held.id, seq=1, actor=body.actor)
        held = Held(id=uuid4(), start=body)
        self.sessions[held.id] = held
        return SessionStartResponse(id=held.id, seq=0, actor=body.actor)

    def add_label(self, target_id: UUID, label: str, *, actor: str) -> None:
        """Add a label to a session once."""
        self.label_actors.append(actor)
        labels = self.sessions[target_id].labels
        if label not in labels:
            labels.append(label)

    def add_edge(
        self,
        from_id: UUID,
        to_id: UUID,
        edge_kind: str,
        *,
        actor: str,
        note: str = "",
        labels: Sequence[str] | None = (),
    ) -> None:
        """Record an edge between two sessions."""
        del note
        self.edges.append((from_id, to_id, edge_kind, actor, list(labels or ())))

    def append_records(
        self,
        session_id: UUID,
        *,
        name: str,
        manifest: ManifestBody,
        records: list[RecordBody],
    ) -> None:
        """Append records to a file's part, refusing a position already held.

        Args:
          session_id: The session that owns the part.
          name: The file name, which names the part.
          manifest: What the file declares.
          records: The next record of the part.

        """
        part = self.sessions[session_id].parts.setdefault(name, [])
        if manifest.name != name:
            raise ValueError("Expected manifest.name == name.")
        if [record.idx for record in records] != [len(part)]:
            raise ValueError(
                "Expected [record.idx for record in records] == [len(part)].",
            )
        if manifest.records != len(part) + 1:
            raise ValueError("Expected manifest.records == len(part) + 1.")
        part.extend(records)

    def drain_inbound(
        self,
        session_id: UUID,
        *,
        wait_sec: float = 0.0,
    ) -> list[Queued]:
        """Hand over the next batch; an empty queue stops the helper.

        Args:
          session_id: The service session.
          wait_sec: How long a real server would hold the request.

        Returns:
          batch: The lines queued, or none once every batch is taken.

        """
        del wait_sec
        if session_id != self.service().id:
            raise ValueError("Expected session_id == self.service().id.")
        if not self.batches:
            self.stop.set()
            return []
        return self.batches.pop(0)

    def session_end(
        self,
        session_id: UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        """Count an end of a session."""
        del body
        self.sessions[session_id].ended += 1
        return SessionEndResponse(id=session_id, ended=datetime.now(UTC))

    def get(self, path: str) -> PlainTree:
        """Answer the profile read, which names the key the helper runs under.

        Args:
          path: The path read, always ``/api/me/profile``.

        Returns:
          profile: The key's id, or nothing for a server that names none.

        """
        if path != "/api/me/profile":
            raise ValueError('Expected path == "/api/me/profile".')
        profile: dict[str, PlainTree] = {}
        if self.key is not None:
            profile["api_key_id"] = self.key
        return profile

    def list_kind(
        self,
        kind: str,
        *,
        limit: int,
        offset: int,
        filters: list[Filter],
    ) -> list[dict[str, PlainTree]]:
        """List the science chats held, as a page.

        Args:
          kind: The kind listed, always ``AgentSession``.
          limit: The page size.
          offset: Rows to skip.
          filters: The clauses the helper asked for, kept for the test to read.

        Returns:
          rows: One row of an id and a ``cli_session_id`` per chat.

        """
        if kind != "AgentSession":
            raise ValueError('Expected kind == "AgentSession".')
        self.listings.append(filters)
        rows: list[dict[str, PlainTree]] = [
            {"id": str(held.id), "cli_session_id": held.start.cli_session_id}
            for held in self.sessions.values()
            if (held.start.cli_session_id or "").startswith("chat:")
            and "science-chat" in held.labels
        ]
        return rows[offset : offset + limit]

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        """List a session's files as parts, in the order they began.

        Args:
          session_id: The session.

        Returns:
          parts: One part per file.

        """
        return [
            PartBody(part=number, name=name, format="sagent", records=len(records))
            for number, (name, records) in enumerate(
                self.sessions[session_id].parts.items(),
            )
        ]

    def read_session_records(
        self,
        session_id: UUID,
        *,
        part: int,
        after_idx: int,
        plaintext_only: bool,
    ) -> list[RecordBody]:
        """Return a part's records after a position.

        Args:
          session_id: The session.
          part: Which file.
          after_idx: The position to read after.
          plaintext_only: Whether ciphertext is skipped; the helper skips it.

        Returns:
          records: Those after the position, in order.

        """
        if not plaintext_only:
            raise ValueError("Expected plaintext_only.")
        records = list(self.sessions[session_id].parts.values())[part]
        return [record for record in records if record.idx > after_idx]
