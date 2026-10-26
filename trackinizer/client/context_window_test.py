"""The window a switch loads: one cut for every provider, then a per-target budget.

Every part here is read by its own provider's reader from lines shaped like the ones
measured: claude's ``compact_boundary`` and flagged summary, codex's sealed
``compacted`` line, the bot's ``context_splice``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING

import json

from treekle import immutable

import pytest

from trackinizer.client.context_window import (
    CUT_ABOVE,
    Budget,
    Window,
    cli_records,
    cut_text,
    weight,
    window,
)
from trackinizer.client.conversation import Placed, Segment, lines
from trackinizer.lib.agent.sessions import claude, codex, sagent
from trackinizer.sagent.session.types import (
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    ShellCommandResult,
    ToolCall,
    UserMessage,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.sagent.session.types import SessionRecord


_SESSION = "11111111-1111-1111-1111-111111111111"
_SEALED = "gAAAAABpSealedSummaryOfTheEarlierConversation=="


def _at(second: int) -> str:
    return f"2026-10-20T10:00:{second:02d}Z"


def _claude(*lines: dict[str, object]) -> list[SessionRecord]:
    """Read claude transcript lines, each with the envelope claude repeats."""
    envelope = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/w",
        "sessionId": _SESSION,
        "version": "2.1.304",
        "gitBranch": "main",
    }
    text = "".join(
        json.dumps(envelope | line, separators=(",", ":")) + "\n" for line in lines
    )
    return list(claude.normalize(StringIO(text)))


def _claude_user(
    text: str,
    *,
    uuid: str,
    at: int,
    **flags: object,
) -> dict[str, object]:
    return {
        "parentUuid": None,
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": uuid,
        "timestamp": _at(at),
    } | flags


def _claude_answer(text: str, *, uuid: str, at: int) -> dict[str, object]:
    return {
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-opus-5-5",
            "id": f"msg_{uuid}",
            "type": "message",
        },
        "uuid": uuid,
        "timestamp": _at(at),
    }


def _claude_compaction(summary: str, *, at: int) -> list[dict[str, object]]:
    """Return the two lines claude 2.1.304 writes when it compacts."""
    return [
        {
            "parentUuid": None,
            "logicalParentUuid": "before",
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "isMeta": False,
            "level": "info",
            "compactMetadata": {"trigger": "manual", "preTokens": 967_000},
            "uuid": "boundary",
            "timestamp": _at(at),
        },
        _claude_user(
            summary,
            uuid="summary",
            at=at,
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
    ]


def _codex(*items: dict[str, object], forked_from: str = "") -> list[SessionRecord]:
    """Read a codex rollout: its launch line, then ``items``."""
    meta: dict[str, object] = {"id": "s1", "session_id": "s1", "cwd": "/w"}
    if forked_from:
        meta["forked_from_id"] = forked_from
    rows = [{"timestamp": _at(0), "type": "session_meta", "payload": meta}, *items]
    return list(codex.normalize(StringIO("".join(json.dumps(r) + "\n" for r in rows))))


def _codex_turn(role: str, text: str) -> dict[str, object]:
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def _codex_item(role: str, text: str, *, at: int) -> dict[str, object]:
    return {
        "timestamp": _at(at),
        "type": "response_item",
        "payload": _codex_turn(role, text),
    }


def _codex_compacted(*, at: int, message: str = "") -> list[dict[str, object]]:
    """Return a measured-shape compaction: sealed unless ``message`` says otherwise."""
    return [
        {
            "timestamp": _at(at),
            "type": "compacted",
            "payload": {
                "message": message,
                "replacement_history": [
                    _codex_turn("user", "kept: which bucket?"),
                    _codex_turn("developer", "<permissions instructions>"),
                    _codex_turn("assistant", "kept: europe-west4"),
                    {"type": "compaction", "encrypted_content": _SEALED},
                ],
            },
        },
        {
            "timestamp": _at(at),
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        },
    ]


def _sagent(*rows: dict[str, object]) -> list[SessionRecord]:
    return list(sagent.normalize(StringIO("".join(json.dumps(r) + "\n" for r in rows))))


def _epoch(second: int) -> float:
    return datetime(2026, 10, 20, 10, 0, second, tzinfo=UTC).timestamp()


def _sagent_line(
    kind: str,
    text: str,
    *,
    at: int,
    **fields: object,
) -> dict[str, object]:
    line: dict[str, object] = {
        "kind": "history",
        "ref": {"session_id": "agent", "ordinal": at},
        "type": kind,
        "text": text,
        "attachments": [],
        "id": at,
        "parent_id": -1,
        "timestamp": _epoch(at),
        "hidden": False,
    }
    return line | fields


def _sagent_compaction(summary: str) -> dict[str, object]:
    return {
        "kind": "context_splice",
        "ref": {"session_id": "agent", "ordinal": 99},
        "mask": [],
        "insert_after": None,
        "payload": [{"type": "user", "text": summary, "timestamp": 0.0}],
        "strategy": "summary",
        "token_before": 9_000,
        "token_after": 300,
        "fallback_reason": "",
        "preserved_tail_count": 0,
        "paired_externally": [],
    }


def _segment(part: int, fmt: str, records: Sequence[SessionRecord]) -> Segment:
    return Segment(part=part, name=f"{part}.jsonl", format=fmt, records=list(records))


def _texts(seen: Window) -> list[str]:
    """Return every line the window holds, its kept history first."""
    return [
        line.text
        for line in lines(
            [each.record for each in (*seen.history, *seen.records)],
            bot="bot",
        )
    ]


def _users(records: Sequence[SessionRecord]) -> list[str]:
    return [
        record.content or "" for record in records if isinstance(record, UserMessage)
    ]


def _seven() -> int:
    return 7


def _claude_compacting_part(part: int = 0) -> Segment:
    return _segment(
        part,
        "claude",
        _claude(
            _claude_user("F1 is apples", uuid="u1", at=1),
            _claude_answer("noted F1", uuid="a1", at=2),
            *_claude_compaction("Summary: F1 is apples.", at=3),
            _claude_user("F2 is pears", uuid="u2", at=4),
            _claude_answer("noted F2", uuid="a2", at=5),
        ),
    )


class TestTheCut:
    """The last compaction of any part opens the window."""

    def test_a_claude_compaction_opens_the_window_once_and_the_note_names_the_rest(
        self,
    ) -> None:
        part = _claude_compacting_part()
        leader = next(
            idx
            for idx, record in enumerate(part.records)
            if isinstance(record, ContextCompaction)
        )

        seen = window([part])
        loaded = cli_records(seen, "codex", seq=_seven)

        assert _texts(seen) == ["F2 is pears", "noted F2"]
        assert seen.skipped == leader
        [head, *rest] = _users(loaded.records)
        assert head.startswith(
            f"[trax] {leader} earlier records are not loaded ({leader} before the "
            "last compaction, 0 over the size budget). Read them: trax agentsession 7 "
            f"records --to 0:{leader - 1}",
        )
        assert "Summary of the conversation before this (by claude):\n" in head
        assert sum("Summary: F1 is apples." in text for text in (head, *rest)) == 1
        assert not any("F1 is apples" in text for text in rest)

    def test_the_cut_follows_time_order_across_parts(self) -> None:
        bot_early = _sagent(
            _sagent_line("agent_send", "q1 before", at=1, source="ada"),
            _sagent_line("agent_send", "q9 after", at=20, source="ada"),
        )
        compacting = _claude(
            _claude_user("claude before", uuid="u1", at=2),
            *_claude_compaction("Summary: q1 and claude before.", at=5),
            _claude_user("claude after", uuid="u2", at=6),
        )
        bot_late = _sagent(_sagent_line("agent_send", "q10 later", at=30, source="bob"))

        seen = window(
            [
                _segment(0, "sagent", bot_early),
                _segment(1, "claude", compacting),
                _segment(2, "sagent", bot_late),
            ],
        )

        assert seen.opening is not None
        assert seen.opening.provider == "claude"
        assert _texts(seen) == [
            "claude after",
            "q9 after",
            "q10 later",
        ]

    def test_session_opening_clears_and_a_codex_fork_never_cut(self) -> None:
        """A fork's ``cleared_session_id`` would otherwise drop every earlier part."""
        parts = [
            _segment(
                0,
                "sagent",
                _sagent(_sagent_line("agent_send", "q1", at=1, source="ada")),
            ),
            _segment(1, "claude", _claude(_claude_user("q2", uuid="u1", at=2))),
            _segment(
                2,
                "codex",
                _codex(_codex_item("user", "q3", at=3), forked_from="abc"),
            ),
        ]
        cleared = [
            record
            for part in parts
            for record in part.records
            if isinstance(record, ContextClear)
        ]

        seen = window(parts)

        assert any(record.cleared_session_id == "abc" for record in cleared)
        assert (seen.opening, seen.skipped) == (None, 0)
        assert _texts(seen) == ["q1", "q2", "q3"]

    def test_a_claude_clear_starts_the_window_at_its_part(self) -> None:
        before = _claude(_claude_user("F1 is apples", uuid="u1", at=1))
        cleared = _claude(
            _claude_user(
                "<local-command-caveat>Caveat: The messages below were generated by "
                "the user while running local commands.</local-command-caveat>",
                uuid="c1",
                at=5,
                isMeta=True,
            ),
            _claude_user(
                "<command-name>/clear</command-name>\n"
                "<command-message>clear</command-message>\n<command-args></command-args>",
                uuid="c2",
                at=5,
            ),
            _claude_user(
                "<local-command-stdout></local-command-stdout>",
                uuid="c3",
                at=5,
            ),
            _claude_user("fresh start", uuid="c4", at=6),
        )

        seen = window([_segment(0, "claude", before), _segment(1, "claude", cleared)])
        loaded = cli_records(seen, "claude", seq=_seven)

        assert seen.opening is not None
        assert seen.opening.discard
        assert _texts(seen) == ["fresh start"]
        assert _users(loaded.records)[0].startswith(
            f"[trax] {len(before)} earlier records are not loaded ({len(before)} "
            "cleared with /clear, 0 over the size budget).",
        )

    def test_a_clear_after_an_answer_is_no_discard(self) -> None:
        """Only a part BEGUN by ``/clear`` cleared; a later mention is a line."""
        part = _claude(
            _claude_user("hi", uuid="u1", at=1),
            _claude_answer("hello", uuid="a1", at=2),
            _claude_user("<command-name>/clear</command-name>", uuid="u2", at=3),
        )

        assert window([_segment(0, "claude", part)]).opening is None


