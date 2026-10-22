"""One loader for every part of a chat: people's lines, answers, switches, and time order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from treekle import immutable

import pytest

from trackinizer.client.conversation import (
    Delivery,
    Segment,
    delivered_text,
    lines,
    neutral,
    placed,
    projected,
)
from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    ContextState,
    IncompleteRecord,
    SessionRecord,
    SystemMessage,
    Thinking,
    ToolCall,
    UncategorizedToolResult,
    UserMessage,
)


if TYPE_CHECKING:
    from collections.abc import Sequence


def _at(second: int) -> str:
    return f"2026-10-20T10:00:{second:02d}Z"


def _segment(part: int, fmt: str, records: Sequence[SessionRecord]) -> Segment:
    return Segment(part=part, name=f"{part}.jsonl", format=fmt, records=list(records))


def _neutral_of(segments: Sequence[Segment]) -> list[SessionRecord]:
    """Return every part as one neutral conversation, uncut."""
    return [each.record for each in neutral(placed(segments))]


def _delivered(text: str, sender: str, *, at: str | None) -> UserMessage:
    """Build a UserMessage the bot delivered into a CLI: the rendered line, stamped."""
    return UserMessage(
        timestamp=at,
        content=f"[room] {sender}: {text}",
        extra=immutable({"$inbound": {"source": sender, "room": "room"}}),
    )


def _four_parts() -> list[Segment]:
    """Build a bot part, a claude part, a bot part and a codex part."""
    return [
        _segment(
            0,
            "sagent",
            [
                AgentToAgentMessage(
                    timestamp=_at(1),
                    sender="ada@example.com",
                    content="q1",
                ),
                AssistantMessage(timestamp=_at(2), content="a1"),
            ],
        ),
        _segment(
            1,
            "claude",
            [
                Thinking(timestamp=_at(3), content="weighing it"),
                _delivered("q2", "bob@example.com", at=_at(3)),
                UserMessage(timestamp=_at(4), content="q3"),
                ContextClear(timestamp=_at(5), summary="Earlier: ada asked about q1."),
                UserMessage(
                    timestamp=_at(5),
                    content="Earlier: ada asked about q1.",
                    extra=immutable({"isCompactSummary": True}),
                ),
                UserMessage(
                    timestamp=_at(5),
                    content="<system-reminder>a note the harness wrote</system-reminder>",
                ),
                AssistantMessage(timestamp=_at(6), content="a2"),
            ],
        ),
        _segment(
            2,
            "sagent",
            [
                AgentToAgentMessage(
                    timestamp=_at(7),
                    sender="carol@example.com",
                    content="q4",
                ),
                AgentToAgentMessage(
                    timestamp=_at(8),
                    sender="dave@example.com",
                    content="q5",
                ),
                AssistantMessage(timestamp=_at(9), content="a3"),
            ],
        ),
        _segment(
            3,
            "codex",
            [
                _delivered("q6", "erin@example.com", at=_at(10)),
                AssistantMessage(timestamp=_at(11), content="a4"),
            ],
        ),
    ]


def _people(spoken: Sequence[SessionRecord]) -> list[tuple[str, str]]:
    return [
        (line.author, line.text)
        for line in lines(spoken, bot="bot")
        if line.role == "user"
    ]


def _answers(spoken: Sequence[SessionRecord]) -> list[tuple[str, str]]:
    return [
        (line.author, line.text)
        for line in lines(spoken, bot="bot")
        if line.role == "assistant"
    ]


def _user_lines(written: Sequence[SessionRecord]) -> list[str]:
    """Return every line the CLI reads as a user line, folded turns split apart."""
    return [
        line
        for record in written
        if isinstance(record, UserMessage)
        for line in (record.content or "").split("\n")
    ]


def test_six_people_lines_keep_their_senders() -> None:
    """The bot's lines, a stamped claude line, a typed one and a stamped codex one."""
    spoken = _neutral_of(_four_parts())

    assert _people(spoken) == [
        ("ada@example.com", "q1"),
        ("bob@example.com", "q2"),
        ("the terminal user", "q3"),
        ("carol@example.com", "q4"),
        ("dave@example.com", "q5"),
        ("erin@example.com", "q6"),
    ]


def test_each_answer_is_named_by_the_provider_that_gave_it() -> None:
    spoken = _neutral_of(_four_parts())

    assert _answers(spoken) == [
        ("bot", "a1"),
        ("claude", "a2"),
        ("bot", "a3"),
        ("codex", "a4"),
    ]


