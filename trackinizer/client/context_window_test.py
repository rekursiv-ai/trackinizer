"""The window a switch loads: one cut for every provider, then a per-target budget.

Every part here is read by its own provider's reader from lines shaped like the ones
measured: claude's ``compact_boundary`` and flagged summary, codex's sealed
``compacted`` line, and the bot's journal of IR records.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING

import json

from treekle import immutable

import pytest

from trackinizer.client.context_window import (
    BUDGETS,
    CUT_ABOVE,
    Budget,
    Window,
    cli_records,
    cut_text,
    weight,
    window,
)
from trackinizer.client.conversation import Placed, Segment, lines
from trackinizer.sagent.scripts.convert_session import claude, codex, sagent
from trackinizer.sagent.session.jsonl import record_line
from trackinizer.sagent.session.types import (
    AgentToAgentMessage,
    AssistantMessage,
    BashResult,
    ContextClear,
    ContextCompaction,
    EditResult,
    GrepResult,
    Splice,
    Thinking,
    ToolCall,
    ToolResult,
    UncategorizedToolResult,
    UserMessage,
    WebSearchHit,
    WebSearchResult,
    WriteResult,
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


def _sagent(*records: SessionRecord) -> list[SessionRecord]:
    """Read ``records`` back from a journal sagent wrote of them."""
    text = b"".join(record_line(record) for record in records).decode()
    return list(sagent.normalize(StringIO(text)))


def _iso(second: int) -> str:
    return datetime(2026, 10, 20, 10, 0, second, tzinfo=UTC).isoformat()


def _peer(text: str, *, at: int, source: str | None = None) -> AgentToAgentMessage:
    return AgentToAgentMessage(timestamp=_iso(at), content=text, sender=source)


def _answer(text: str, *, at: int) -> AssistantMessage:
    return AssistantMessage(timestamp=_iso(at), content=text)


def _shell(call_id: str, command: str, out: str, *, at: int) -> list[SessionRecord]:
    """Return a Bash call and its result, as sagent journals them."""
    return [
        ToolCall(
            timestamp=_iso(at),
            call_id=call_id,
            name="Bash",
            arguments={"command": command},
        ),
        ToolResult(
            timestamp=_iso(at + 1),
            call_id=call_id,
            state="done",
            stdout=(out,),
            result=BashResult(command=(command,)),
        ),
    ]


def _sagent_compaction(summary: str) -> list[SessionRecord]:
    return [
        ContextCompaction(extra={"strategy": "summary", "token_before": 9_000}),
        ContextClear(summary=summary),
    ]


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
            _peer("q1 before", at=1, source="ada"),
            _peer("q9 after", at=20, source="ada"),
        )
        compacting = _claude(
            _claude_user("claude before", uuid="u1", at=2),
            *_claude_compaction("Summary: q1 and claude before.", at=5),
            _claude_user("claude after", uuid="u2", at=6),
        )
        bot_late = _sagent(_peer("q10 later", at=30, source="bob"))

        seen = window(
            [
                _segment(0, "assistant", bot_early),
                _segment(1, "claude", compacting),
                _segment(2, "assistant", bot_late),
            ],
        )

        assert seen.opening is not None
        assert seen.opening.provider == "claude"
        assert _texts(seen) == [
            "claude after",
            "q9 after",
            "q10 later",
        ]

    def test_the_retrieval_command_ends_at_the_last_omitted_part(self) -> None:
        seen = window(
            [
                _segment(
                    0,
                    "assistant",
                    [UserMessage(timestamp=_at(1), content="old")],
                ),
                _segment(1, "codex", [UserMessage(timestamp=_at(2), content="recent")]),
                _segment(
                    2,
                    "claude",
                    [
                        ContextClear(timestamp=_at(3), summary="Earlier questions"),
                        UserMessage(timestamp=_at(4), content="continue"),
                    ],
                ),
            ],
        )

        loaded = cli_records(seen, "claude", seq=_seven)

        assert loaded.skipped == 2
        assert "trax agentsession 7 records --to 1:0" in _users(loaded.records)[0]
        assert _users(loaded.records)[-1] == "the terminal user: continue"

    @pytest.mark.parametrize("tool", [False, True], ids=["answer", "tool"])
    def test_the_first_new_provider_is_named_after_the_carried_history(
        self,
        tool: bool,
    ) -> None:
        last = (
            ToolCall(call_id="t", name="Read", arguments={})
            if tool
            else AssistantMessage(content="old answer")
        )
        seen = window(
            [
                _segment(
                    0,
                    "codex",
                    [
                        ContextClear(
                            timestamp=_at(1),
                            summary="Earlier work",
                            history=(last, UserMessage(content="pending question")),
                        ),
                    ],
                ),
                _segment(
                    1,
                    "claude",
                    [AssistantMessage(timestamp=_at(2), content="new answer")],
                ),
            ],
        )

        loaded = cli_records(seen, "claude", seq=_seven)

        switches = [
            text for text in _users(loaded.records) if "Provider switch:" in text
        ]
        assert len(switches) == 1
        assert "assistant turns above were answered by codex." in switches[0]
        assert "From here claude answers -- that is you." in switches[0]
        assert any(
            isinstance(record, AssistantMessage)
            and "new answer" in (record.content or "")
            for record in loaded.records
        )

    def test_session_opening_clears_and_a_codex_fork_never_cut(self) -> None:
        """A fork's ``cleared_session_id`` would otherwise drop every earlier part."""
        parts = [
            _segment(
                0,
                "assistant",
                _sagent(_peer("q1", at=1, source="ada")),
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


def _sealed_codex_parts() -> list[Segment]:
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


class TestASealedCodexSummary:
    """Codex's summary is ciphertext: only codex reads it; the kept turns cross."""

    def test_codex_gets_its_compaction_back_natively(self) -> None:
        loaded = cli_records(window(_sealed_codex_parts()), "codex", seq=_seven)

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
        loaded = cli_records(window(_sealed_codex_parts()), "claude", seq=_seven)

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
        records: list[SessionRecord] = []
        for turn in range(count):
            records.extend(
                [
                    _peer(f"q{turn:02d} " + "x" * 90, at=turn, source="ada"),
                    _answer(f"a{turn:02d} " + "y" * 90, at=turn),
                ],
            )
        return [_segment(0, "assistant", _sagent(*records))]

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
        part = _sagent(
            _peer("go", at=1, source="ada"),
            *_shell("old", "old", "z" * 400, at=2),
            *_shell("new", "new", "z" * 400, at=4),
            _answer("done", at=6),
        )
        seen = window([_segment(0, "assistant", part)])
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
            r.call_id
            for r in loaded.records
            if isinstance(r, ToolResult) and isinstance(r.result, BashResult)
        ] == [
            "new",
        ]

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

    @pytest.mark.parametrize(
        ("fit", "most", "compact"),
        [(9, 11, False), (8, 9, True), (9, 9, False)],
        ids=["at-fit", "at-max", "at-both"],
    )
    def test_exact_byte_limits_preserve_the_whole_window(
        self,
        fit: int,
        most: int,
        compact: bool,
    ) -> None:
        seen = window(
            [
                _segment(
                    0,
                    "codex",
                    [
                        UserMessage(timestamp=_at(1), content="é"),
                        AssistantMessage(timestamp=_at(2), content="hello!!"),
                    ],
                ),
            ],
        )

        loaded = cli_records(
            seen,
            "codex",
            seq=_seven,
            budget=Budget(fit_bytes=fit, max_bytes=most),
        )

        assert _users(loaded.records) == ["the terminal user: é"]
        assert [
            r.content for r in loaded.records if isinstance(r, AssistantMessage)
        ] == [
            "hello!!",
        ]
        assert (loaded.loaded_records, loaded.skipped, loaded.estimated_tokens) == (
            2,
            0,
            2,
        )
        assert loaded.compact_after_load is compact

    def test_sagent_loads_past_codexs_budget_whole_and_is_not_asked_to_compact(
        self,
    ) -> None:
        """Its model's window decides: sagent compacts before a call it cannot hold."""
        peers = [_peer(f"q{turn:02d} " + "x" * 15_000, at=turn) for turn in range(60)]
        seen = window([_segment(0, "assistant", _sagent(*peers))])

        loaded = cli_records(seen, "sagent", seq=_seven)

        assert sum(weight(each.record) for each in seen.records) > (
            BUDGETS["codex"].max_bytes
        )
        assert (loaded.loaded_records, loaded.skipped) == (60, 0)
        assert not loaded.compact_after_load

    def test_a_huge_shell_result_keeps_its_head_and_tail_and_its_call(self) -> None:
        part = _sagent(
            _peer("run it", at=1, source="ada"),
            *_shell("t1", "make", "h" * 25_000 + "t" * 25_000, at=2),
        )
        seen = window([_segment(0, "assistant", part)])

        loaded = cli_records(seen, "claude", seq=_seven)

        [result] = [
            r
            for r in loaded.records
            if isinstance(r, ToolResult) and isinstance(r.result, BashResult)
        ]
        assert result.stdout == (
            "h" * 10_000 + "[... 36000 bytes cut ...]" + "t" * 4_000,
        )
        assert any(
            isinstance(r, ToolCall) and r.call_id == "t1" for r in loaded.records
        )


def test_a_text_at_the_cap_is_kept_whole() -> None:
    assert cut_text("x" * CUT_ABOVE) == "x" * CUT_ABOVE


def test_a_utf8_cut_discards_partial_characters_at_both_edges() -> None:
    text = "x" * 9_999 + "é" + "m" * 2_000 + "é" + "z" * 3_999

    assert cut_text(text) == "x" * 9_999 + "[... 2002 bytes cut ...]" + "z" * 3_999


@pytest.mark.parametrize("shell", [False, True], ids=["answer", "stderr"])
def test_loading_shortens_each_oversized_text_without_changing_the_original(
    shell: bool,
) -> None:
    text = "h" * 10_000 + "m" * (CUT_ABOVE - 14_000 + 1) + "t" * 4_000
    record = (
        ToolResult(
            call_id="t",
            state="done",
            stdout=("ok",),
            stderr=(text,),
            result=BashResult(),
        )
        if shell
        else AssistantMessage(content=text)
    )
    seen = window([_segment(0, "codex", [record])])

    loaded = cli_records(seen, "codex", seq=_seven)

    [shortened] = loaded.records
    expected = "h" * 10_000 + "[... 2001 bytes cut ...]" + "t" * 4_000
    if shell:
        assert isinstance(shortened, ToolResult)
        assert isinstance(shortened.result, BashResult)
        assert (shortened.stdout, shortened.stderr) == (("ok",), (expected,))
        assert isinstance(record, ToolResult)
        assert record.stderr == (text,)
    else:
        assert isinstance(shortened, AssistantMessage)
        assert shortened.content == expected
        assert isinstance(record, AssistantMessage)
        assert record.content == text


def test_loading_cuts_a_write_results_content_and_leaves_an_edits_splices() -> None:
    """A write's text is its result's ``content``; an edit's splices are never cut."""
    text = "h" * 10_000 + "m" * (CUT_ABOVE - 14_000 + 1) + "t" * 4_000
    write_record = ToolResult(
        timestamp=_at(1),
        call_id="w",
        state="done",
        stdout=("Wrote a.py.",),
        result=WriteResult(path="a.py", content=text),
    )
    edit_record = ToolResult(
        timestamp=_at(2),
        call_id="e",
        state="done",
        stdout=("Edited a.py.", Splice(before=text, after="")),
        result=EditResult(path="a.py"),
    )
    seen = window([_segment(0, "codex", [write_record, edit_record])])

    loaded = cli_records(seen, "codex", seq=_seven)

    cut = "h" * 10_000 + "[... 2001 bytes cut ...]" + "t" * 4_000
    assert loaded.records == [
        replace(write_record, result=WriteResult(path="a.py", content=cut)),
        edit_record,
    ]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            ToolResult(
                call_id="w2",
                state="done",
                stdout=("Wrote a.py.",),
                result=WriteResult(path="a.py", content="é字"),
            ),
            5,
        ),
        (ToolResult(call_id="w3", state="done", result=WriteResult(path="a.py")), 0),
        (
            ToolResult(
                call_id="b",
                state="done",
                stdout=("a", "b"),
                stderr=("c",),
                # The exit code is a result field, not output, so it adds no weight.
                result=BashResult(exit_code=2),
            ),
            3,
        ),
        (
            ToolResult(
                call_id="g",
                state="done",
                stdout=("abcd",),
                result=GrepResult(pattern="x"),
            ),
            0,
        ),
        (
            ToolResult(
                call_id="u",
                state="done",
                stdout=("abcd",),
                result=UncategorizedToolResult(),
            ),
            4,
        ),
        (ToolResult(call_id="f", state="failed", stderr=("boom",)), 0),
        (ToolResult(call_id="p", state="pending", shown="[running]"), 0),
    ],
    ids=[
        "write",
        "write-without-content",
        "shell",
        "another-tool",
        "unknown-tool",
        "failed",
        "pending",
    ],
)
def test_weight_counts_a_tool_call_records_text_by_its_result_class(
    record: SessionRecord,
    expected: int,
) -> None:
    assert weight(record) == expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            ToolResult(
                call_id="e1",
                state="done",
                stdout=(
                    "Edited the file.",
                    Splice(before="ab", after="cde"),
                    Splice(after="f"),
                ),
                result=EditResult(),
            ),
            6,
        ),
        (
            ToolResult(
                call_id="w1",
                state="done",
                stdout=(
                    "AB\nhttps://a.b\nxyz\n\nC",
                    WebSearchHit(url="https://a.b", title="AB", snippet="xyz"),
                    WebSearchHit(title="C"),
                ),
                result=WebSearchResult(),
            ),
            17,
        ),
        (ContextClear(summary="four"), 4),
        (ContextClear(), 0),
        (Thinking(content="not read by a target"), 0),
        (ToolCall(call_id="t", name="Bash", arguments={"n": 123}), 14),
        (AssistantMessage(content="é字"), 5),
        (AssistantMessage(), 0),
    ],
    ids=[
        "edit",
        "web-search",
        "clear",
        "clear-without-summary",
        "thinking",
        "call-arguments",
        "utf8",
        "empty-answer",
    ],
)
def test_weight_counts_the_text_a_target_reads(
    record: SessionRecord,
    expected: int,
) -> None:
    assert weight(record) == expected