class TestASealedCodexSummary:
    """Codex's summary is ciphertext: only codex reads it; the kept turns cross."""

    def _parts(self) -> list[Segment]:
        return [
            _segment(
                0,
                "codex",
                _codex(
                    _codex_item("user", "F1 is apples", at=1),
                    _codex_item("assistant", "noted F1", at=2),
                    *_codex_compacted(at=3),
                    _codex_item("user", "F4 is figs", at=4),
                ),
            ),
        ]

    def test_codex_gets_its_compaction_back_natively(self) -> None:
        loaded = cli_records(window(self._parts()), "codex", seq=_seven)

        compaction, clear = loaded.records[:2]
        assert isinstance(compaction, ContextCompaction)
        assert isinstance(clear, ContextClear)
        assert clear.summary == _SEALED
        assert [type(record).__name__ for record in clear.history] == [
            "UserMessage",
            "SystemMessage",
            "AssistantMessage",
        ]
        assert not any("F1 is apples" in text for text in _users(loaded.records))

    def test_another_target_gets_the_kept_turns_and_never_the_ciphertext(self) -> None:
        loaded = cli_records(window(self._parts()), "claude", seq=_seven)

        said = [
            record.content or ""
            for record in loaded.records
            if isinstance(record, UserMessage | AssistantMessage)
        ]
        assert not any("gAAAA" in text for text in said)
        assert any("kept: which bucket?" in text for text in said)
        assert any(
            "codex's summary of the earlier conversation is sealed" in text
            for text in said
        )
        assert not any(isinstance(record, ContextClear) for record in loaded.records)