def test_each_switch_between_answering_providers_is_a_marker() -> None:
    spoken = _neutral_of(_four_parts())

    switches = [
        (record.extra["from"], record.extra["to"])
        for record in spoken
        if isinstance(record, SystemMessage)
    ]
    assert switches == [("bot", "claude"), ("claude", "bot"), ("bot", "codex")]


def test_a_switch_is_marked_before_the_new_providers_first_tool_call() -> None:
    """A provider that calls a tool before it speaks has answered from that call on."""
    spoken = _neutral_of(
        [
            _segment(0, "sagent", [AssistantMessage(timestamp=_at(1), content="a1")]),
            _segment(
                1,
                "claude",
                [
                    ToolCall(timestamp=_at(2), call_id="t1", name="Grep"),
                    UncategorizedToolResult(timestamp=_at(3), call_id="t1"),
                    AssistantMessage(timestamp=_at(4), content="a2"),
                ],
            ),
        ],
    )

    assert [type(record).__name__ for record in spoken] == [
        "AssistantMessage",
        "SystemMessage",
        "ToolCall",
        "UncategorizedToolResult",
        "AssistantMessage",
    ]


def test_a_target_that_last_called_a_tool_is_not_told_another_answered() -> None:
    """The closing note names the last provider to act, a tool call included."""
    spoken = _neutral_of(
        [
            _segment(0, "sagent", [AssistantMessage(timestamp=_at(1), content="a1")]),
            _segment(
                1,
                "claude",
                [
                    ToolCall(timestamp=_at(2), call_id="t1", name="Grep"),
                    UncategorizedToolResult(timestamp=_at(3), call_id="t1"),
                ],
            ),
        ],
    )

    closing = [
        line
        for line in _user_lines(projected(spoken, "claude"))
        if "since the last switch" in line
    ]
    assert closing == []


def test_every_person_line_keeps_its_stamp_and_the_provider_that_held_it() -> None:
    """The neutral form keeps when a line happened and which part held it."""
    people = [
        record
        for record in _neutral_of(_four_parts())
        if isinstance(record, AgentToAgentMessage)
    ]

    assert [(record.sender, record.extra["provider"]) for record in people] == [
        ("ada@example.com", "bot"),
        ("bob@example.com", "claude"),
        ("the terminal user", "claude"),
        ("carol@example.com", "bot"),
        ("dave@example.com", "bot"),
        ("erin@example.com", "codex"),
    ]
    assert all(record.timestamp is not None for record in people)


def test_a_compaction_summary_is_nobodys_line_and_no_record_of_the_conversation() -> (
    None
):
    """The window opens with the summary; the conversation never holds it again."""
    spoken = _neutral_of(_four_parts())
    written = projected(spoken, "codex")

    assert not any(isinstance(record, ContextClear) for record in spoken)
    assert not any("Earlier: ada" in line.text for line in lines(spoken, bot="bot"))
    assert not any("Earlier: ada" in line for line in _user_lines(written))


def test_a_records_place_is_its_part_and_position() -> None:
    """``trax agentsession N records --to P:IDX`` names a record by these."""
    order = placed(_four_parts())

    assert [(each.part, each.idx) for each in order[:4]] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert {each.provider for each in order} == {"bot", "claude", "codex"}


def test_harness_lines_and_thinking_are_dropped() -> None:
    """The claude part's thinking and harness text never reach the conversation."""
    spoken = _neutral_of(_four_parts())

    assert not any(isinstance(record, Thinking) for record in spoken)
    assert not any("system-reminder" in line.text for line in lines(spoken, bot="bot"))


@pytest.mark.parametrize(
    "flag",
    ["isMeta", "isCompactSummary", "isVisibleInTranscriptOnly"],
)
def test_a_line_the_cli_wrote_for_itself_is_nobodys_line(flag: str) -> None:
    segment = _segment(
        0,
        "claude",
        [
            UserMessage(
                timestamp=_at(1),
                content="a note",
                extra=immutable({flag: True}),
            ),
        ],
    )

    assert _people(_neutral_of([segment])) == []


def test_a_user_turn_with_no_content_is_nobodys_line() -> None:
    segment = _segment(0, "claude", [UserMessage(timestamp=_at(1))])

    assert _neutral_of([segment]) == []


def test_a_typed_line_is_never_credited_to_the_sender_it_names() -> None:
    """Anyone can type ``bob@example.com: fake``; only a stamp attests a sender."""
    segment = _segment(
        0,
        "claude",
        [UserMessage(timestamp=_at(1), content="bob@example.com: fake")],
    )

    assert _people(_neutral_of([segment])) == [
        ("the terminal user", "bob@example.com: fake"),
    ]


