"""Tests for reading the lines a fork opens with and linking it to its original."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from trackinizer.client.chat_forks import (
    holds_records,
    link_fork,
    read_fork_lines,
)
from trackinizer.wire.wire_science_chat import (
    FORK_EDGE_LABEL,
    ChatForkAt,
    fork_point_label,
)
from trackinizer.wire.wire_session_ir import PartBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import Sequence


ORIGINAL = UUID("11111111-1111-1111-1111-111111111111")
FORK = UUID("22222222-2222-2222-2222-222222222222")


def _user(idx: int, text: str, *, sender: str = "ada@example.com") -> RecordBody:
    return RecordBody(
        idx=idx,
        kind="AgentToAgentMessage",
        payload={"sender": sender, "content": text},
    )


def _answer(idx: int, text: str) -> RecordBody:
    return RecordBody(idx=idx, kind="AssistantMessage", payload={"content": text})


def _call(idx: int) -> RecordBody:
    return RecordBody(
        idx=idx,
        kind="AssistantMessage",
        payload={"content": "", "tool_calls": [{"name": "Read"}]},
    )


@dataclass(slots=True, kw_only=True)
class _Session:
    """An original with two parts; the second one's records follow the first's."""

    parts: dict[int, list[RecordBody]] = field(
        default_factory=lambda: {
            0: [_user(0, "q1"), _call(1), _answer(2, "a1")],
            1: [_user(0, "q2", sender="grace@example.com"), _answer(1, "a2")],
        },
    )
    edges: list[tuple[UUID, UUID, str, str, str, list[str]]] = field(
        default_factory=list,
    )

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        assert session_id == ORIGINAL
        return [
            PartBody(
                part=number,
                name=f"{number}.jsonl",
                format="sagent",
                records=len(each),
            )
            for number, each in reversed(self.parts.items())
        ]

    def read_session_records(
        self,
        session_id: UUID,
        *,
        part: int,
        after_idx: int,
        plaintext_only: bool,
    ) -> list[RecordBody]:
        assert (session_id, after_idx, plaintext_only) == (ORIGINAL, -1, True)
        return self.parts[part]

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
        self.edges.append((from_id, to_id, edge_kind, actor, note, list(labels or ())))
        return None


def _at(part: int, idx: int) -> ChatForkAt:
    return ChatForkAt(session_id=ORIGINAL, part=part, idx=idx)


def _read(part: int, idx: int) -> list[tuple[str, str]]:
    return [
        (line.author, line.text)
        for line in read_fork_lines(_Session(), fork=_at(part, idx))
    ]


def test_a_session_holds_records_when_any_part_does() -> None:
    assert holds_records(_Session(), session=ORIGINAL)
    assert holds_records(_Session(parts={0: [], 1: [_user(0, "q")]}), session=ORIGINAL)
    assert not holds_records(_Session(parts={0: []}), session=ORIGINAL)
    assert not holds_records(_Session(parts={}), session=ORIGINAL)


def test_a_fork_opens_with_the_lines_up_to_and_including_its_fork_point() -> None:
    # Tool calls are no lines, parts are read in order wherever they are listed, and
    # the line the fork starts from is the last it holds.
    assert _read(0, 0) == [("ada@example.com", "q1")]
    assert _read(0, 2) == [("ada@example.com", "q1"), ("", "a1")]
    assert _read(1, 0) == [
        ("ada@example.com", "q1"),
        ("", "a1"),
        ("grace@example.com", "q2"),
    ]
    assert _read(1, 1)[-1] == ("", "a2")


def test_the_fork_is_linked_to_its_original_with_the_fork_point() -> None:
    session = _Session()

    link_fork(session, session=FORK, fork=_at(1, 0), actor="assistant")

    assert session.edges == [
        (
            FORK,
            ORIGINAL,
            "produced_by",
            "assistant",
            "Forked after record 0 of part 1",
            [FORK_EDGE_LABEL, fork_point_label(part=1, idx=0)],
        ),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
