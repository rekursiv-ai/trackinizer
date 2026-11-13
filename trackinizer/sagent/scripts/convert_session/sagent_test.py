"""sagent -> IR: today's journal reads back as written; an old log stays uncategorized."""

from __future__ import annotations

from io import StringIO
from typing import Final, cast

import inspect
import json

from treekle import immutable

import pytest

from trackinizer.sagent.scripts.convert_session import sagent
from trackinizer.sagent.scripts.convert_session.convert import detect_format, main
from trackinizer.sagent.session.jsonl import record_line
from trackinizer.sagent.session.types import (
    AssistantMessage,
    ContextClear,
    IncompleteRecord,
    SessionRecord,
    ToolCall,
    ToolResult,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
)


def _read(*lines: dict[str, object] | str) -> list[SessionRecord]:
    """Normalize ``lines`` written as one sagent session file."""
    text = "".join(
        (line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines
    )
    return list(sagent.normalize(StringIO(text)))


_JOURNAL: Final = (
    ContextClear(timestamp="2026-10-31T00:00:00Z", system_prompt="", history=()),
    UserMessage(content="hello", extra={"$inbound": {"source": "bob"}}),
    ToolCall(call_id="c1", name="Bash", arguments={"command": "ls"}),
    ToolResult(
        call_id="c1",
        state="done",
        stdout=("a b",),
        result=UncategorizedToolResult(),
    ),
    AssistantMessage(content="hi"),
)
"""Records as today's sagent journals them: one IR record a line."""


def test_a_journal_today_s_sagent_writes_reads_back_record_for_record() -> None:
    """A sagent run is captured through this reader, so nothing may be retyped."""
    text = b"".join(record_line(record) for record in _JOURNAL).decode()

    assert list(sagent.normalize(StringIO(text))) == list(_JOURNAL)


def test_a_journal_today_s_sagent_writes_is_detected_as_sagent() -> None:
    head = record_line(_JOURNAL[0]).decode()

    assert sagent.is_sagent(head)
    assert detect_format(head) == "sagent"


def test_records_written_as_a_journal_read_back_as_they_were() -> None:
    """A stored conversation is handed back to sagent this way, to resume."""
    torn = IncompleteRecord(text='{"py/object": "UserMess\n')
    out = StringIO()

    sagent.denormalize([*_JOURNAL, torn], out)

    assert out.getvalue() == (
        b"".join(record_line(record) for record in _JOURNAL).decode() + torn.text
    )
    assert list(sagent.normalize(StringIO(out.getvalue()))) == [*_JOURNAL, torn]


def test_a_torn_journal_line_keeps_its_bytes() -> None:
    """A run read mid-write sees half a line; it must not cost the lines after it."""
    whole = record_line(_JOURNAL[1]).decode()
    torn = whole[: len(whole) // 2] + "\n"

    records = list(sagent.normalize(StringIO(torn + whole)))

    assert records == [IncompleteRecord(text=torn), _JOURNAL[1]]


def test_a_line_that_is_not_json_is_kept_verbatim_and_a_blank_one_is_nothing() -> None:
    assert _read("", "{truncated", "[1]") == [
        IncompleteRecord(text="{truncated\n"),
        IncompleteRecord(text="[1]\n"),
    ]


def test_an_old_sagent_s_records_are_kept_whole_and_uncategorized() -> None:
    """Only ``legacy_journal`` reads them; ``upgrade`` makes them today's."""
    old: list[dict[str, object]] = [
        {"kind": "history", "type": "user", "text": "hi"},
        {"kind": "", "note": "x"},
        {"other": 1},
    ]

    assert _read(*old) == [
        UncategorizedRecord(kind="history", payload=immutable(old[0])),
        UncategorizedRecord(kind="unknown", payload=immutable(old[1])),
        UncategorizedRecord(kind="unknown", payload=immutable(old[2])),
    ]


def test_convert_names_the_format_and_offers_it() -> None:
    """An old sagent log is detected as sagent, never as codex, and is a target."""
    head = json.dumps({"kind": "context_override", "payload": {}}) + "\n"

    offered = cast(
        tuple[str, ...],
        inspect.signature(main).parameters["formats"].default,
    )

    assert detect_format(head) == "sagent"
    assert "sagent" in offered


def test_sagent_is_recognized_by_its_record_kinds() -> None:
    assert sagent.is_sagent(json.dumps({"kind": "meta"}) + "\n")
    assert sagent.is_sagent(
        json.dumps({"kind": "message", "role": "user", "content": "x"}) + "\n",
    )
    assert not sagent.is_sagent(
        json.dumps({"type": "user", "sessionId": "x", "uuid": "u"}) + "\n",
    )
    assert sagent.is_sagent("\n" + json.dumps({"kind": "meta"}) + "\n")
    assert not sagent.is_sagent("{truncated\n")
    assert not sagent.is_sagent(json.dumps({"kind": ["meta"]}) + "\n")
    assert not sagent.is_sagent(json.dumps(["kind", "meta"]) + "\n")
    assert not sagent.is_sagent("")


@pytest.mark.parametrize(
    "kind",
    [
        "meta",
        "history",
        "tool_state",
        "context_override",
        "context_splice",
        "persistent_agent",
        "runtime_event",
        "event",
        "update",
        "clear",
    ],
)
def test_each_record_kind_only_sagent_writes_names_it(kind: str) -> None:
    assert sagent.is_sagent(json.dumps({"kind": kind}) + "\n")


def test_a_message_names_sagent_only_with_a_descriptor_or_a_role() -> None:
    assert sagent.is_sagent(json.dumps({"kind": "message", "descriptor": "x"}) + "\n")
    assert not sagent.is_sagent(json.dumps({"kind": "message"}) + "\n")
    assert not sagent.is_sagent(json.dumps({"kind": "other", "role": "user"}) + "\n")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