class TestTheBudget:
    """Newest whole turns within the target's budget, the note counting the rest."""

    def _turns(self, count: int) -> list[Segment]:
        rows: list[dict[str, object]] = []
        for turn in range(count):
            rows.extend(
                [
                    _sagent_line(
                        "agent_send",
                        f"q{turn:02d} " + "x" * 90,
                        at=turn,
                        source="ada",
                    ),
                    _sagent_line(
                        "assistant",
                        f"a{turn:02d} " + "y" * 90,
                        at=turn,
                        tool_calls=[],
                    ),
                ],
            )
        return [_segment(0, "sagent", _sagent(*rows))]

    def test_the_newest_whole_turns_that_fit_are_kept(self) -> None:
        parts = self._turns(40)
        seen = window(parts)
        turn = sum(weight(each.record) for each in seen.records[:2])

        loaded = cli_records(
            seen,
            "codex",
            seq=_seven,
            budget=Budget(fit_bytes=turn * 3, max_bytes=turn * 3),
        )

        people = [text for text in _users(loaded.records) if text.startswith("ada: ")]
        assert [text[5:8] for text in people] == ["q37", "q38", "q39"]
        answers = [
            record.content or ""
            for record in loaded.records
            if isinstance(record, AssistantMessage)
            and (record.content or "").startswith("a")
        ]
        assert [text[:3] for text in answers] == ["a37", "a38", "a39"]
        assert loaded.skipped == 74
        assert (
            "74 earlier records are not loaded (0 before the last compaction, 74 over"
            in (_users(loaded.records)[0])
        )
        assert loaded.compact_after_load

    def test_the_newest_turn_is_kept_even_when_it_alone_is_over(self) -> None:
        loaded = cli_records(
            window(self._turns(3)),
            "codex",
            seq=_seven,
            budget=Budget(fit_bytes=10, max_bytes=10),
        )

        assert any("q02" in text for text in _users(loaded.records))
        assert any(
            (record.content or "").startswith("a02")
            for record in loaded.records
            if isinstance(record, AssistantMessage)
        )
        assert loaded.skipped == 4

    def test_an_oversized_newest_turn_keeps_its_line_answer_and_newest_calls(
        self,
    ) -> None:
        def call(name: str, at: int) -> list[dict[str, object]]:
            return [
                _sagent_line(
                    "assistant",
                    "",
                    at=at,
                    tool_calls=[
                        {"id": name, "name": "Bash", "args": {"command": name}},
                    ],
                ),
                {
                    "kind": "history",
                    "type": "tool_result",
                    "call_id": name,
                    "content": "z" * 400,
                    "timestamp": _epoch(at),
                },
            ]

        part = _sagent(
            _sagent_line("agent_send", "go", at=1, source="ada"),
            *call("old", 2),
            *call("new", 3),
            _sagent_line("assistant", "done", at=4, tool_calls=[]),
        )
        seen = window([_segment(0, "sagent", part)])
        room = sum(
            weight(each.record)
            for each in seen.records
            if not isinstance(each.record, ToolCall) or each.record.call_id == "new"
        )

        loaded = cli_records(
            seen,
            "codex",
            seq=_seven,
            budget=Budget(fit_bytes=room - 400, max_bytes=room - 400),
        )
        said = "\n".join(
            record.content or ""
            for record in loaded.records
            if isinstance(record, UserMessage | AssistantMessage)
        )

        assert "ada: go" in said
        assert "done" in said
        # A shell result crosses as structure, so its call does too.
        assert [r.call_id for r in loaded.records if isinstance(r, ToolCall)] == ["new"]
        assert [
            r.call_id for r in loaded.records if isinstance(r, ShellCommandResult)
        ] == ["new"]

    @pytest.mark.parametrize(
        ("fit", "most", "compact", "noted"),
        [
            (10_000, 20_000, False, False),
            (100, 20_000, True, False),
            (100, 200, True, True),
        ],
        ids=["under-fit", "between", "over-max"],
    )
    def test_past_fit_the_target_compacts_and_past_max_turns_are_left_out(
        self,
        fit: int,
        most: int,
        compact: bool,
        noted: bool,
    ) -> None:
        loaded = cli_records(
            window(self._turns(5)),
            "codex",
            seq=_seven,
            budget=Budget(fit_bytes=fit, max_bytes=most),
        )

        assert loaded.compact_after_load is compact
        assert _users(loaded.records)[0].startswith("[trax]") is noted
        assert (loaded.skipped > 0) is noted

    def test_a_huge_shell_result_keeps_its_head_and_tail_and_its_call(self) -> None:
        part = _sagent(
            _sagent_line("agent_send", "run it", at=1, source="ada"),
            _sagent_line(
                "assistant",
                "",
                at=2,
                tool_calls=[{"id": "t1", "name": "Bash", "args": {"command": "make"}}],
            ),
            {
                "kind": "history",
                "type": "tool_result",
                "call_id": "t1",
                "content": "h" * 25_000 + "t" * 25_000,
                "timestamp": _epoch(3),
            },
        )
        seen = window([_segment(0, "sagent", part)])

        loaded = cli_records(seen, "claude", seq=_seven)

        [result] = [r for r in loaded.records if isinstance(r, ShellCommandResult)]
        assert result.stdout == (
            "h" * 10_000 + "[... 36000 bytes cut ...]" + "t" * 4_000
        )
        assert any(
            isinstance(r, ToolCall) and r.call_id == "t1" for r in loaded.records
        )