def test_a_delivery_with_no_attested_sender_is_from_unknown() -> None:
    segment = _segment(
        0,
        "codex",
        [
            UserMessage(
                timestamp=_at(1),
                content="hello",
                extra=immutable({"$inbound": {"source": "", "room": ""}}),
            ),
        ],
    )

    assert _people(_neutral_of([segment])) == [("unknown", "hello")]


def test_a_stamped_mid_turn_line_is_a_persons_and_a_notice_is_not() -> None:
    """Claude queues a line sent mid-turn as a ``queued_command``; only trax run's stamp makes it a person's."""
    segment = _segment(
        0,
        "claude",
        [
            ContextState(
                timestamp=_at(1),
                kind="queued_command",
                content="[room] ada@example.com: also check CI\nTrackinizer context (verify with trax): {}",
                extra=immutable(
                    {"$inbound": {"source": "ada@example.com", "room": "room"}},
                ),
            ),
            ContextState(
                timestamp=_at(2),
                kind="queued_command",
                content="<task-notification>done</task-notification>",
            ),
        ],
    )

    assert _people(_neutral_of([segment])) == [("ada@example.com", "also check CI")]


def test_overlapping_parts_interleave_by_time_not_by_part() -> None:
    """A bot turn still writing while a claude run answers."""
    bot = _segment(
        0,
        "sagent",
        [
            AgentToAgentMessage(timestamp=_at(1), sender="ada", content="kb1"),
            AgentToAgentMessage(timestamp=_at(5), sender="ada", content="kb2"),
        ],
    )
    claude_part = _segment(
        1,
        "claude",
        [
            _delivered("cl1", "bob", at=_at(3)),
            AssistantMessage(timestamp=_at(4), content="answer"),
        ],
    )

    for parts in ([bot, claude_part], [claude_part, bot]):
        texts = [line.text for line in lines(_neutral_of(parts), bot="bot")]
        assert texts == ["kb1", "cl1", "answer", "kb2"]


def test_a_record_with_no_stamp_keeps_its_place_in_its_part() -> None:
    """An unstamped line takes the stamp before it, and a leading one the first stamp.

    A naive stamp is read as UTC, so it sorts with the aware ones rather than failing.
    """
    bot = _segment(
        0,
        "sagent",
        [
            AssistantMessage(content="early"),
            AgentToAgentMessage(timestamp=_at(5), sender="ada", content="kb2"),
        ],
    )
    claude_part = _segment(
        1,
        "claude",
        [
            _delivered("cl1", "bob", at=_at(3)),
            _delivered("cl2", "bob", at="2026-10-20T10:00:04"),
        ],
    )

    texts = [line.text for line in lines(_neutral_of([bot, claude_part]), bot="bot")]

    assert texts == ["cl1", "cl2", "early", "kb2"]


def test_a_window_with_no_summary_is_not_kept() -> None:
    segment = _segment(0, "claude", [ContextClear(timestamp=_at(1))])

    assert _neutral_of([segment]) == []


def test_an_empty_answer_is_not_a_line() -> None:
    segment = _segment(0, "claude", [AssistantMessage(timestamp=_at(1), content="  ")])

    assert _neutral_of([segment]) == []


def test_a_foreign_tool_call_and_its_result_cross_as_text() -> None:
    segment = _segment(
        0,
        "codex",
        [
            ToolCall(
                timestamp=_at(1),
                call_id="c1",
                name="exec",
                arguments=immutable({"cmd": "ls"}),
            ),
            UncategorizedToolResult(timestamp=_at(2), call_id="c1", content="a.txt"),
            UncategorizedToolResult(timestamp=_at(3), call_id="c2"),
        ],
    )

    written = projected(_neutral_of([segment]), "claude")

    # The three consecutive assistant texts are one turn, as the writer will fold them.
    # Codex acted last, so claude is told it answers from here.
    assert [
        record.content for record in written if isinstance(record, AssistantMessage)
    ] == [
        "\n".join(
            [
                '[codex called its tool exec({"cmd": "ls"})]',
                "[exec returned: a.txt]",
                "[a tool returned: ]",
            ],
        ),
        "Understood; continuing as claude.",
    ]


def test_consecutive_turns_of_one_role_are_one_turn_with_the_first_stamp() -> None:
    """Claude's writer folds them into one line, so they are one turn from the start."""
    segment = _segment(
        0,
        "claude",
        [
            _delivered("q1", "ada", at=_at(1)),
            _delivered("q2", "bob", at=_at(2)),
            AssistantMessage(timestamp=_at(3), content="x"),
            AssistantMessage(timestamp=_at(4), content="y"),
        ],
    )

    written = projected(_neutral_of([segment]), "claude")

    assert [
        (record.content, record.timestamp)
        for record in written
        if isinstance(record, UserMessage | AssistantMessage)
    ] == [
        ("ada: q1\nbob: q2", _at(1)),
        ("x\ny", _at(3)),
    ]