@pytest.mark.parametrize(("most", "first"), [(5, 2), (6, 1)])
def test_native_history_uses_the_cumulative_budget_after_the_summary(
    most: int,
    first: int,
) -> None:
    history = (
        UserMessage(content="aa"),
        AssistantMessage(content="bb"),
        UserMessage(content="c"),
        AssistantMessage(content="d"),
    )
    original = ContextClear(timestamp=_at(1), summary="Σ", history=history)
    seen = window(
        [
            _segment(
                0,
                "codex",
                [
                    original,
                    UserMessage(timestamp=_at(2), content="x"),
                ],
            ),
        ],
    )

    loaded = cli_records(
        seen,
        "codex",
        seq=_seven,
        budget=Budget(fit_bytes=most, max_bytes=most),
    )

    [clear] = [r for r in loaded.records if isinstance(r, ContextClear)]
    assert clear.history == history[first:]
    assert original.history == history
    assert (loaded.loaded_records, loaded.skipped, loaded.estimated_tokens) == (
        len(history) - first + 1,
        first,
        1,
    )
    note = _users(loaded.records)[0]
    assert f"{first} earlier records are not loaded" in note
    assert "trax agentsession 7 records --to 0:0" in note
    assert _users(loaded.records)[-1] == "the terminal user: x"
    assert loaded.compact_after_load


