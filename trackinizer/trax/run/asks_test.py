"""Tests for the ask model: its records in Chat, the answers to it, and the driver seam.

The records must stay a superset of the ones :mod:`.prompt_bridge` writes today, so the
web ``PromptCard`` that reads ``type``, ``tool``, ``input``, ``questions``, ``plan`` and
``decision`` keeps rendering them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import asyncio
import inspect
import json

from treekle import PlainTree, from_plain

import pytest

from trackinizer.trax.run.asks import (
    Answer,
    Ask,
    AskKind,
    Driver,
    Option,
    Resolved,
    Resolver,
    StateChanged,
    answer_from_payload,
    opened_record,
    resolved_record,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping


_AT: Final = datetime(2026, 10, 21, 12, 30, tzinfo=UTC)

_OPTIONS: Final = (
    Option(id="allow_once", label="Yes", kind="allow_once"),
    Option(id="allow_session", label="Yes, for this session", kind="allow_session"),
    Option(id="deny", label="No", kind="deny"),
    Option(id="abort", label="No, and stop", kind="deny_stop"),
)

_QUESTIONS: PlainTree = [
    {
        "question": "Pick a color",
        "header": "Color",
        "options": [{"label": "red"}, {"label": "blue"}],
        "multiSelect": False,
    },
    {
        "question": "Pick a size",
        "header": "Size",
        "options": [{"label": "S"}, {"label": "L"}],
        "multiSelect": True,
    },
]


def _command_ask() -> Ask:
    return Ask(
        id="a1",
        provider="codex",
        kind="command",
        title="Run a command",
        tool="shell",
        tool_call_id="item-7",
        command="touch x",
        cwd="/work",
        reason="create the file",
        input={"command": "touch x"},
        options=_OPTIONS,
    )


def test_opened_record_carries_every_field() -> None:
    record = opened_record(_command_ask(), at=_AT)
    assert record.subtype == "interaction"
    assert record.timestamp == _AT.isoformat()
    assert record.content == "Codex asks to use shell"
    assert from_plain(record.extra["$interaction"], dict[str, PlainTree]) == {
        "id": "a1",
        "type": "permission",
        "tool": "shell",
        "input": {"command": "touch x"},
        "questions": None,
        "plan": None,
        "state": "pending",
        "provider": "codex",
        "kind": "command",
        "title": "Run a command",
        "tool_call_id": "item-7",
        "command": "touch x",
        "cwd": "/work",
        "reason": "create the file",
        "diff": "",
        "options": [
            {"id": "allow_once", "label": "Yes", "kind": "allow_once"},
            {
                "id": "allow_session",
                "label": "Yes, for this session",
                "kind": "allow_session",
            },
            {"id": "deny", "label": "No", "kind": "deny"},
            {"id": "abort", "label": "No, and stop", "kind": "deny_stop"},
        ],
        "answerable_in_chat": True,
    }


def test_opened_record_survives_a_json_round_trip() -> None:
    record = opened_record(_command_ask(), at=_AT)
    assert json.loads(json.dumps(record.extra)) == dict(record.extra)


def test_claude_permission_keeps_todays_fields() -> None:
    ask = Ask(
        id="p1",
        provider="claude",
        kind="permission",
        tool="Bash",
        input={"command": "ls"},
    )
    record = opened_record(ask, at=_AT)
    assert record.content == "Claude asks to use Bash"
    card = from_plain(record.extra["$interaction"], dict[str, PlainTree])
    assert {key: card[key] for key in _TODAY} == {
        "id": "p1",
        "type": "permission",
        "tool": "Bash",
        "input": {"command": "ls"},
        "questions": None,
        "plan": None,
        "state": "pending",
    }


_TODAY: Final = ("id", "type", "tool", "input", "questions", "plan", "state")


def test_question_keeps_questions_and_reads_them_in_content() -> None:
    ask = Ask(
        id="q1",
        provider="claude",
        kind="question",
        tool="AskUserQuestion",
        input={"questions": _QUESTIONS},
        questions=tuple(from_plain(_QUESTIONS, list[PlainTree])),
    )
    record = opened_record(ask, at=_AT)
    assert record.content == "Claude asks: Pick a color Pick a size"
    card = from_plain(record.extra["$interaction"], dict[str, PlainTree])
    assert card["type"] == "question"
    assert card["questions"] == _QUESTIONS
    assert card["plan"] is None


def test_question_without_questions_reads_as_its_tool() -> None:
    ask = Ask(
        id="q2",
        provider="claude",
        kind="question",
        tool="AskUserQuestion",
        questions=(),
    )
    assert opened_record(ask, at=_AT).content == "Claude asks to use AskUserQuestion"


def test_plan_keeps_plan_and_reads_it_in_content() -> None:
    ask = Ask(
        id="pl",
        provider="claude",
        kind="plan",
        tool="ExitPlanMode",
        input={"plan": "1. Do it"},
        plan="1. Do it",
    )
    record = opened_record(ask, at=_AT)
    assert record.content == "Claude asks to approve its plan"
    card = from_plain(record.extra["$interaction"], dict[str, PlainTree])
    assert (card["type"], card["plan"], card["questions"]) == ("plan", "1. Do it", None)


@pytest.mark.parametrize(
    ("kind", "card_type"),
    [
        ("permission", "permission"),
        ("command", "permission"),
        ("file_change", "permission"),
        ("permissions", "permission"),
        ("elicitation", "permission"),
        ("dialog", "permission"),
        ("question", "question"),
        ("plan", "plan"),
        ("trust", "trust"),
    ],
)
def test_card_type_is_the_one_the_web_card_knows(
    kind: AskKind,
    card_type: str,
) -> None:
    ask = Ask(id="k", provider="codex", kind=kind)
    card = from_plain(
        opened_record(ask, at=_AT).extra["$interaction"],
        dict[str, PlainTree],
    )
    assert (card["kind"], card["type"]) == (kind, card_type)


def test_content_falls_back_to_the_title_without_a_tool() -> None:
    ask = Ask(id="d", provider="native", kind="dialog", title="Sign in first")
    assert opened_record(ask, at=_AT).content == "Native asks: Sign in first"


def test_content_without_tool_or_title_still_reads() -> None:
    ask = Ask(id="d", provider="codex", kind="dialog")
    assert opened_record(ask, at=_AT).content == "Codex asks for an answer"


def test_a_diff_and_a_terminal_only_ask_are_carried() -> None:
    ask = Ask(
        id="f",
        provider="codex",
        kind="file_change",
        diff="@@ -1 +1 @@",
        answerable_in_chat=False,
    )
    card = from_plain(
        opened_record(ask, at=_AT).extra["$interaction"],
        dict[str, PlainTree],
    )
    assert (card["diff"], card["answerable_in_chat"]) == ("@@ -1 +1 @@", False)


def test_duplicate_option_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="allow_once"):
        _ = Ask(
            id="x",
            provider="claude",
            kind="permission",
            options=(_OPTIONS[0], _OPTIONS[0]),
        )


def test_unknown_option_kind_is_refused_on_a_read() -> None:
    with pytest.raises(ValueError, match="kind"):
        _ = from_plain({"id": "a", "label": "b", "kind": "maybe"}, Option)


@pytest.mark.parametrize("record", [Option, Ask, Answer, Resolved, StateChanged])
def test_records_are_keyword_only(record: type) -> None:
    kinds = {each.kind for each in inspect.signature(record).parameters.values()}
    assert kinds == {inspect.Parameter.KEYWORD_ONLY}


def test_resolved_record_carries_the_option_and_who() -> None:
    resolved = _command_ask().resolve("allow_session", by="chat", who="jp")
    record = resolved_record(resolved, at=_AT)
    assert record.subtype == "interaction_resolved"
    assert record.timestamp == _AT.isoformat()
    assert record.content == "Allowed from Chat"
    assert from_plain(record.extra["$interaction_resolved"], dict[str, PlainTree]) == {
        "id": "a1",
        "option": "allow_session",
        "by": "chat",
        "who": "jp",
        "decision": "allow",
    }


@pytest.mark.parametrize(
    ("option", "by", "decision", "words"),
    [
        ("allow_once", "terminal", "allow", "Allowed in the terminal"),
        ("deny", "chat", "deny", "Denied from Chat"),
        ("abort", "chat", "deny", "Denied from Chat"),
        ("", "run", "released", "Released by the run"),
        ("", "released", "released", "Released"),
        ("", "timeout", "released", "Released on timeout"),
        ("other", "chat", "answered", "Answered from Chat"),
    ],
)
def test_resolve_names_the_decision_the_web_card_knows(
    option: str,
    by: Resolver,
    decision: str,
    words: str,
) -> None:
    resolved = _command_ask().resolve(option, by=by)
    record = resolved_record(resolved, at=_AT)
    assert (
        from_plain(record.extra["$interaction_resolved"], dict[str, PlainTree])[
            "decision"
        ]
        == decision
    )
    assert record.content == words


_CODEX_OPTIONS: Final = (
    Option(id="accept", label="Yes", kind="allow_once"),
    Option(id="decline", label="No", kind="deny"),
)


@pytest.mark.parametrize(
    ("decision", "option", "resolved"),
    [("allow", "accept", "allow"), ("deny", "decline", "deny")],
)
def test_an_old_answer_finds_the_agents_own_option(
    decision: str,
    option: str,
    resolved: str,
) -> None:
    ask = Ask(id="c", provider="codex", kind="command", options=_CODEX_OPTIONS)
    answer = answer_from_payload({"prompt_id": "c", "decision": decision})
    chosen = ask.chosen(answer.option)
    assert chosen is not None
    assert chosen.id == option
    end = ask.resolve(answer.option, by="chat")
    assert (end.option, end.decision) == (option, resolved)


def test_an_option_is_found_by_its_id_before_its_kind() -> None:
    ask = Ask(
        id="c",
        provider="codex",
        kind="command",
        options=(
            Option(id="x", label="Yes", kind="deny"),
            Option(id="deny", label="No", kind="allow_once"),
        ),
    )
    assert [ask.chosen(each) for each in ("deny", "x", "allow_once", "y")] == [
        ask.options[1],
        ask.options[0],
        ask.options[1],
        None,
    ]


def test_a_skipped_question_is_denied_not_answered() -> None:
    ask = Ask(
        id="q",
        provider="claude",
        kind="question",
        tool="AskUserQuestion",
        options=(
            Option(id="allow_once", label="Answer", kind="allow_once"),
            Option(id="deny", label="Skip", kind="deny"),
        ),
    )
    record = resolved_record(ask.resolve("deny", by="chat"), at=_AT)
    card = from_plain(record.extra["$interaction_resolved"], dict[str, PlainTree])
    assert (card["option"], card["decision"]) == ("deny", "deny")
    assert record.content == "Denied from Chat"
    assert ask.resolve("allow_once", by="chat").decision == "answered"


def test_a_question_is_answered_not_allowed() -> None:
    ask = Ask(id="q", provider="claude", kind="question", tool="AskUserQuestion")
    resolved = ask.resolve("allow_once", by="chat")
    assert resolved.decision == "answered"


def test_a_resolution_built_by_hand_reads_as_answered() -> None:
    resolved = Resolved(ask_id="a", option="x", by="chat")
    card = from_plain(
        resolved_record(resolved, at=_AT).extra["$interaction_resolved"],
        dict[str, PlainTree],
    )
    assert (card["decision"], card["who"]) == ("answered", "")


def test_a_resolution_by_the_run_reads_as_released() -> None:
    resolved = Resolved(ask_id="a", option="", by="run")
    card = from_plain(
        resolved_record(resolved, at=_AT).extra["$interaction_resolved"],
        dict[str, PlainTree],
    )
    assert card["decision"] == "released"


def test_new_answer_payload() -> None:
    answer = answer_from_payload(
        {
            "prompt_id": "a1",
            "option": "allow_once",
            "updated_input": {"answers": {"Pick a color": "red"}},
            "message": "ok",
        },
    )
    assert answer == Answer(
        ask_id="a1",
        option="allow_once",
        updated_input={"answers": {"Pick a color": "red"}},
        message="ok",
    )


def test_new_answer_payload_defaults() -> None:
    answer = answer_from_payload({"prompt_id": "a1", "option": "deny"})
    assert answer == Answer(ask_id="a1", option="deny")
    assert (answer.updated_input, answer.message, answer.who) == (None, "", "")


@pytest.mark.parametrize(
    ("decision", "option"),
    [("allow", "allow_once"), ("deny", "deny")],
)
def test_old_answer_payload_is_an_alias(decision: str, option: str) -> None:
    answer = answer_from_payload(
        {"prompt_id": "a1", "decision": decision, "message": "no"},
    )
    assert answer == Answer(ask_id="a1", option=option, message="no")


def test_old_answer_payload_as_todays_web_sends_it() -> None:
    answer = answer_from_payload(
        {"prompt_id": "a1", "decision": "allow", "updated_input": None, "message": ""},
    )
    assert answer == Answer(ask_id="a1", option="allow_once")


def test_old_answer_payload_keeps_updated_input() -> None:
    answer = answer_from_payload(
        {"prompt_id": "a1", "decision": "allow", "updated_input": {"answers": {}}},
    )
    assert answer.updated_input == {"answers": {}}


_NO_PROMPT: Final = "^an answer must name a prompt_id$"
_NO_OPTION: Final = "^an answer must name an option$"
_BOTH: Final = "^an answer must name an option or a decision, not both$"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "prompt_id"),
        ({"prompt_id": "a1"}, _NO_OPTION),
        ({"option": "deny"}, "prompt_id"),
        ({"prompt_id": "a1", "option": "deny", "decision": "deny"}, _BOTH),
        ({"prompt_id": "a1", "option": ""}, _NO_OPTION),
        ({"prompt_id": "", "option": "deny"}, _NO_PROMPT),
        ({"prompt_id": "a1", "decision": "maybe"}, "decision"),
        ({"prompt_id": "a1", "option": 3}, "option"),
        ({"prompt_id": 1, "option": "deny"}, "prompt_id"),
        ({"prompt_id": "a1", "option": "deny", "updated_input": [1]}, "updated_input"),
        ({"prompt_id": "a1", "option": "deny", "message": 4}, "message"),
        ({"prompt_id": "a1", "option": "deny", "extra": 1}, "extra"),
    ],
)
def test_bad_payloads_are_refused(
    payload: Mapping[str, PlainTree],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _ = answer_from_payload(payload)


class _Fake:
    """A driver that asks once, takes the answer, and settles."""

    def __init__(self) -> None:
        self.answers: list[Answer] = []
        self._events: asyncio.Queue[Ask | Resolved | StateChanged | None] = (
            asyncio.Queue()
        )

    async def start(self, *, cwd: Path, resume: str, model: str, effort: str) -> str:
        del cwd, model, effort
        return resume or "thread-1"

    async def send_line(self, text: str) -> None:
        del text
        await self._events.put(StateChanged(state="running"))
        await self._events.put(_command_ask())
        await self._events.put(StateChanged(state="requires_action"))

    async def command(self, verb: str, args: str) -> None:
        del verb, args

    async def answer(self, answer: Answer) -> None:
        self.answers.append(answer)
        await self._events.put(_command_ask().resolve(answer.option, by="chat"))
        await self._events.put(StateChanged(state="idle"))
        await self._events.put(None)

    async def interrupt(self) -> None:
        await self._events.put(None)

    async def settings(self, *, model: str, effort: str) -> None:
        del model, effort

    async def stop(self) -> None:
        await self._events.put(None)

    async def events(self) -> AsyncIterator[Ask | Resolved | StateChanged]:
        while (event := await self._events.get()) is not None:
            yield event


async def _drive(driver: Driver) -> list[Ask | Resolved | StateChanged]:
    session = await driver.start(cwd=Path("/work"), resume="", model="m", effort="low")
    assert session == "thread-1"
    await driver.send_line("hi")
    seen: list[Ask | Resolved | StateChanged] = []
    async for event in driver.events():
        seen.append(event)
        if isinstance(event, Ask):
            await driver.answer(
                answer_from_payload({"prompt_id": event.id, "option": "deny"}),
            )
    return seen


def test_a_driver_streams_asks_resolutions_and_states() -> None:
    seen = asyncio.run(_drive(_Fake()))
    assert [type(each).__name__ for each in seen] == [
        "StateChanged",
        "Ask",
        "StateChanged",
        "Resolved",
        "StateChanged",
    ]
    resolved = seen[3]
    assert isinstance(resolved, Resolved)
    assert (resolved.ask_id, resolved.option, resolved.decision) == (
        "a1",
        "deny",
        "deny",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