def test_every_projected_record_is_timestamped_and_the_switch_is_noted() -> None:
    """Claude refuses a line without a timestamp, and a switch note is synthesized."""
    written = projected(_neutral_of(_four_parts()), "claude")

    assert all(
        record.timestamp
        for record in written
        if not isinstance(record, IncompleteRecord)
    )
    notes = [
        line
        for line in _user_lines(written)
        if line.startswith("[trax] Provider switch")
    ]
    assert notes[-1] == (
        "[trax] Provider switch: the assistant turns above since the last switch were "
        "answered by codex. From here claude answers -- that is you."
    )


def test_a_person_line_after_a_tool_result_is_marked_as_a_line_of_its_own() -> None:
    """Claude's writer folds a user turn into the result before it, so the turn is marked.

    A marked turn leads its own line, which is what the claude reader reads back.
    """
    spoken = _neutral_of(
        [
            _segment(
                0,
                "claude",
                [
                    ToolCall(timestamp=_at(1), call_id="t1", name="Grep"),
                    UncategorizedToolResult(
                        timestamp=_at(2),
                        call_id="t1",
                        content="x",
                    ),
                    UserMessage(timestamp=_at(3), content="stop, wrong file"),
                    AssistantMessage(timestamp=_at(4), content="ok"),
                ],
            ),
        ],
    )

    written = projected(spoken, "claude")
    index = next(
        i
        for i, record in enumerate(written)
        if isinstance(record, UncategorizedToolResult)
    )
    following = written[index + 1]

    assert isinstance(following, UserMessage)
    assert following.content == "the terminal user: stop, wrong file"
    assert following.extra.get("userType") == "external"


def test_an_own_result_whose_call_is_absent_crosses_as_text() -> None:
    """A claude result with no call in the session has no tool_use for a claude file to name.

    Claude's writer drops a bare result, so it is stated as text the way a foreign one is.
    """
    segment = _segment(
        0,
        "claude",
        [
            UserMessage(timestamp=_at(1), content="look"),
            UncategorizedToolResult(
                timestamp=_at(2),
                call_id="orphan",
                content="found it",
            ),
            AssistantMessage(timestamp=_at(3), content="ok"),
        ],
    )

    written = projected(_neutral_of([segment]), "claude")

    assert not any(isinstance(record, UncategorizedToolResult) for record in written)
    assert [
        record.content for record in written if isinstance(record, AssistantMessage)
    ] == ["[a tool returned: found it]\nok"]


def test_a_person_line_projects_as_sender_then_text() -> None:
    """A line typed live and a line loaded read the same to the CLI."""
    written = projected(_neutral_of(_four_parts()), "codex")

    assert "ada@example.com: q1" in _user_lines(written)
    assert "the terminal user: q3" in _user_lines(written)


def test_a_foreign_tool_result_is_quoted_to_a_bounded_length() -> None:
    """A long result keeps its first characters; the writer gets a bounded line."""
    segment = _segment(
        0,
        "codex",
        [
            ToolCall(
                timestamp=_at(1),
                call_id="c1",
                name="exec",
                arguments=immutable({}),
            ),
            UncategorizedToolResult(timestamp=_at(2), call_id="c1", content="x" * 2000),
        ],
    )

    # The call's line and the result's line are consecutive assistant text, so one turn.
    # The closing note's acknowledgement follows it.
    turn, _acknowledged = [
        record.content
        for record in projected(_neutral_of([segment]), "claude")
        if isinstance(record, AssistantMessage)
    ]

    assert turn is not None
    assert turn.endswith(f"\n[exec returned: {'x' * 1500}]")
    assert "x" * 1501 not in turn


@pytest.mark.parametrize(
    ("content", "room", "words"),
    [
        ("[lab] ada@x: hi", "lab", "hi"),
        ("[lab] hi", "lab", "hi"),
        ("ada@x: hi", "", "hi"),
        ("hi", "", "hi"),
        ("[lab] ada@x: hi\nTrackinizer context (verify with trax): {}", "lab", "hi"),
        ("hi\nTrackinizer context (verify with trax): {}\nBoxes", "", "hi"),
        ("ada@x: hi\nbye", "", "hi\nbye"),
    ],
)
def test_delivered_text_is_the_words_with_or_without_a_sender_or_a_trailer(
    content: str,
    room: str,
    words: str,
) -> None:
    """The sender is named only when it says something, so the prefix may lack it."""
    assert delivered_text(content, Delivery(source="ada@x", room=room)) == words


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