def test_past_max_the_newest_kept_turns_of_the_opening_fit_first() -> None:
    """Room left after the summary goes to the opening's newest kept turns."""
    seen = window(_sealed_codex_parts())
    kept = [each.record for each in seen.history]
    newest = weight(kept[-1])
    assert newest > 0
    assert all(weight(record) > 0 for record in kept[:-1])

    loaded = cli_records(
        seen,
        "claude",
        seq=_seven,
        budget=Budget(fit_bytes=newest, max_bytes=newest),
    )

    said = [
        record.content or ""
        for record in loaded.records
        if isinstance(record, UserMessage | AssistantMessage)
    ]
    assert any("kept: europe-west4" in text for text in said)
    assert not any("kept: which bucket?" in text for text in said)
    assert loaded.compact_after_load


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_target_reads_the_summary_with_its_own_flags_and_original_time(
    target: str,
) -> None:
    """Its own turn, flagged, then an acknowledgement the next line cannot fold into."""
    loaded = cli_records(window([_claude_compacting_part()]), target, seq=_seven)

    opening, ack = loaded.records[:2]
    assert isinstance(opening, UserMessage)
    assert opening.extra == immutable(
        {"isCompactSummary": True, "isVisibleInTranscriptOnly": True}
        if target == "claude"
        else {},
    )
    assert isinstance(ack, AssistantMessage)
    assert ack.content == "Understood."
    expected = datetime.fromisoformat(_at(3))
    assert datetime.fromisoformat(opening.timestamp or "") == expected
    assert datetime.fromisoformat(ack.timestamp or "") == expected


def test_a_summary_of_the_bot_opens_the_window_by_its_provider() -> None:
    part = _sagent(
        _peer("F1 is apples", at=1, source="ada"),
        *_sagent_compaction("Summary: ada said F1 is apples."),
        _peer("F2 is pears", at=2, source="ada"),
    )

    seen = window([_segment(0, "assistant", part)])

    assert seen.opening is not None
    assert (seen.opening.provider, seen.opening.summary) == (
        "bot",
        "Summary: ada said F1 is apples.",
    )
    assert _texts(seen) == ["F2 is pears"]


def test_nothing_left_out_asks_for_no_seq() -> None:
    def refuse() -> int:
        raise AssertionError("the note is not written, so its seq is not asked")

    part = _sagent(_peer("q1", at=1, source="ada"))
    loaded = cli_records(window([_segment(0, "assistant", part)]), "codex", seq=refuse)

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