def test_a_text_at_the_cap_is_kept_whole() -> None:
    assert cut_text("x" * CUT_ABOVE) == "x" * CUT_ABOVE


def test_a_claude_target_reads_the_summary_as_claude_flags_its_own() -> None:
    """Its own turn, flagged, then an acknowledgement the next line cannot fold into."""
    loaded = cli_records(window([_claude_compacting_part()]), "claude", seq=_seven)

    opening, ack = loaded.records[:2]
    assert isinstance(opening, UserMessage)
    assert opening.extra == immutable(
        {"isCompactSummary": True, "isVisibleInTranscriptOnly": True},
    )
    assert isinstance(ack, AssistantMessage)
    assert ack.content == "Understood."


def test_a_summary_of_the_bot_opens_the_window_by_its_provider() -> None:
    part = _sagent(
        _sagent_line("agent_send", "F1 is apples", at=1, source="ada"),
        _sagent_compaction("Summary: ada said F1 is apples."),
        _sagent_line("agent_send", "F2 is pears", at=2, source="ada"),
    )

    seen = window([_segment(0, "sagent", part)])

    assert seen.opening is not None
    assert (seen.opening.provider, seen.opening.summary) == (
        "bot",
        "Summary: ada said F1 is apples.",
    )
    assert _texts(seen) == ["F2 is pears"]


def test_nothing_left_out_asks_for_no_seq() -> None:
    def refuse() -> int:
        raise AssertionError("the note is not written, so its seq is not asked")

    part = _sagent(_sagent_line("agent_send", "q1", at=1, source="ada"))
    loaded = cli_records(window([_segment(0, "sagent", part)]), "codex", seq=refuse)

    assert loaded.skipped == 0
    assert loaded.loaded_records == 1


def test_a_placed_history_keeps_the_place_of_its_clear() -> None:
    seen = window(
        [
            _segment(
                0,
                "codex",
                _codex(_codex_item("user", "q", at=1), *_codex_compacted(at=3)),
            ),
        ],
    )

    assert seen.opening is not None
    assert {(each.part, each.idx) for each in seen.history} == {
        (seen.opening.at.part, seen.opening.at.idx),
    }
    assert all(isinstance(each, Placed) for each in seen.history)
    assert any(isinstance(each.record, AgentToAgentMessage) for each in seen.history)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
