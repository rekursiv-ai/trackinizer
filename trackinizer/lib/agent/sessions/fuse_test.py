"""Tests for joining session files into one conversation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from io import StringIO
from pathlib import Path

import pytest

from trackinizer.lib.agent.sessions import claude, codex, fuse
from trackinizer.lib.agent.sessions.convert import _Adapter
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    SessionRecord,
    TurnContext,
    UserMessage,
)
from trackinizer.lib.custom_json import DictCodec, StrCodec


_TESTDATA = Path(__file__).resolve().parent / "testdata"


def _unfused(records: Iterable[SessionRecord]) -> list[list[SessionRecord]]:
    """Every part of a fused stream, drained in order.

    ``unfuse`` yields one ITERATOR per part and they are consumed in order, so
    a test that wants them all materializes each as it arrives.
    """
    return [list(part) for part in fuse.unfuse(records)]


def _part(name: str, *records: SessionRecord) -> list[SessionRecord]:
    """A record stream whose opening settings declare it as ``name``."""
    return [TurnContext(extra={"payload": {"id": name}}), *records]


def test_fusing_nothing_yields_no_records() -> None:
    assert list(fuse.fuse(())) == []


def test_one_part_fuses_to_itself() -> None:
    # No seam, because nothing was joined: a boundary here would claim the
    # conversation resumed something.
    part = _part("a", UserMessage(content="Hi."))

    assert list(fuse.fuse([part])) == part


def test_a_seam_opens_a_window_between_the_parts() -> None:
    first = _part("a", UserMessage(content="Hi."))
    second = _part("b", UserMessage(content="Still here?"))

    joined = list(fuse.fuse([first, second]))

    boundary = joined[2]
    assert isinstance(boundary, ContextClear)
    assert [type(r).__name__ for r in joined] == [
        "TurnContext",
        "UserMessage",
        "ContextClear",
        "TurnContext",
        "UserMessage",
    ]


def test_a_part_carrying_a_summary_states_it_on_the_seam() -> None:
    # What a boundary carries is whatever crossed it: claude writes the earlier
    # conversation's summary as the first turn of the file that continues it,
    # and that summary IS what crossed.
    first = _part("a", UserMessage(content="Hi."))
    second = _part(
        "b",
        UserMessage(content="Summary: we said hi.", extra={"isCompactSummary": True}),
    )

    boundary = list(fuse.fuse([first, second]))[2]

    assert isinstance(boundary, ContextClear)
    assert boundary.summary == "Summary: we said hi."


def test_a_seam_does_not_duplicate_the_part_it_introduced() -> None:
    """The boundary names the seam; the part keeps its OWN opening records.

    Storing the resumed part's context on the boundary as well wrote every
    carried record twice on the way back -- 11 records returned as 12.
    """
    first = _part("a", UserMessage(content="Hi."))
    second = _part(
        "b",
        UserMessage(content="Summary: we said hi.", extra={"isCompactSummary": True}),
        UserMessage(content="carried on"),
    )

    back = _unfused(fuse.fuse([first, second]))

    assert back == [first, second]


def test_unfusing_gives_back_the_parts() -> None:
    parts = [
        _part("a", UserMessage(content="Hi.")),
        _part("b", AssistantMessage(content="Still here.")),
    ]

    back = _unfused(fuse.fuse(parts))

    assert back == parts


def test_unfusing_restores_each_part_s_own_declaration() -> None:
    # A part's launch settings are the file's, not the fused stream's, so the
    # part must come back carrying them or the second file cannot be written.
    parts = [_part("a", UserMessage(content="Hi.")), _part("b", UserMessage())]

    back = _unfused(fuse.fuse(parts))

    assert [part[0] for part in back] == [part[0] for part in parts]


def test_the_root_s_file_name_survives_a_fuse() -> None:
    # There is no seam before the first part, so its name rides the settings it
    # opens with -- and unfusing must take it back off, or the part no longer
    # equals the records it was read from.
    parts = [_part("a", UserMessage(content="Hi.")), _part("b", UserMessage())]

    joined = list(fuse.fuse(parts, ["a.jsonl", "b.jsonl"]))

    assert fuse.names_of(joined) == ["a.jsonl", "b.jsonl"]
    assert _unfused(joined) == parts


def test_a_compaction_inside_one_part_is_not_a_seam() -> None:
    # Codex compacts in place, so a part may already hold one. Splitting there
    # would invent a file that never existed.
    part = _part(
        "a",
        UserMessage(content="Hi."),
        ContextCompaction(summary="earlier"),
        ContextClear(summary="earlier"),
        UserMessage(content="Now."),
    )

    assert len(_unfused(fuse.fuse([part]))) == 1


@pytest.mark.parametrize(
    ("adapter", "fixtures"),
    [
        (claude, ("claude_main.jsonl", "claude_sidechain.jsonl")),
        (codex, ("codex_main.jsonl",)),
    ],
    ids=["claude", "codex"],
)
def test_captured_sessions_survive_a_fuse_and_unfuse(
    adapter: _Adapter, fixtures: tuple[str, ...]
) -> None:
    # The invariant the whole module rests on: joining is a view, not a merge,
    # so every part comes back as the bytes it was read from.
    native = [(_TESTDATA / name).read_text(encoding="utf-8") for name in fixtures]
    parts = [list(adapter.normalize(StringIO(text))) for text in native]

    back = _unfused(fuse.fuse(parts))

    rebuilt: list[str] = []
    for part in back:
        out = StringIO()
        adapter.denormalize(part, out)
        rebuilt.append(out.getvalue())
    assert rebuilt == native


def test_chain_keeps_every_part_that_forked_from_one_thread() -> None:
    # A parent may be resumed more than once -- a session forked twice, or two
    # rollouts naming the same thread. Keeping one successor per parent DROPPED
    # the rest: 16 of 392 rollouts on one captured day, 84 MB, and a fused
    # session that silently rebuilt short.
    root = _part("a", UserMessage(content="Hi."))
    forks = [
        [TurnContext(extra={"payload": {"id": name, "forked_from_id": "a"}})]
        for name in ("b", "c", "d")
    ]

    ordered = fuse.chain([root, *forks])

    assert len(ordered) == 4, "every part must survive the ordering"
    named = [_declared_id(part) for part in ordered]
    assert named[0] == "a"
    assert sorted(named[1:]) == ["b", "c", "d"]


def test_chain_orders_parts_by_the_thread_each_forked_from() -> None:
    # Codex names it, so the order is the provider's rather than a guess from
    # timestamps -- which a subagent writing concurrently would break.
    first = _part("a", UserMessage(content="Hi."))
    second = [TurnContext(extra={"payload": {"id": "b", "forked_from_id": "a"}})]
    third = [TurnContext(extra={"payload": {"id": "c", "forked_from_id": "b"}})]

    ordered = fuse.chain([third, first, second])

    assert [_declared_id(part) for part in ordered] == ["a", "b", "c"]


def _declared_id(part: Sequence[SessionRecord]) -> str:
    """The thread id a part's launch settings name."""
    opening = part[0]
    assert isinstance(opening, TurnContext)
    return StrCodec.coerce(DictCodec.coerce(opening.extra.get("payload")).get("id"))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
