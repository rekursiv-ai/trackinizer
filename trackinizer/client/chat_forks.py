"""A fork of a science chat, read as a client reads one.

A fork is a science chat of its own that opens with the original's lines, up to and
including the one it forks from (see :mod:`trackinizer.wire.wire_science_chat`).
The server makes it whole when the fork is posted, whoever serves it: it opens the
fork's session holding copies of the original's records up to the fork point, and
links the fork to the original. Nothing that serves a fork seeds it. These helpers read
an original's lines up to a fork point as a client reads them, from the original's
records under the reader's own key, say whether a session holds any record yet, and
add a fork's edge as the server adds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from treekle import from_plain

from trackinizer.client.conversation import delivered_text, delivery_of
from trackinizer.wire.wire_science_chat import FORK_EDGE_LABEL, fork_point_label


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from trackinizer.wire.wire_science_chat import ChatForkAt
    from trackinizer.wire.wire_session_ir import PartBody, RecordBody


__all__ = [
    "EdgeWriter",
    "ForkLine",
    "ForkReader",
    "holds_records",
    "link_fork",
    "read_fork_lines",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ForkLine:
    """One line of the original that the fork opens with."""

    role: Literal["user", "assistant"]
    author: str
    """The poster's attested email for a person's line; empty for an answer."""

    text: str
    created: datetime | None
    """When the original recorded it, on the clock of the one who wrote it."""


class ForkReader(Protocol):
    """The slice of the trackinizer ``Client`` that reads an original's lines."""

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        """List the files a session was captured from, in part order."""
        ...

    def read_session_records(
        self,
        session_id: UUID,
        *,
        part: int,
        after_idx: int,
        plaintext_only: bool,
    ) -> list[RecordBody]:
        """Read one part's records from just after an index."""
        ...


class EdgeWriter(Protocol):
    """The slice of the trackinizer ``Client`` that links a fork to its original."""

    def add_edge(
        self,
        from_id: UUID,
        to_id: UUID,
        edge_kind: str,
        *,
        actor: str,
        note: str = "",
        labels: Sequence[str] | None = (),
    ) -> object:
        """Add an edge, which is no error on repeat."""
        ...


def read_fork_lines(client: ForkReader, *, fork: ChatForkAt) -> list[ForkLine]:
    """Read the original's lines up to and including the one the fork starts from.

    Args:
      client: The trackinizer client of the one serving the fork.
      fork: The original's session, and the part and position of the last line.

    Returns:
      lines: A person's lines and the answers with words, oldest first; tool calls
        and their results are not lines.

    """
    lines: list[ForkLine] = []
    for part in sorted(client.read_session_parts(fork.session_id), key=_part_number):
        if part.part < 0 or part.part > fork.part:
            continue
        for record in client.read_session_records(
            fork.session_id,
            part=part.part,
            after_idx=-1,
            plaintext_only=True,
        ):
            if part.part == fork.part and record.idx > fork.idx:
                break
            line = _line_of(record)
            if line is not None:
                lines.append(line)
    return lines


def holds_records(client: ForkReader, *, session: UUID) -> bool:
    """Say whether a session already holds any record.

    A fork is opened with lines only in a session nothing was recorded in. One that
    holds records is a conversation that was already going, and gaining another
    chat's lines would rewrite it.

    Args:
      client: The trackinizer client of the one serving the fork.
      session: The session the fork would open.

    Returns:
      held: True when any of its parts holds a record.

    """
    return any(part.records for part in client.read_session_parts(session))


def link_fork(
    client: EdgeWriter,
    *,
    session: UUID,
    fork: ChatForkAt,
    actor: str,
) -> None:
    """Link a fork's session to the session it was forked from.

    The edge is ``produced_by`` (fork to original): trackinizer has no fork edge, and
    this is the one that reads as "this session came out of that one". Its labels say
    that it is a fork and which record it starts after.

    Args:
      client: The trackinizer client of the one serving the fork.
      session: The fork's session.
      fork: The original's session, and the part and position of the last line.
      actor: Who the edge is attributed to.

    """
    _ = client.add_edge(
        session,
        fork.session_id,
        "produced_by",
        actor=actor,
        note=f"Forked after record {fork.idx} of part {fork.part}",
        labels=[FORK_EDGE_LABEL, fork_point_label(part=fork.part, idx=fork.idx)],
    )


def _part_number(part: PartBody) -> int:
    return part.part


# A person's line is the bot's own AgentToAgentMessage, or a UserMessage a CLI part
# holds that bot delivered or that names its author, the rule the server's poster
# and Chat read too. A typed line with neither names no one, so it is not a line
# of the fork.
def _line_of(record: RecordBody) -> ForkLine | None:
    """Read a record as a line: a person's, an answer with words, or neither."""
    content = from_plain(record.payload.get("content"), str, default="")
    if record.kind == "AgentToAgentMessage":
        sender = from_plain(record.payload.get("sender"), str, default="unknown")
        return ForkLine(
            role="user",
            author=sender,
            text=content,
            created=record.timestamp,
        )
    if record.kind == "UserMessage":
        extra = from_plain(record.payload.get("extra"), dict[str, object], default={})
        delivery = delivery_of(extra)
        if delivery is None:
            author = from_plain(extra.get("author"), str, default="")
            if not author or not content:
                return None
            return ForkLine(
                role="user",
                author=author,
                text=content,
                created=record.timestamp,
            )
        return ForkLine(
            role="user",
            author=delivery.source,
            text=delivered_text(content, delivery),
            created=record.timestamp,
        )
    if record.kind == "AssistantMessage" and content:
        return ForkLine(
            role="assistant",
            author="",
            text=content,
            created=record.timestamp,
        )
    return None
