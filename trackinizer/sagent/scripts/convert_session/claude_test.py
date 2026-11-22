"""Tests for Claude session streams."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import base64
import binascii
import json

from treekle import ReadError, from_plain, json_loads

import pytest

from trackinizer.sagent.scripts.convert_session import claude
from trackinizer.sagent.scripts.convert_session.claude import _group
from trackinizer.sagent.scripts.convert_session.shell_results import (
    rewrite_shell_source,
)
from trackinizer.sagent.scripts.convert_session.tail import Tail
from trackinizer.sagent.scripts.convert_session.testdata.mistype import (
    field_paths,
    mistyped,
    unread,
    without,
)
from trackinizer.sagent.session.types import (
    AgentMessage,
    AgentSpawnResult,
    AssistantMessage,
    Attachment,
    BashResult,
    ContextClear,
    ContextCompaction,
    ContextState,
    EditResult,
    IncompleteRecord,
    McpResult,
    ReadResult,
    SessionContext,
    SessionRecord,
    SkillResult,
    Splice,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    ToolMessage,
    TranscriptItem,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchHit,
    WebSearchResult,
    WriteResult,
    former_kind,
    stored_plain,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from treekle import PlainTree


_CWD: Final[Path] = Path(__file__).resolve().parent


ENVELOPE = (
    '"userType":"external","cwd":"/workspace","sessionId":"s1","version":"2.1.241"'
)
FIXTURES: Final = {
    name: (_CWD / "testdata" / name).read_text().splitlines(keepends=True)
    for name in ("claude_main.jsonl", "claude_sidechain.jsonl")
}
"""Real sessions, one line per element, by file name."""

_KEPT_AS_TEXT: Final = frozenset(
    {
        "permissionMode",
        "message.model",
        "message.content",
        "message.content.0.type",
        "message.content.0.tool_use_id",
        "message.content.0.content",
        "toolUseResult.results.0.content",
        "attachment",
        "attachment.type",
    },
)
"""Fields whose wrong value only the line's own text can write back.

Read as absent, the line would be rewritten with a different value, so the line is
kept as text instead.
"""


def _line(**fields: str) -> str:
    """Return one claude record line with the session envelope appended."""
    body = ",".join(f'"{key}":{value}' for key, value in fields.items())
    return "{" + body + "," + ENVELOPE + "}\n"


def _transcript(records: Iterable[SessionRecord]) -> tuple[TranscriptItem, ...]:
    """Return the conversation and tool records among ``records``."""
    return tuple(
        record
        for record in records
        if isinstance(
            record,
            UserMessage | AssistantMessage | Thinking | ToolCall | ToolMessage,
        )
    )


def _result(record: object) -> ToolMessage:
    """Return ``record`` after asserting it is a ``ToolMessage``."""
    assert isinstance(record, ToolMessage)
    return record


def _typed_result[T](record: object, kind: type[T]) -> T:
    """Return ``record.result`` after asserting it is an instance of ``kind``."""
    tool_result = _result(record).result
    assert isinstance(tool_result, kind)
    return tool_result


# The reader restates the file's spelling as a trailing :class:`SessionContext` whenever it
# changes -- the final record of a stream may be that state, not the act a line carried.
def _last_act(records: Sequence[SessionRecord]) -> SessionRecord:
    """Return the last record that is not a restatement of the settings."""
    return next(
        record for record in reversed(records) if not isinstance(record, SessionContext)
    )


def test_a_user_turn_normalizes_to_a_user_message() -> None:
    native = _line(
        parentUuid="null",
        type='"user"',
        message='{"role":"user","content":"Inspect it."}',
        uuid='"u1"',
        timestamp='"2026-08-24T00:00:00.000Z"',
    )

    records = list(claude.normalize(StringIO(native)))

    message = _transcript(records)[0]
    assert isinstance(message, UserMessage)
    assert message.content == "Inspect it."
    assert message.extra["uuid"] == "u1"


def test_a_session_declares_its_context_first() -> None:
    # Every record names its settings by index, so the context has to lead.
    native = _line(
        type='"user"',
        message='{"role":"user","content":"Hi."}',
        uuid='"u1"',
    )

    records = list(claude.normalize(StringIO(native)))

    # Index 2: every session opens with the settings in force and the
    # ``ContextClear`` that states the context it begins from, and the turn's
    # own settings follow them.
    assert isinstance(records[1], ContextClear)
    context = records[2]
    assert isinstance(context, SessionContext)
    assert context.extra["sessionId"] == "s1"
    assert context.extra["cwd"] == "/workspace"
    assert context.extra["version"] == "2.1.241"


def test_an_assistant_turn_splits_into_one_record_per_act() -> None:
    # Thinking, text, and a tool call arrive on one line but are three acts;
    # keeping them nested would make a tool call unfindable without walking
    # every message's content.
    native = _line(
        type='"assistant"',
        uuid='"a1"',
        requestId='"req1"',
        message=(
            '{"model":"claude-opus-5","id":"msg1","type":"message",'
            '"role":"assistant","content":['
            '{"type":"thinking","thinking":"Reading.","signature":"sig"},'
            '{"type":"text","text":"Here."},'
            '{"type":"tool_use","id":"c1","name":"Read","input":{"path":"/a"}}]}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))

    # In the order the blocks appeared, which is not always prose-first: this
    # line thinks, then speaks, then calls.
    assert [type(r).__name__ for r in _transcript(records)] == [
        "Thinking",
        "AssistantMessage",
        "ToolCall",
    ]


def test_the_first_act_of_a_line_carries_what_the_line_reported() -> None:
    # Axiom 10: one line becomes several records, and only the first holds the
    # line's residual -- so the writer rebuilds one line, not three.
    native = _line(
        type='"assistant"',
        uuid='"a1"',
        requestId='"req1"',
        message=(
            '{"model":"claude-opus-5","id":"msg1","type":"message",'
            '"role":"assistant","content":['
            '{"type":"thinking","thinking":"Reading.","signature":"s"},'
            '{"type":"tool_use","id":"c1","name":"Read","input":{}}],'
            '"stop_reason":"end_turn"}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))

    acts = _transcript(records)
    assert [type(r).__name__ for r in acts] == ["Thinking", "ToolCall"]
    assert acts[0].extra["requestId"] == "req1"
    assert acts[0].extra["uuid"] == "a1"
    assert not [key for key in acts[1].extra if not key.startswith("$")]
    # The model is the turn's setting, not the line's: it never varies within
    # a session, so it lives once on the context.
    context = records[2]
    assert isinstance(context, SessionContext)
    assert context.model == "claude-opus-5"


def test_a_tool_result_keeps_the_text_the_model_read() -> None:
    native = _line(
        type='"user"',
        uuid='"u1"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"boom","is_error":true}]}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))

    result = _result(_transcript(records)[0])
    assert _typed_result(result, UncategorizedToolResult) == UncategorizedToolResult()
    assert result.call_id == "c1"
    assert "".join(text for text in result.stdout if isinstance(text, str)) == "boom"


def test_an_image_result_decodes_to_its_bytes() -> None:
    native = _line(
        type='"user"',
        uuid='"u1"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":[{"type":"image","source":'
            '{"type":"base64","data":"aGk=","media_type":"image/png"}}]}]}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))

    result = _result(_transcript(records)[0])
    assert _typed_result(result, UncategorizedToolResult) == UncategorizedToolResult()
    assert result.attachments == (Attachment(mime_descriptor="image/png", data=b"hi"),)


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        (
            (
                '{"type":"attachment","attachment":{"type":"total_tokens_reminder",'
                '"text":"left"},"uuid":"x1"}'
            ),
            ContextState,
        ),
        (
            (
                '{"type":"system","subtype":"turn_duration","durationMs":5,'
                '"timestamp":null,"uuid":"y1","isMeta":true}'
            ),
            SystemMessage,
        ),
        (
            (
                '{"type":"queue-operation","operation":"enqueue","timestamp":null,'
                '"sessionId":"s1","content":"Go."}'
            ),
            UncategorizedRecord,
        ),
        (
            '{"type":"ai-title","aiTitle":"A session","sessionId":"s1"}',
            UncategorizedRecord,
        ),
    ],
    ids=["attachment", "system", "queue", "ai-title"],
)
def test_a_record_outside_the_transcript_maps_to_its_own_type(
    native: str,
    expected: type[SessionRecord],
) -> None:
    # A line only claude writes -- its queue, its identity lines -- stays
    # uncategorized: a type for it would be one no other provider can fill.
    records = list(claude.normalize(StringIO(native + "\n")))

    assert isinstance(_last_act(records), expected)


def test_a_system_line_reads_its_time_text_and_kind() -> None:
    native = (
        '{"type":"system","subtype":"informational","content":"Hook ran",'
        '"timestamp":"2026-08-24T20:29:52.614Z","uuid":"y1"}\n'
    )

    message = _last_act(list(claude.normalize(StringIO(native))))

    assert isinstance(message, SystemMessage)
    assert (message.timestamp, message.content, message.subtype) == (
        datetime(2026, 8, 24, 20, 29, 52, 614_000, UTC),
        "Hook ran",
        "informational",
    )
    # Read into fields, so not kept a second time in the residual.
    assert not {"content", "subtype"} & set(message.extra)
    assert message.extra["$timestamp_as_written"] == "2026-08-24T20:29:52.614Z"


def test_every_record_of_a_line_keeps_the_time_as_the_line_spelled_it() -> None:
    """A line's call and its usage are records of their own, each stored at its time."""
    native = (
        '{"type":"assistant","timestamp":"2026-08-24T20:29:52.614Z","uuid":"a1",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"on it"},'
        '{"type":"tool_use","id":"c1","name":"Read","input":{}}],'
        '"usage":{"input_tokens":1}}}\n'
        '{"type":"queue-operation","timestamp":"soon","operation":"enqueue"}\n'
    )

    records = [
        r
        for r in claude.normalize(StringIO(native))
        if isinstance(r, AssistantMessage | ToolCall | TokenUsage | UncategorizedRecord)
    ]

    assert [(type(r), stored_plain(r)["timestamp"]) for r in records] == [
        (AssistantMessage, "2026-08-24T20:29:52.614Z"),
        (ToolCall, "2026-08-24T20:29:52.614Z"),
        (TokenUsage, "2026-08-24T20:29:52.614Z"),
        (UncategorizedRecord, "soon"),
    ]
    output = StringIO()
    claude.denormalize(claude.normalize(StringIO(native)), output)
    assert output.getvalue() == native


def test_a_line_with_no_time_writes_a_null_only_where_it_stated_one() -> None:
    stated = '{"type":"system","content":"x","timestamp":null,"uuid":"y1"}\n'
    assert _round_trip(stated) == stated

    timed = stated.replace("null", '"2026-08-24T20:29:52.614Z"')
    cleared = [
        replace(record, timestamp=None) if isinstance(record, SystemMessage) else record
        for record in claude.normalize(StringIO(timed))
    ]
    output = StringIO()
    claude.denormalize(cleared, output)
    assert output.getvalue() == '{"type":"system","content":"x","uuid":"y1"}\n'


def test_a_queued_message_is_its_contexts_prose() -> None:
    # A message sent while the agent worked arrives as a ``queued_command``
    # attachment with its text under ``prompt``. Read from ``text`` or
    # ``content`` only, the message reached the IR as a residual alone: no
    # search found it and no viewer showed it.
    native = _line(
        type='"attachment"',
        attachment=(
            '{"type":"queued_command","prompt":"[room-a] someone: Go ahead.",'
            '"commandMode":"prompt","origin":{"kind":"human"}}'
        ),
        uuid='"q1"',
        timestamp='"2026-10-01T13:00:24.494Z"',
    )

    state = _last_act(list(claude.normalize(StringIO(native))))

    assert isinstance(state, ContextState)
    assert (state.kind, state.content) == (
        "queued_command",
        "[room-a] someone: Go ahead.",
    )
    assert _round_trip(native) == native


@pytest.mark.parametrize(
    ("tool", "result", "expected"),
    [
        ("Bash", '{"stdout":"hi","stderr":"","interrupted":false}', BashResult),
        ("Read", '{"file":{"filePath":"/a"},"type":"text"}', ReadResult),
        ("Write", '{"filePath":"/a","content":"body"}', WriteResult),
        ("Edit", '{"filePath":"/a","replaceAll":false}', EditResult),
        ("WebSearch", '{"query":"q","durationSeconds":1.5}', WebSearchResult),
        ("WebFetch", '{"url":"https://x","result":"body"}', WebFetchResult),
        ("Agent", '{"agentId":"a1","status":"done"}', AgentSpawnResult),
        ("Skill", '{"commandName":"tdd","success":true}', SkillResult),
        ("ToolSearch", '{"matches":[]}', UncategorizedToolResult),
        ("mcp__docs__search", '{"hits":[]}', McpResult),
    ],
    ids=[
        "bash",
        "read",
        "write",
        "edit",
        "search",
        "fetch",
        "agent",
        "skill",
        "uncategorized",
        "mcp",
    ],
)
def test_a_result_is_typed_by_what_its_tool_did(
    tool: str,
    result: str,
    expected: type[object],
) -> None:
    # Dispatch is on the tool NAME, which only the call carries -- so the
    # result two lines later can only be typed by looking back at it.
    native = _line(
        type='"assistant"',
        uuid='"a1"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"' + tool + '","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        uuid='"u1"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=result,
    )

    records = list(claude.normalize(StringIO(native)))

    assert type(_result(_last_act(records)).result) is expected


def _stated(
    order: Sequence[str],
    states: Mapping[str, str],
    residual: Mapping[str, PlainTree],
    raw: Mapping[str, PlainTree] | None = None,
) -> dict[str, PlainTree]:
    """Return what a record keeps of a payload whose modeled fields it read."""
    return {
        "$__custom_json_fields__": {
            "version": 1,
            "order": list(order),
            "states": dict(states),
            "raw": {} if raw is None else dict(raw),
            "residual": dict(residual),
        },
    }


def _kept(name: str, stored: PlainTree, **shape: PlainTree) -> dict[str, PlainTree]:
    """Return the ``extra`` of ``name``'s result, read beside one line field."""
    return {
        "line": 1,
        "$result": {"tool_name": name, **shape},
        "toolUseResult": stored,
    }


@pytest.mark.parametrize(
    ("name", "payload", "text", "expected"),
    [
        pytest.param(
            "Bash",
            {"stdout": "out", "stderr": "err", "interrupted": False, "note": 1},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("out",),
                stderr=("err",),
                result=BashResult(),
                extra=_kept(
                    "Bash",
                    _stated(
                        ["stdout", "stderr", "interrupted", "note"],
                        {"stdout": "value", "stderr": "value"},
                        {"interrupted": False, "note": 1},
                    ),
                ),
            ),
            id="bash",
        ),
        pytest.param(
            "Bash",
            {"stdout": 1, "stderr": 2},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=BashResult(),
                extra=_kept(
                    "Bash",
                    _stated(["stdout", "stderr"], {}, {"stdout": 1, "stderr": 2}),
                ),
            ),
            id="bash-mistyped",
        ),
        pytest.param(
            "Bash",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=BashResult(),
                extra=_kept("Bash", _stated([], {}, {})),
            ),
            id="bash-absent",
        ),
        pytest.param(
            "Read",
            {
                "file": {"filePath": "/a.py", "content": "body", "numLines": 1},
                "type": "text",
            },
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("body",),
                result=ReadResult(path="/a.py"),
                extra=_kept(
                    "Read",
                    {
                        "type": "text",
                        "file": _stated(
                            ["filePath", "content", "numLines"],
                            {"filePath": "value", "content": "value"},
                            {"numLines": 1},
                        ),
                    },
                ),
            ),
            id="read",
        ),
        pytest.param(
            "Read",
            {"file": {"filePath": 1, "content": 2}},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=ReadResult(),
                extra=_kept(
                    "Read",
                    {
                        "file": _stated(
                            ["filePath", "content"],
                            {},
                            {"filePath": 1, "content": 2},
                        ),
                    },
                ),
            ),
            id="read-mistyped",
        ),
        pytest.param(
            "Read",
            {"file": 3, "type": "text"},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=ReadResult(),
                extra=_kept(
                    "Read",
                    _stated(["file", "type"], {}, {"file": 3, "type": "text"}),
                ),
            ),
            id="read-no-file",
        ),
        pytest.param(
            "Read",
            {"file": None},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=ReadResult(),
                extra=_kept("Read", _stated(["file"], {"file": "null"}, {})),
            ),
            id="read-null-file",
        ),
        pytest.param(
            "Write",
            {"filePath": "/a.py", "content": "body", "type": "create"},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Wrote /a.py.",),
                result=WriteResult(path="/a.py", content="body"),
                extra=_kept(
                    "Write",
                    _stated(
                        ["filePath", "content", "type"],
                        {"filePath": "value", "content": "value"},
                        {"type": "create"},
                    ),
                ),
            ),
            id="write",
        ),
        pytest.param(
            "Write",
            {"filePath": 1, "content": 2},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Wrote the file.",),
                result=WriteResult(),
                extra=_kept(
                    "Write",
                    _stated(
                        ["filePath", "content"],
                        {},
                        {"filePath": 1, "content": 2},
                    ),
                ),
            ),
            id="write-mistyped",
        ),
        pytest.param(
            "Write",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Wrote the file.",),
                result=WriteResult(),
                extra=_kept("Write", _stated([], {}, {})),
            ),
            id="write-absent",
        ),
        pytest.param(
            "Edit",
            {
                "filePath": "/a.py",
                "oldString": "old",
                "newString": "new",
                "structuredPatch": [],
            },
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Edited /a.py.", Splice(before="old", after="new")),
                result=EditResult(path="/a.py"),
                extra=_kept(
                    "Edit",
                    _stated(
                        ["filePath", "oldString", "newString", "structuredPatch"],
                        {
                            "filePath": "value",
                            "oldString": "value",
                            "newString": "value",
                        },
                        {"structuredPatch": []},
                    ),
                    splice=["old", "new"],
                ),
            ),
            id="edit",
        ),
        pytest.param(
            "Edit",
            {"filePath": 1, "oldString": 2, "newString": 3, "structuredPatch": []},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Edited the file.",),
                result=EditResult(),
                extra=_kept(
                    "Edit",
                    _stated(
                        ["filePath", "oldString", "newString", "structuredPatch"],
                        {},
                        {
                            "filePath": 1,
                            "oldString": 2,
                            "newString": 3,
                            "structuredPatch": [],
                        },
                    ),
                    splice=[None, None],
                ),
            ),
            id="edit-mistyped",
        ),
        pytest.param(
            "Edit",
            {"oldString": "old"},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Edited the file.", Splice(before="old")),
                result=EditResult(),
                extra=_kept(
                    "Edit",
                    _stated(["oldString"], {"oldString": "value"}, {}),
                ),
            ),
            id="edit-only-what-it-took-out",
        ),
        pytest.param(
            "Edit",
            {"newString": "new"},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("Edited the file.", Splice(after="new")),
                result=EditResult(),
                extra=_kept(
                    "Edit",
                    _stated(["newString"], {"newString": "value"}, {}),
                ),
            ),
            id="edit-only-what-it-put-in",
        ),
        pytest.param(
            "WebSearch",
            {
                "query": "q",
                "durationSeconds": 1.5,
                "results": [
                    {
                        "tool_use_id": "s1",
                        "content": [
                            {"title": "T", "url": "https://x"},
                            "junk",
                            {"title": "U", "url": "https://y"},
                        ],
                    },
                    "prose",
                ],
            },
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=(
                    "T\nhttps://x\n\nU\nhttps://y",
                    WebSearchHit(url="https://x", title="T"),
                    WebSearchHit(url="https://y", title="U"),
                ),
                result=WebSearchResult(query="q", duration_sec=1.5),
                extra=_kept(
                    "WebSearch",
                    _stated(
                        ["query", "durationSeconds", "results"],
                        {
                            "query": "value",
                            "durationSeconds": "value",
                            "results": "value",
                        },
                        {},
                        {"durationSeconds": 1.5},
                    ),
                    rows=[
                        {
                            "template": {
                                "tool_use_id": "s1",
                                "content": [
                                    {"title": "T", "url": "https://x"},
                                    "junk",
                                    {"title": "U", "url": "https://y"},
                                ],
                            },
                            "rows": 2,
                        },
                        "prose",
                    ],
                ),
            ),
            id="search",
        ),
        pytest.param(
            "WebSearch",
            {"query": 1, "durationSeconds": "d", "results": 3},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=WebSearchResult(),
                extra=_kept(
                    "WebSearch",
                    _stated(
                        ["query", "durationSeconds", "results"],
                        {},
                        {"query": 1, "durationSeconds": "d", "results": 3},
                    ),
                    rows=[],
                ),
            ),
            id="search-mistyped",
        ),
        pytest.param(
            "WebSearch",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=WebSearchResult(),
                extra=_kept("WebSearch", _stated([], {}, {})),
            ),
            id="search-absent",
        ),
        pytest.param(
            "WebFetch",
            {
                "url": "https://x",
                "result": "body",
                "code": 200,
                "durationMs": 1500,
                "bytes": 4,
                "codeText": "OK",
            },
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("body",),
                result=WebFetchResult(
                    url="https://x",
                    code=200,
                    duration_sec=1.5,
                    size=4,
                ),
                extra=_kept(
                    "WebFetch",
                    _stated(
                        ["url", "result", "code", "durationMs", "bytes", "codeText"],
                        {
                            "url": "value",
                            "result": "value",
                            "code": "value",
                            "durationMs": "value",
                            "bytes": "value",
                        },
                        {"codeText": "OK"},
                        {"code": 200, "durationMs": 1500, "bytes": 4},
                    ),
                    duration_ms=1500,
                ),
            ),
            id="fetch",
        ),
        pytest.param(
            "WebFetch",
            {"url": 1, "result": 2, "code": "c", "durationMs": "d", "bytes": "b"},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=WebFetchResult(),
                extra=_kept(
                    "WebFetch",
                    _stated(
                        ["url", "result", "code", "durationMs", "bytes"],
                        {},
                        {
                            "url": 1,
                            "result": 2,
                            "code": "c",
                            "durationMs": "d",
                            "bytes": "b",
                        },
                    ),
                ),
            ),
            id="fetch-mistyped",
        ),
        pytest.param(
            "WebFetch",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=WebFetchResult(),
                extra=_kept("WebFetch", _stated([], {}, {})),
            ),
            id="fetch-absent",
        ),
        pytest.param(
            "Agent",
            {
                "agentId": "a1",
                "agentType": "general",
                "prompt": "do it",
                "content": [
                    {"type": "text", "text": "one"},
                    "junk",
                    {"type": "tool_use"},
                    {"type": "text", "text": "two"},
                ],
                "resolvedModel": "m",
                "status": "completed",
                "totalTokens": 7,
                "totalDurationMs": 2500,
                "totalToolUseCount": 3,
                "outputFile": "/o",
                "usage": {},
            },
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("one\n\ntwo",),
                result=AgentSpawnResult(
                    agent_id="a1",
                    agent_kind="general",
                    prompt="do it",
                    model="m",
                    state="completed",
                    tokens=7,
                    duration_sec=2.5,
                    tool_calls=3,
                    output_file="/o",
                ),
                extra=_kept(
                    "Agent",
                    _stated(
                        [
                            "agentId",
                            "agentType",
                            "prompt",
                            "content",
                            "resolvedModel",
                            "status",
                            "totalTokens",
                            "totalDurationMs",
                            "totalToolUseCount",
                            "outputFile",
                            "usage",
                        ],
                        {
                            "agentId": "value",
                            "agentType": "value",
                            "prompt": "value",
                            "content": "value",
                            "resolvedModel": "value",
                            "status": "value",
                            "totalTokens": "value",
                            "totalDurationMs": "value",
                            "totalToolUseCount": "value",
                            "outputFile": "value",
                        },
                        {"usage": {}},
                        {
                            "totalTokens": 7,
                            "totalDurationMs": 2500,
                            "totalToolUseCount": 3,
                        },
                    ),
                    duration_ms=2500,
                    agent_blocks=[
                        {"type": "text", "text": "one"},
                        "junk",
                        {"type": "tool_use"},
                        {"type": "text", "text": "two"},
                    ],
                ),
            ),
            id="agent",
        ),
        pytest.param(
            "Agent",
            {
                "agentId": 1,
                "agentType": 2,
                "prompt": 3,
                "content": "c",
                "resolvedModel": 4,
                "status": 5,
                "totalTokens": "t",
                "totalDurationMs": "d",
                "totalToolUseCount": "n",
                "outputFile": 6,
            },
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=AgentSpawnResult(),
                extra=_kept(
                    "Agent",
                    _stated(
                        [
                            "agentId",
                            "agentType",
                            "prompt",
                            "content",
                            "resolvedModel",
                            "status",
                            "totalTokens",
                            "totalDurationMs",
                            "totalToolUseCount",
                            "outputFile",
                        ],
                        {},
                        {
                            "agentId": 1,
                            "agentType": 2,
                            "prompt": 3,
                            "content": "c",
                            "resolvedModel": 4,
                            "status": 5,
                            "totalTokens": "t",
                            "totalDurationMs": "d",
                            "totalToolUseCount": "n",
                            "outputFile": 6,
                        },
                    ),
                ),
            ),
            id="agent-mistyped",
        ),
        pytest.param(
            "Agent",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=AgentSpawnResult(),
                extra=_kept("Agent", _stated([], {}, {})),
            ),
            id="agent-absent",
        ),
        pytest.param(
            "Skill",
            {"commandName": "tdd"},
            "ran",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("ran",),
                result=SkillResult(),
                extra=_kept("Skill", {"commandName": "tdd"}),
            ),
            id="skill",
        ),
        pytest.param(
            "Skill",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=SkillResult(),
                extra=_kept("Skill", {}),
            ),
            id="skill-silent",
        ),
        pytest.param(
            "mcp__docs__search__deep",
            {"hits": []},
            "found",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("found",),
                result=McpResult(server="docs", tool="search__deep"),
                extra=_kept("mcp__docs__search__deep", {"hits": []}),
            ),
            id="mcp",
        ),
        pytest.param(
            "mcp__docs__search",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=McpResult(server="docs", tool="search"),
                extra=_kept("mcp__docs__search", {}),
            ),
            id="mcp-silent",
        ),
        pytest.param(
            "ToolSearch",
            {"matches": []},
            "found",
            ToolMessage(
                call_id="c1",
                state="done",
                stdout=("found",),
                result=UncategorizedToolResult(),
                extra=_kept("ToolSearch", {"matches": []}),
            ),
            id="uncategorized",
        ),
        pytest.param(
            "ToolSearch",
            {},
            "",
            ToolMessage(
                call_id="c1",
                state="done",
                result=UncategorizedToolResult(),
                extra=_kept("ToolSearch", {}),
            ),
            id="uncategorized-silent",
        ),
    ],
)
def test_a_structured_result_reads_as_the_whole_record_of_what_its_tool_did(
    name: str,
    payload: dict[str, object],
    text: str,
    expected: ToolMessage,
) -> None:
    read = claude._typed_result(
        name,
        payload,
        {"toolUseResult": payload},
        call_id="c1",
        command=None,
        failed=False,
        text=text,
        attachments=(),
        extra={"line": 1},
        shape={"tool_name": name},
    )

    assert read == expected


def test_a_read_result_keeps_its_file_after_the_rest_of_its_payload() -> None:
    payload: dict[str, object] = {"file": {"filePath": "/a.py"}, "type": "text"}

    read = claude._typed_result(
        "Read",
        payload,
        {"toolUseResult": payload},
        call_id="c1",
        command=None,
        failed=False,
        text="",
        attachments=(),
        extra={},
        shape={},
    )

    assert list(from_plain(read.extra["toolUseResult"], dict[str, object])) == [
        "type",
        "file",
    ]


def test_a_result_with_no_payload_object_keeps_none() -> None:
    read = claude._typed_result(
        "ToolSearch",
        {},
        {"toolUseResult": "Error: no such tool"},
        call_id="c1",
        command=None,
        failed=True,
        text="Error: no such tool",
        attachments=(),
        extra={"line": 1},
        shape={"tool_name": "ToolSearch"},
    )

    assert read == ToolMessage(
        call_id="c1",
        state="done",
        stdout=("Error: no such tool",),
        result=UncategorizedToolResult(),
        extra={"line": 1, "$result": {"tool_name": "ToolSearch"}},
    )


@pytest.mark.parametrize(
    ("command", "content", "result_payload", "expected"),
    [
        (
            "/bin/cat a.txt",
            '"body\\n"',
            '{"stdout":"body\\n","stderr":"","interrupted":false}',
            ReadResult,
        ),
        (
            "/usr/bin/printf body > a.txt",
            '""',
            '{"stdout":"","stderr":"","interrupted":false}',
            WriteResult,
        ),
    ],
    ids=["read", "write"],
)
def test_a_successful_bash_file_operation_lifts_to_its_specific_type(
    command: str,
    content: str,
    result_payload: str,
    expected: type[object],
) -> None:
    native = _line(
        type='"assistant"',
        uuid='"a1"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"' + command + '"}}]}'
        ),
    ) + _line(
        type='"user"',
        uuid='"u1"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":' + content + "}]}"
        ),
        toolUseResult=result_payload,
    )

    records = list(claude.normalize(StringIO(native)))

    result = _result(_last_act(records))
    tool_result = result.result
    assert type(tool_result) is expected
    assert isinstance(tool_result, ReadResult | WriteResult | EditResult)
    assert tool_result.path == "a.txt"
    if isinstance(tool_result, ReadResult):
        assert (
            "".join(text for text in result.stdout if isinstance(text, str)) == "body\n"
        )
    if isinstance(tool_result, WriteResult):
        assert tool_result.content == "body"
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_a_bash_in_place_edit_names_its_file_without_claiming_a_diff() -> None:
    """``sed -i`` rewrites the file and prints nothing.

    So the record names WHICH file was edited -- that is knowable -- and states
    no splice, because the file's new bytes appear nowhere in the transcript
    and any before/after here would be invented.
    """
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/sed -i s/old/new/ a.txt"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":""}]}'
        ),
        toolUseResult='{"stdout":"","stderr":"","interrupted":false}',
    )

    records = list(claude.normalize(StringIO(native)))
    result = _result(_last_act(records))

    assert _typed_result(result, EditResult).path == "a.txt"
    # A regex program names no text it replaced, so it states no splice
    # rather than inventing a before and after.
    assert tuple(item for item in result.stdout if isinstance(item, Splice)) == ()
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_a_native_edit_carries_the_text_it_replaced() -> None:
    """Claude's Edit names the TEXT it replaced, never a line number.

    That is a whole edit -- enough to say what changed and to reverse it --
    so it becomes a :class:`Splice` on the record rather than staying an
    opaque residual. Position is absent because claude located the edit by
    content, and resolving one would need the file.
    """
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Edit","input":{"file_path":"/w/a.py"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        # Key order is claude's own: ``replaceAll`` trails the patch.
        toolUseResult=(
            '{"filePath":"/w/a.py","oldString":"price - pct",'
            '"newString":"price * (1 - pct / 100)",'
            '"structuredPatch":[{"oldStart":1,"oldLines":1,"newStart":1,'
            '"newLines":1,"lines":["-price - pct",'
            '"+price * (1 - pct / 100)"]}],"replaceAll":false}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))
    result = _result(_last_act(records))

    assert _typed_result(result, EditResult).path == "/w/a.py"
    assert tuple(item for item in result.stdout if isinstance(item, Splice)) == (
        Splice(before="price - pct", after="price * (1 - pct / 100)"),
    )
    assert (
        next(item for item in result.stdout if isinstance(item, Splice)).start is None
    )
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_editing_a_splice_rewrites_the_replayed_edit() -> None:
    """The splice is the edit, so changing it changes what replays."""
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Edit","input":{"file_path":"/w/a.py"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=('{"filePath":"/w/a.py","oldString":"old","newString":"new"}'),
    )
    records = list(claude.normalize(StringIO(native)))
    edit = _result(_last_act(records))
    assert isinstance(edit.result, EditResult)
    changed = replace(
        edit,
        stdout=(
            "".join(text for text in edit.stdout if isinstance(text, str)),
            Splice(before="old", after="changed"),
        ),
    )

    output = StringIO()
    claude.denormalize(
        [changed if record is edit else record for record in records],
        output,
    )

    assert '"newString":"changed"' in output.getvalue()


@pytest.mark.parametrize(
    "blocks",
    [
        (
            '{"type":"text","text":"reading"},{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat a.txt"}}'
        ),
        (
            '{"type":"thinking","thinking":"first"},{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat a.txt"}}'
        ),
    ],
    ids=["text-then-call", "thinking-then-call"],
)
def test_a_rename_reaches_a_bash_call_that_did_not_lead_its_line(
    blocks: str,
) -> None:
    """A lifted edit must reach its call wherever the call sat on the line.

    The writer queues one line per GROUP, and a group's head is whichever act
    claude wrote first. A line that speaks before it calls -- prose, or a
    thinking block -- puts the ``tool_use`` second, and registering only the
    head left that call unreachable: the rename was silently dropped and the
    session replayed the original path.
    """
    native = _line(
        type='"assistant"',
        message=f'{{"role":"assistant","content":[{blocks}]}}',
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"body\\n"}]}'
        ),
        toolUseResult=(
            '{"stdout":"body\\n","stderr":"","interrupted":false,"isImage":false}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    lifted = _result(_last_act(records))

    output = StringIO()
    claude.denormalize(
        [
            replace(
                lifted,
                result=replace(_typed_result(lifted, ReadResult), path="renamed.txt"),
            )
            if record is lifted
            else record
            for record in records
        ],
        output,
    )

    assert '"command":"/bin/cat renamed.txt"' in output.getvalue()


def test_a_rename_reaches_the_second_of_two_bash_calls_on_one_line() -> None:
    """One line may open several calls, and each result edits its own.

    Registering only the group's head made the second call unreachable, so an
    edit to the record answering it replayed against the wrong command.
    """
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":['
            '{"type":"tool_use","id":"c1","name":"Bash",'
            '"input":{"command":"/bin/cat a.txt"}},'
            '{"type":"tool_use","id":"c2","name":"Bash",'
            '"input":{"command":"/bin/cat b.txt"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c2","content":"body\\n"}]}'
        ),
        toolUseResult=(
            '{"stdout":"body\\n","stderr":"","interrupted":false,"isImage":false}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    lifted = _result(_last_act(records))
    assert isinstance(lifted.result, ReadResult)
    assert lifted.call_id == "c2"

    output = StringIO()
    claude.denormalize(
        [
            replace(
                lifted,
                result=replace(_typed_result(lifted, ReadResult), path="renamed.txt"),
            )
            if record is lifted
            else record
            for record in records
        ],
        output,
    )

    written = output.getvalue()
    assert '"command":"/bin/cat renamed.txt"' in written
    # The untouched call keeps its own path: an edit reaches ONE command.
    assert '"command":"/bin/cat a.txt"' in written


def test_deleting_one_call_does_not_move_another_across_prose() -> None:
    """A slot names the call it held, so a deletion empties that slot.

    Matching calls to slots by ORDINAL let a surviving call take a removed
    one's place: from ``text, a, text, b``, dropping ``a`` emitted
    ``text, b, text`` -- ``b`` jumped backwards across prose it followed, so a
    caller editing one act silently reordered another.
    """
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":['
            '{"type":"text","text":"one"},'
            '{"type":"tool_use","id":"a","name":"X","input":{}},'
            '{"type":"text","text":"two"},'
            '{"type":"tool_use","id":"b","name":"Y","input":{}}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    kept = [
        record
        for record in records
        if not (isinstance(record, ToolCall) and record.call_id == "a")
    ]

    output = StringIO()
    claude.denormalize(kept, output)

    blocks = _content_blocks(output.getvalue())
    assert [block.get("type") for block in blocks] == ["text", "text", "tool_use"]
    assert blocks[-1].get("id") == "b"


def _content_blocks(native: str) -> list[dict[str, object]]:
    """Return the message content blocks of a written line."""
    record = json_loads(native.splitlines()[0], dict[str, object])
    message = from_plain(
        record.get("message"),
        dict[str, object],
        default=dict[str, object](),
    )
    return list(
        from_plain(
            message.get("content"),
            list[dict[str, object]],
            default=list[dict[str, object]](),
        ),
    )


def test_a_malformed_failure_marker_prevents_lifting() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat a.txt"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"body\\n","is_error":1}]}'
        ),
        toolUseResult=('{"stdout":"body\\n","stderr":"","interrupted":false}'),
    )

    records = list(claude.normalize(StringIO(native)))

    assert isinstance(_result(_last_act(records)).result, BashResult)
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_an_idless_tool_call_does_not_correlate_with_an_idless_result() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use",'
            '"name":"Bash","input":{"command":"/bin/cat a.txt"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result","content":"body\\n"}]}'
        ),
        toolUseResult='{"stdout":"body\\n","stderr":"","interrupted":false}',
    )

    result = _last_act(list(claude.normalize(StringIO(native))))

    assert _typed_result(result, UncategorizedToolResult) == UncategorizedToolResult()


def test_a_structured_bash_result_without_success_flags_stays_shell() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat a.txt"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"body\\n"}]}'
        ),
        toolUseResult='{"stdout":"body\\n","stderr":""}',
    )

    result = _last_act(list(claude.normalize(StringIO(native))))

    assert isinstance(_result(result).result, BashResult)


def test_a_bash_result_without_its_payload_shows_what_the_model_read() -> None:
    """``claude -p`` writes a sub-agent's results with no ``toolUseResult`` at all."""
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"gh pr create --fill"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"https://github.com/o/r/pull/7",'
            '"is_error":false}]}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))

    result = _result(_last_act(records))
    assert isinstance(result.result, BashResult)
    assert result.text() == "https://github.com/o/r/pull/7"
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def _call_and_result(
    tool: str,
    block: str,
    *,
    command: str = "/bin/cat a.txt",
    **fields: str,
) -> str:
    """Return a ``tool`` call of ``command`` and the line answering it with ``block``."""
    return _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            f'"name":"{tool}","input":{{"command":"{command}"}}}}]}}'
        ),
    ) + _line(
        type='"user"',
        message='{"role":"user","content":[' + block + "]}",
        **fields,
    )


def test_a_failed_call_with_its_payload_reads_as_its_fields_say() -> None:
    """The stored form shows nothing but what ``toolUseResult`` held."""
    native = _call_and_result(
        "Bash",
        '{"type":"tool_result","tool_use_id":"c1","content":"Error: boom",'
        '"is_error":true}',
        toolUseResult='"Error: boom"',
    )

    result = _result(_last_act(list(claude.normalize(StringIO(native)))))

    assert isinstance(result.result, BashResult)
    assert result.shown is None


@pytest.mark.parametrize(
    ("is_error", "expected"),
    [("false", ReadResult), ("true", BashResult)],
)
def test_a_call_lifts_to_what_it_did_only_when_it_did_not_fail(
    is_error: str,
    expected: type[object],
) -> None:
    native = _call_and_result(
        "Bash",
        '{"type":"tool_result","tool_use_id":"c1","content":"body\\n",'
        f'"is_error":{is_error}}}',
        toolUseResult='{"stdout":"body\\n","stderr":"","interrupted":false}',
    )

    result = _result(_last_act(list(claude.normalize(StringIO(native)))))

    assert type(result.result) is expected


def test_a_results_text_parts_are_read_in_order_and_the_rest_kept() -> None:
    """Each text part joins the text; any other member stays in the stored blocks."""
    native = _call_and_result(
        "mcp__docs__search",
        '{"type":"tool_result","tool_use_id":"c1","content":["note",'
        '{"type":"reference","id":"r1"},{"type":"text","text":"a"},'
        '{"type":"text"},{"type":"text","text":"b"}]}',
    )

    records = list(claude.normalize(StringIO(native)))

    result = _result(_last_act(records))
    assert isinstance(result.result, McpResult)
    assert result.text() == "a\n\nb"
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_a_result_naming_no_call_is_read_and_stored_as_untyped() -> None:
    native = _line(
        type='"user"',
        message='{"role":"user","content":[{"type":"tool_result","content":"ok"}]}',
    )

    result = _result(_last_act(list(claude.normalize(StringIO(native)))))

    assert _typed_result(result, UncategorizedToolResult) == UncategorizedToolResult()
    assert result.call_id == ""
    # Stored form, frozen as normalized_golden_test.py freezes it: a call never seen
    # names no tool.
    assert from_plain(result.extra["$result"], dict[str, object])["tool_name"] == ""


def test_a_result_is_written_from_its_fields_once_they_change() -> None:
    """A redaction writes the result as it now stands, its old text nowhere."""
    native = _call_and_result(
        "Bash",
        '{"type":"tool_result","tool_use_id":"c1","content":"secret\\n",'
        '"is_error":false}',
        command="printenv TOKEN",
        toolUseResult='{"stdout":"secret\\n","stderr":"","interrupted":false}',
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(_last_act(records))
    assert isinstance(result.result, BashResult)
    changed = [
        replace(result, stdout=("[redacted]\n",)) if record is result else record
        for record in records
    ]

    output = StringIO()
    claude.denormalize(changed, output)

    assert output.getvalue() == native.replace("secret", "[redacted]")


def test_a_result_stores_only_what_its_fields_do_not_hold() -> None:
    """Stored form, frozen as normalized_golden_test.py freezes it, for one result.

    Its type, call and text are the record's own, so the block keeps only the rest,
    and a text the fields spell is a marker, not a second copy.
    """
    native = _call_and_result(
        "Bash",
        '{"type":"tool_result","tool_use_id":"c1","content":"body\\n",'
        '"is_error":false}',
        toolUseResult='{"stdout":"body\\n","stderr":""}',
    )

    result = _result(_last_act(list(claude.normalize(StringIO(native)))))

    assert isinstance(result.result, BashResult)
    assert from_plain(result.extra["$result"], dict[str, object]) == {
        "block": {"is_error": False},
        "keys": ("type", "tool_use_id", "content", "is_error"),
        "tool_name": "Bash",
        "$text": True,
    }


def test_a_line_answering_two_calls_round_trips_and_each_keeps_the_line() -> None:
    """Stored form, frozen as normalized_golden_test.py freezes it: each result of a
    line of several keeps the line's blocks, which the first also keeps as its shape.
    """
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":['
            '{"type":"tool_use","id":"c1","name":"Bash","input":{"command":"ls"}},'
            '{"type":"tool_use","id":"c2","name":"Bash","input":{"command":"pwd"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":['
            '{"type":"tool_result","tool_use_id":"c1","content":"a"},'
            '{"type":"tool_result","tool_use_id":"c2","content":"/w"}]}'
        ),
    )

    records = list(claude.normalize(StringIO(native)))

    results = [
        record
        for record in records
        if isinstance(record, ToolMessage) and isinstance(record.result, BashResult)
    ]
    assert [result.text() for result in results] == ["a", "/w"]
    line = json_loads(native.splitlines()[1], dict[str, object])
    blocks = from_plain(line["message"], dict[str, object])["content"]
    for result in results:
        shape = from_plain(result.extra["$result"], dict[str, object])
        assert from_plain(shape["message_blocks"], list[dict[str, object]]) == blocks
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_reused_call_ids_correlate_with_the_preceding_call() -> None:
    first_call = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat a.txt"}}]}'
        ),
    )
    first_result = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"A\\n"}]}'
        ),
        toolUseResult='{"stdout":"A\\n","stderr":"","interrupted":false}',
    )
    second_call = first_call.replace("a.txt", "b.txt")
    second_result = first_result.replace('"A\\n"', '"B\\n"')

    results = [
        record
        for record in claude.normalize(
            StringIO(first_call + first_result + second_call + second_result),
        )
        if isinstance(record, ToolMessage) and isinstance(record.result, ReadResult)
    ]

    assert [
        (
            _typed_result(result, ReadResult).path,
            "".join(text for text in result.stdout if isinstance(text, str)),
        )
        for result in results
    ] == [
        ("a.txt", "A\n"),
        ("b.txt", "B\n"),
    ]


def test_a_foreign_lifted_read_uses_its_semantic_content() -> None:
    records = (
        ToolMessage(
            call_id="c1",
            state="done",
            stdout=("body\n",),
            result=ReadResult(path="a.txt"),
            extra={
                "$shell": {
                    "command": ["/bin/bash", "-lc", "/bin/cat a.txt"],
                    "exit_code": 0,
                },
            },
        ),
    )
    output = StringIO()

    claude.denormalize(records, output)

    assert '"content":"body\\n"' in output.getvalue()


def test_an_edited_lifted_path_updates_the_claude_bash_call() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat old.txt"}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"body\\n"}]}'
        ),
        toolUseResult=('{"stdout":"body\\n","stderr":"","interrupted":false}'),
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(records[-1])
    changed = [
        *records[:-1],
        replace(
            result,
            result=replace(_typed_result(result, ReadResult), path="new.txt"),
        ),
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert '"command":"/bin/cat new.txt"' in output.getvalue()


def test_a_null_read_file_round_trips() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Read","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult='{"file":null}',
    )
    records = list(claude.normalize(StringIO(native)))
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue() == native


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        ("Read", '{"type":"text","file":{"filePath":"/a","content":""}}'),
        ("Read", '{"type":"text","file":{"filePath":"/a","content":null}}'),
        ("Read", '{"type":"text","file":{"filePath":"/a"}}'),
        ("WebFetch", '{"url":"https://x","result":""}'),
        ("WebFetch", '{"url":"https://x","result":null}'),
        ("WebFetch", '{"url":"https://x"}'),
    ],
    ids=[
        "read-empty",
        "read-null",
        "read-absent",
        "fetch-empty",
        "fetch-null",
        "fetch-absent",
    ],
)
def test_an_empty_null_or_absent_result_text_round_trips(
    tool: str,
    payload: str,
) -> None:
    """Each reads as an empty ``stdout`` and is written back as the line had it."""
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            f'"name":"{tool}","input":{{}}}}]}}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )
    records = list(claude.normalize(StringIO(native)))

    assert _result(records[-1]).stdout == ()
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


@pytest.mark.parametrize(
    "payload",
    ["{}", "null", '"Error: Exit code 1"', '[{"type":"text","text":"ok"}]'],
    ids=["empty", "null", "failed-call-string", "mcp-block-list"],
)
def test_an_explicit_tool_payload_round_trips(payload: str) -> None:
    """A failed call answers with a bare string, an MCP tool with a block list."""
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Write","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )
    records = list(claude.normalize(StringIO(native)))
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue() == native


# One field per case: a line has up to 68 fields, each re-reading its window
# five ways, and one case per line took up to 0.9s.
@pytest.mark.parametrize(
    ("name", "index", "path"),
    [
        (name, index, path)
        for name, lines in FIXTURES.items()
        for index, line in enumerate(lines)
        for path in field_paths(json_loads(line, dict[str, object]))
    ],
)
def test_a_mistyped_field_aborts_neither_the_read_nor_the_write(
    name: str,
    index: int,
    path: str,
) -> None:
    """A log field of the wrong type reads as absent, as a missing one does.

    The line is read beside the call or result it pairs with by tool id, so a
    result meets its wrong field on the path the call's tool name selects.
    """
    lines = FIXTURES[name]
    ids = {token for token in lines[index].split('"') if token.startswith("toolu_")}
    window = [
        at
        for at, line in enumerate(lines)
        if at == index or any(f'"{id_}"' in line for id_ in ids)
    ]
    record = json_loads(lines[index], dict[str, object])
    missing = _swapped(lines, window, index, without(record, path))
    baseline = (
        None if path in _KEPT_AS_TEXT else unread(claude.normalize(StringIO(missing)))
    )
    failed: list[str] = []
    for _, changed in mistyped(record, path):
        session = _swapped(lines, window, index, changed)
        records = list(claude.normalize(StringIO(session)))
        try:
            claude.denormalize(records, StringIO())
        except TypeError as error:
            failed.append(f"{path}: {error}")
        if baseline is not None and unread(records) > baseline:
            failed.append(f"{path}: the line reads worse than without the field")

    assert failed == [], "\n".join(failed)


def _swapped(lines: list[str], window: list[int], index: int, record: object) -> str:
    """Return the session of ``window`` with line ``index`` replaced by ``record``."""
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    return "".join(line if at == index else lines[at] for at in window)


@pytest.mark.parametrize(
    ("payload", "expected_payload"),
    [
        (
            '{"filePath":"/a","content":"","other":1}',
            '{"filePath":"/a","content":"new","other":1}',
        ),
        ('{"filePath":"/a","other":1}', '{"filePath":"/a","other":1}'),
    ],
    ids=["present field changes", "missing field stays missing"],
)
def test_tool_result_replay_uses_current_values_only_for_stated_fields(
    payload: str,
    expected_payload: str,
) -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Write","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(records[-1])
    tool_result = _typed_result(result, WriteResult)
    changed = [
        *records[:-1],
        replace(result, result=replace(tool_result, content="new")),
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert output.getvalue() == native.replace(payload, expected_payload)


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        ("WebFetch", '{"durationMs":1.5}'),
        ("Agent", '{"totalDurationMs":1.5}'),
    ],
)
def test_fractional_millisecond_durations_round_trip(tool: str, payload: str) -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            f'"name":"{tool}","input":{{}}}}]}}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )
    records = list(claude.normalize(StringIO(native)))
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue() == native


def test_large_integer_millisecond_duration_round_trips_exactly() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"WebFetch","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult='{"durationMs":12345678901234567}',
    )

    assert _round_trip(native) == native


@pytest.mark.parametrize(
    ("tool", "result"),
    [
        ("WebSearch", '{"query":"q","durationSeconds":true}'),
        ("WebFetch", '{"durationMs":true,"url":"https://x"}'),
    ],
)
def test_a_boolean_is_not_a_numeric_duration(tool: str, result: str) -> None:
    native = _line(
        type='"assistant"',
        uuid='"a1"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"' + tool + '","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        uuid='"u1"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=result,
    )

    records = list(claude.normalize(StringIO(native)))

    typed_result = _result(_last_act(records)).result
    assert isinstance(typed_result, WebSearchResult | WebFetchResult)
    assert typed_result.duration_sec is None
    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_an_unparsable_line_survives_verbatim() -> None:
    native = '{"type":"user","message":"truncated\n'

    records = list(claude.normalize(StringIO(native)))

    assert _last_act(records) == IncompleteRecord(text=native)


def test_a_neutral_tool_result_emits_a_complete_claude_block() -> None:
    records = (
        SessionContext(context_id=0, extra={"sessionId": "s1"}),
        ToolMessage(
            context_id=0,
            call_id="c1",
            state="done",
            stdout=("ok",),
            result=UncategorizedToolResult(),
        ),
    )
    out = StringIO()

    claude.denormalize(records, out)

    assert out.getvalue() == (
        '{"parentUuid":null,"isSidechain":false,"type":"user","message":'
        '{"role":"user","content":['
        '{"type":"tool_result","tool_use_id":"c1","content":"ok"}]},'
        '"timestamp":"1970-01-01T00:00:00.000Z",'
        '"sessionId":"s1","userType":"external","cwd":"","version":"2.1.241",'
        '"uuid":"281e2493-8e17-56d5-94de-df1e217d5a73"}\n'
    )


def test_a_synthesized_session_denormalizes_to_claude_records() -> None:
    records = (
        SessionContext(context_id=0, extra={"sessionId": "s1", "cwd": "/w"}),
        UserMessage(context_id=0, content="Hi.", extra={"uuid": "u1"}),
        Thinking(context_id=0, content="Reading.", encrypted="sig"),
        ToolCall(
            context_id=0,
            call_id="c1",
            name="Read",
            arguments={"path": "/a"},
            extra={"uuid": "a2"},
        ),
        ToolMessage(
            context_id=0,
            call_id="c1",
            state="done",
            stdout=("ok",),
            result=UncategorizedToolResult(),
            extra={"uuid": "u2", "$result": {"keys": ["type", "tool_use_id"]}},
        ),
    )
    out = StringIO()

    claude.denormalize(records, out)

    rebuilt = claude.normalize(StringIO(out.getvalue()))
    # The result comes back as what the tool DID, not as a bare result: the
    # ``Read`` call two records earlier is what names it.
    assert [
        former_kind(r) if isinstance(r, ToolMessage) else type(r).__name__
        for r in _transcript(rebuilt)
    ] == [
        "UserMessage",
        "Thinking",
        "ToolCall",
        "ReadResult",
    ]


def _round_trip(native: str) -> str:
    """Return one Claude stream after normalization and denormalization."""
    output = StringIO()
    claude.denormalize(claude.normalize(StringIO(native)), output)
    return output.getvalue()


@pytest.mark.parametrize(
    "native",
    [
        _line(
            type='"assistant"',
            message='{"role":"assistant","content":[{"type":"text","text":7}]}',
        ),
        _line(
            type='"user"',
            message=(
                '{"content":[{"type":"image","source":{"media_type":"image/png",'
                '"type":"base64","data":"YQ=="}},{"text":"A\\nB","meta":1,'
                '"type":"text"},{"source":{"type":"base64","data":"Yg==",'
                '"media_type":"image/jpeg"},"type":"image"}],"role":"user",'
                '"tail":null}'
            ),
            timestamp='{"malformed":true}',
        ),
        _line(
            type='"assistant"',
            message=(
                '{"role":"assistant","content":[{"type":"text","text":"A\\nB",'
                '"meta":1},{"type":"thinking","thinking":"T","signature":"s",'
                '"meta":2},{"type":"text","text":"C"},{"future":true},'
                '{"type":"tool_use","id":"c","name":"N","input":{},"meta":3}]}'
            ),
        ),
        _line(
            type='"assistant"',
            message=(
                '{"role":"assistant","content":[null,"raw",7,{"type":"text",'
                '"text":"kept"}]}'
            ),
        ),
        _line(
            type='"assistant"',
            message=(
                '{"role":"assistant","content":[{"type":"tool_use","id":null,'
                '"name":7,"input":"bad"},{"type":"tool_use","input":{}}]}'
            ),
        ),
        _line(
            type='"assistant"',
            message='{"role":"assistant","content":[],"usage":null}',
        ),
        _line(
            type='"assistant"',
            message='{"role":"assistant","content":[],"usage":{}}',
        ),
        _line(
            type='"user"',
            message=(
                '{"role":"user","content":[{"type":"image","source":'
                '{"type":"base64","data":""}},{"type":"image","source":'
                '{"type":"base64","media_type":"image/png","data":""}}]}'
            ),
        ),
        _line(
            type='"user"',
            message='{"role":"user","content":"hi","$provider":"raw"}',
            **{"$line": '"raw"'},
        ),
    ],
    ids=[
        "non-string-text",
        "LJR-001-003-007-010-047-049",
        "LJR-002-010-011",
        "LJR-036",
        "LJR-035",
        "LJR-024-null",
        "LJR-024-empty",
        "LJR-048",
        "LJR-044",
    ],
)
def test_lossless_claude_edge_shapes(native: str) -> None:
    assert _round_trip(native) == native


def test_later_line_context_changes_are_explicit_and_lossless() -> None:
    native = _line(
        type='"user"',
        mode='"first"',
        permissionMode='"allow"',
        message='{"role":"user","content":"one"}',
    ) + _line(
        type='"user"',
        mode='"second"',
        permissionMode="null",
        message='{"role":"user","content":"two"}',
    )

    records = list(claude.normalize(StringIO(native)))

    # Settings only: the reader also restates the file's ENCODING as a
    # ``SessionContext``, and those state no turn for a record to name.
    settings = [
        record
        for record in records
        if isinstance(record, SessionContext) and record.extra
    ]
    messages = [record for record in records if isinstance(record, UserMessage)]
    assert len(settings) == 2
    assert [message.context_id for message in messages] == [
        records.index(settings[0]),
        records.index(settings[1]),
    ]
    assert _round_trip(native) == native


def test_web_search_rows_preserve_shape_and_append_new_rows() -> None:
    payload = (
        '{"query":"q","results":[{"tool_use_id":"s1","content":['
        '{"url":"u1","title":null,"extra":1},{"title":"t2"}]}],'
        '"durationSeconds":1}'
    )
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"WebSearch","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(records[-1])
    assert isinstance(result.result, WebSearchResult)
    changed = [
        *records[:-1],
        replace(
            result,
            stdout=(
                *result.stdout,
                WebSearchHit(url="u3", title="t3", snippet="s3"),
            ),
        ),
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert '"url":"u1","title":null,"extra":1' in output.getvalue()
    assert '"title":"t2"' in output.getvalue()
    assert '"title":"t3","url":"u3","snippet":"s3"' in output.getvalue()


def test_agent_result_edits_preserve_block_boundaries() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Agent","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=(
            '{"status":"done","content":[{"type":"text","text":"A\\nB",'
            '"extra":1},{"type":"text","text":"C"}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(records[-1])
    assert isinstance(result.result, AgentSpawnResult)
    changed = [*records[:-1], replace(result, stdout=("X\nY\nZ",))]
    output = StringIO()

    claude.denormalize(changed, output)

    assert (
        '"content":[{"type":"text","text":"X\\nY","extra":1},'
        '{"type":"text","text":"Z"}]' in output.getvalue()
    )


def test_multiblock_tool_result_edits_update_the_matching_text_blocks() -> None:
    native = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":[{"type":"text","text":"A\\nB",'
            '"extra":1},{"type":"text","text":"C"}]}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(_last_act(records))
    assert _typed_result(result, UncategorizedToolResult) == UncategorizedToolResult()
    changed = [
        replace(result, stdout=("X\nY\nZ",)) if record is result else record
        for record in records
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert (
        '"content":[{"type":"text","text":"X\\nY","extra":1},'
        '{"type":"text","text":"Z"}]' in output.getvalue()
    )


def test_foreign_residual_does_not_leak_into_synthesized_claude_wire() -> None:
    records = (
        SessionContext(context_id=0, extra={"sessionId": "s1"}),
        UserMessage(
            context_id=0,
            content="hi",
            extra={"uuid": "u1", "id": "foreign", "$payload": {"x": 1}},
        ),
    )
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue() == (
        '{"parentUuid":null,"isSidechain":false,"type":"user","message":'
        '{"role":"user","content":[{"type":"text","text":"hi"}]},"uuid":"u1",'
        '"timestamp":"1970-01-01T00:00:00.000Z",'
        '"sessionId":"s1","userType":"external","cwd":"","version":"2.1.241"}\n'
    )


def test_changed_edit_diff_does_not_reemit_the_stale_structured_patch() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Edit","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=(
            '{"filePath":"/a","structuredPatch":[{"oldStart":1,"oldLines":1,'
            '"newStart":1,"newLines":1,"lines":["-a","+b"]}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    result = _result(records[-1])
    assert isinstance(result.result, EditResult)
    changed = [
        *records[:-1],
        replace(
            result,
            stdout=(
                "".join(text for text in result.stdout if isinstance(text, str)),
                Splice(before="a", after="c"),
            ),
        ),
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert "structuredPatch" not in output.getvalue()


@pytest.mark.parametrize(
    "native",
    [
        _line(type='"user"', message='{"role":"user","content":7}'),
        (
            '{"type":"system","role":7,"subtype":{"bad":true},'
            '"content":["bad"],"userType":"external","sessionId":"s1"}\n'
        ),
    ],
    ids=["user-content", "system-role-content-subtype"],
)
def test_malformed_message_fields_survive_verbatim(native: str) -> None:
    assert _round_trip(native) == native


def test_assistant_prose_index_counts_only_semantic_acts() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"future":true},'
            '{"type":"text","text":"first"},{"type":"thinking",'
            '"thinking":"second","signature":"sig"}]}'
        ),
    )

    assert [
        type(record).__name__
        for record in _transcript(claude.normalize(StringIO(native)))
    ] == [
        "AssistantMessage",
        "Thinking",
    ]


def test_web_search_preserves_scalar_empty_and_malformed_rows() -> None:
    payload = (
        '{"query":"q","results":[{"tool_use_id":"s1","content":['
        '7,{},["bad"],{"url":7},{"url":"u"}]}]}'
    )
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"WebSearch","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )

    assert _round_trip(native) == native


def test_provider_dollar_key_after_envelope_stays_after_envelope() -> None:
    native = (
        '{"type":"user","message":{"role":"user","content":"hi"},'
        '"userType":"external","sessionId":"s1","$after":true}\n'
    )

    assert _round_trip(native) == native


def test_mixed_unicode_escaping_is_preserved_per_line() -> None:
    escaped = "caf\\u00e9"  # codespell:ignore caf.
    native = _line(
        type='"user"',
        message=f'{{"role":"user","content":"{escaped}"}}',
    ) + _line(type='"user"', message='{"role":"user","content":"café"}')

    records = list(claude.normalize(StringIO(native)))

    output = StringIO()
    claude.denormalize(records, output)
    assert output.getvalue() == native


def test_clearing_user_text_and_images_removes_source_blocks() -> None:
    native = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"text","text":"old"},'
            '{"type":"image","source":{"type":"base64","data":"aGk=",'
            '"media_type":"image/png"}},{"future":true}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    message = next(record for record in records if isinstance(record, UserMessage))
    changed = [
        replace(record, content=None, attachments=()) if record is message else record
        for record in records
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert '"content":[{"future":true}]' in output.getvalue()


def test_clearing_assistant_acts_removes_source_blocks() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"text","text":"old"},'
            '{"type":"thinking","thinking":"thought","signature":"sig"},'
            '{"type":"tool_use","id":"c1","name":"Read","input":{}},'
            '{"future":true}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    message = next(record for record in records if isinstance(record, AssistantMessage))
    changed = [
        replace(record, content=None) if record is message else record
        for record in records
        if not isinstance(record, Thinking | ToolCall)
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert '"content":[{"future":true}]' in output.getvalue()


_FOREIGN = SessionContext(encoding={"newline_terminated": True})


def _tool_uses(records: Sequence[SessionRecord]) -> list[str]:
    """Return the id of every ``tool_use`` block a claude rewrite holds."""
    output = StringIO()
    claude.denormalize(records, output)
    rebuilt = list(claude.normalize(StringIO(output.getvalue())))
    return [record.call_id for record in rebuilt if isinstance(record, ToolCall)]


def test_an_untyped_foreign_result_after_a_typed_one_does_not_abort() -> None:
    # Only a TYPED result has a claude tool to name; an untyped one after an
    # assistant line reached the synthetic-call path and raised ``KeyError``.
    records = [
        _FOREIGN,
        ToolMessage(call_id="a", state="done", result=BashResult(command=("ls",))),
        AssistantMessage(content="x"),
        ToolMessage(
            call_id="b",
            state="done",
            stdout=("y",),
            result=UncategorizedToolResult(),
        ),
    ]

    assert _tool_uses(records) == ["a"]


def test_a_foreign_call_after_its_result_is_written_once() -> None:
    # The call is synthesized ahead of its answer, so the real one arriving
    # later would state the same id a second time.
    records = [
        _FOREIGN,
        ToolMessage(call_id="a", state="done", result=BashResult(command=("ls",))),
        ToolCall(call_id="a", name="shell"),
    ]

    assert _tool_uses(records) == ["a"]


def test_a_foreign_act_run_inside_an_open_call_is_left_out() -> None:
    """Codex's ``exec`` reports the shell command it ran before its own answer."""
    records = [
        _FOREIGN,
        ToolCall(call_id="exec", name="exec"),
        ToolMessage(call_id="inner", state="done", result=BashResult(command=("ls",))),
        ToolMessage(call_id="exec", state="done", result=UncategorizedToolResult()),
        _FOREIGN,
        ToolCall(call_id="interrupted", name="exec"),
        _FOREIGN,
        ToolMessage(call_id="search", state="done", result=WebSearchResult(query="q")),
    ]
    output = StringIO()

    claude.denormalize(records, output)

    assert [
        r.call_id
        for r in claude.normalize(StringIO(output.getvalue()))
        if isinstance(r, ToolMessage)
    ] == ["exec", "search"]


def _written_lines(records: list[SessionRecord]) -> list[dict[str, object]]:
    """Return the claude lines ``records`` are written as, parsed."""
    output = StringIO()
    claude.denormalize(records, output)
    return [
        json_loads(line, dict[str, object]) for line in output.getvalue().splitlines()
    ]


def test_another_cli_s_lines_take_the_last_stated_time_and_stay_raw_utf8() -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)
    lines = _written_lines(
        [
            _FOREIGN,
            UserMessage(timestamp=first, content="é"),
            AssistantMessage(content="b"),
            # As a line of its own CLI spelled it.
            UserMessage(
                timestamp=datetime(2026, 1, 2, tzinfo=UTC),
                content="c",
                extra={"$timestamp_as_written": "2026-01-02T00:00:00.000Z"},
            ),
            AssistantMessage(content="d"),
        ],
    )

    assert [line["timestamp"] for line in lines] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00.000Z",
        "2026-01-02T00:00:00.000Z",
    ]
    assert "$timestamp_as_written" not in json.dumps(lines)
    output = StringIO()
    claude.denormalize([_FOREIGN, UserMessage(content="é")], output)
    assert "é" in output.getvalue()


def test_a_result_another_cli_called_for_is_linked_to_the_line_before_it() -> None:
    lines = _written_lines(
        [
            SessionContext(cwd=Path("/w"), encoding={"newline_terminated": True}),
            UserMessage(content="hi"),
            AssistantMessage(content="ok"),
            ToolMessage(call_id="x", state="done", result=WebSearchResult(query="q")),
        ],
    )

    _, said, called, _ = lines
    assert (called["parentUuid"], called["cwd"]) == (said["uuid"], "/w")


def test_the_call_written_for_another_cli_s_result_is_at_the_result_s_time() -> None:
    """Spelled as the result's own line spelled it."""
    lines = _written_lines(
        [
            _FOREIGN,
            ToolMessage(
                timestamp=datetime(2026, 1, 2, tzinfo=UTC),
                call_id="x",
                state="done",
                result=WebSearchResult(query="q"),
                extra={"$timestamp_as_written": "2026-01-02T00:00:00.000Z"},
            ),
        ],
    )

    assert [line["timestamp"] for line in lines] == ["2026-01-02T00:00:00.000Z"] * 2


def test_a_prompt_source_line_states_the_turn_s_permission_mode() -> None:
    lines = _written_lines(
        [
            SessionContext(
                context_id=0,
                permission="plan",
                encoding={"newline_terminated": True},
            ),
            UserMessage(context_id=0, content="hi", extra={"promptSource": "typed"}),
        ],
    )

    assert lines[0]["permissionMode"] == "plan"


def test_a_spelling_naming_no_exceptions_writes_every_line_raw() -> None:
    output = StringIO()

    claude.denormalize(
        [
            SessionContext(
                encoding={"newline_terminated": True, "ascii_escaped": False},
            ),
            UserMessage(content="é"),
        ],
        output,
    )

    assert "é" in output.getvalue()


def test_a_corrupt_escape_bitmap_is_refused() -> None:
    corrupt = SessionContext(
        encoding={
            "newline_terminated": True,
            "ascii_escaped": False,
            "ascii_escape_exceptions": "!!",
        },
    )

    with pytest.raises(binascii.Error):
        claude.denormalize([corrupt, UserMessage(content="hi")], StringIO())


def test_a_line_s_text_and_result_together_round_trip() -> None:
    native = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"text","text":"a"},'
            '{"type":"tool_result","tool_use_id":"c1","content":"ok"}]}'
        ),
    )

    assert _round_trip(native) == native


def test_an_edited_usage_is_written_from_its_record() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"text","text":"b"}],'
            '"usage":{"output_tokens":1}}'
        ),
    )
    records = [
        replace(r, info={"output_tokens": 9}) if isinstance(r, TokenUsage) else r
        for r in claude.normalize(StringIO(native))
    ]
    output = StringIO()

    claude.denormalize(records, output)

    assert '"usage":{"output_tokens":9}' in output.getvalue()


def test_another_cli_s_bookkeeping_and_openai_s_seal_stay_out_of_claude() -> None:
    records = [
        _FOREIGN,
        UncategorizedRecord(kind="event_msg/task_started", payload={"x": 1}),
        IncompleteRecord(text="{torn\n"),
        UserMessage(content="hi"),
        Thinking(encrypted="gAAA-openai", reasoning_id="rs_1"),
        Thinking(content="plan"),
        AssistantMessage(content="yo"),
    ]
    output = StringIO()

    claude.denormalize(records, output)

    written = output.getvalue()
    assert "task_started" not in written
    assert "torn" not in written
    assert "gAAA-openai" not in written
    assert [
        r.content
        for r in claude.normalize(StringIO(written))
        if isinstance(r, Thinking)
    ] == ["plan"]


@pytest.mark.parametrize("late_call", [False, True])
def test_a_foreign_result_preserves_its_tool_identity_and_late_arguments(
    late_call: bool,
) -> None:
    arguments = {"command": "pwd", "description": "show directory"}
    records: list[SessionRecord] = [
        _FOREIGN,
        ToolMessage(call_id="a", state="done", stdout=("ok",), result=BashResult()),
    ]
    if late_call:
        records.append(ToolCall(call_id="a", name="shell", arguments=arguments))
    output = StringIO()

    claude.denormalize(records, output)

    call, result = _transcript(claude.normalize(StringIO(output.getvalue())))
    assert isinstance(call, ToolCall)
    shell = _result(result)
    assert isinstance(shell.result, BashResult)
    assert (call.call_id, call.name, dict(call.arguments)) == (
        "a",
        "Bash",
        arguments if late_call else {},
    )
    assert (shell.call_id, shell.stdout, shell.stderr) == ("a", ("ok",), ())


def test_a_foreign_session_preserves_stated_identity_and_parent_links() -> None:
    stated = "11111111-1111-4111-8111-111111111111"
    output = StringIO()

    claude.denormalize(
        [
            _FOREIGN,
            UserMessage(content="hi", extra={"uuid": stated}),
            AssistantMessage(content="reply"),
        ],
        output,
    )

    user, assistant = _transcript(claude.normalize(StringIO(output.getvalue())))
    assert isinstance(user, UserMessage)
    assert isinstance(assistant, AssistantMessage)
    assert user.extra["uuid"] == stated
    assert assistant.extra["parentUuid"] == stated


def test_a_call_added_after_a_deleted_one_is_still_written() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":['
            '{"type":"tool_use","id":"a","name":"Read","input":{}},'
            '{"type":"tool_use","id":"b","name":"Read","input":{}}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    # Same context and an empty residual, so the new call stays on the line.
    kept = [
        replace(record, call_id="c")
        if isinstance(record, ToolCall) and record.call_id == "b"
        else record
        for record in records
    ]

    assert _tool_uses(kept) == ["a", "c"]


def test_thinking_encryption_uses_provider_string() -> None:
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"thinking",'
            '"thinking":"thought","signature":"sealed"}]}'
        ),
    )

    thinking = next(
        record
        for record in claude.normalize(StringIO(native))
        if isinstance(record, Thinking)
    )
    assert thinking.encrypted == "sealed"


def test_user_line_normalizes_every_act() -> None:
    native = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"text","text":"before"},'
            '{"type":"image","source":{"type":"base64","data":"aGk=",'
            '"media_type":"image/png"}},{"type":"tool_result",'
            '"tool_use_id":"c1","content":"one"},{"type":"text",'
            '"text":"after"},{"type":"tool_result","tool_use_id":"c2",'
            '"content":"two"}]}'
        ),
    )

    records = _transcript(claude.normalize(StringIO(native)))

    assert [
        former_kind(r) if isinstance(r, ToolMessage) else type(r).__name__
        for r in records
    ] == [
        "UserMessage",
        "UncategorizedToolResult",
        "UncategorizedToolResult",
    ]
    message = records[0]
    assert isinstance(message, UserMessage)
    assert message.content == "before\nafter"
    assert message.attachments == (Attachment(mime_descriptor="image/png", data=b"hi"),)
    assert [
        record.call_id for record in records if isinstance(record, ToolMessage)
    ] == [
        "c1",
        "c2",
    ]


def test_each_user_image_is_decoded_once() -> None:
    native = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"image","source":'
            '{"type":"base64","data":"aGk=","media_type":"image/png"}}]}'
        ),
    )

    with patch.object(base64, "b64decode", wraps=base64.b64decode) as decode:
        # Drained, not merely called: ``normalize`` yields as it reads, so an
        # undrained iterator decodes nothing.
        list(claude.normalize(StringIO(native)))

    decode.assert_called_once_with("aGk=", validate=True)


def test_grouping_holds_only_the_line_being_written() -> None:
    """Axiom 11: a writer holds neither stream, so grouping cannot be a list.

    ``_group`` returned one list per record -- a structure proportional to the
    session, measured at ~75-83 bytes per record and growing linearly
    (16776 / 31176 / 60456 bytes for 200 / 400 / 800 records). Only the acts of
    ONE line ever share a group, so the window is bounded and the groups are
    yielded rather than accumulated. The same defect codex's ``_grouped``
    carried.
    """
    records: tuple[SessionRecord, ...] = tuple(
        UserMessage(context_id=0, content=f"line {index}", extra={"$keys": ["type"]})
        for index in range(400)
    )

    groups = _group(records)

    assert not isinstance(groups, list)
    assert [len(group) for group in groups] == [1] * 400


def test_grouping_still_joins_the_acts_of_one_line() -> None:
    """The bound must not cost the property grouping exists for.

    One assistant line carries its acts and the usage reported beside them
    (axiom 3), and only the first holds the line's residual (axiom 10) -- so a
    following record with none of its own still belongs to the line before it.
    """
    lead = AssistantMessage(context_id=0, content="hi", extra={"$keys": ["type"]})
    call = ToolCall(context_id=0, call_id="c1", name="Read")
    usage = TokenUsage(context_id=0, info={"input_tokens": 1})
    next_line = UserMessage(context_id=0, content="ok", extra={"$keys": ["type"]})

    groups = [list(group) for group in _group((lead, call, usage, next_line))]

    assert groups == [[lead, call, usage], [next_line]]


def test_claude_helpers_preserve_missing_and_malformed_values() -> None:
    line: dict[str, object] = {"message": {"content": [7, {}]}}
    claude._rename_calls(line, {"c": "Bash"})
    assert line == {"message": {"content": [7, {}]}}
    assert claude._split_widths(None, [1]) == []
    assert claude._split_widths("a", []) == ["a"]
    assert claude._split_widths("a\nb", [1]) == ["a\nb"]
    assert claude._split_widths("a\nb", [1, 1]) == ["a", "b"]
    assert claude._read_attachment({"type": "image", "source": {"type": "bad"}}) is None
    assert (
        claude._read_attachment(
            {"type": "image", "source": {"type": "base64", "data": "!"}},
        )
        is None
    )
    assert claude._effort("future") is None
    assert claude._millis(None) is None
    assert claude._millis(1.5, 1500) == 1500
    assert claude._millis(1.5, 1501) == 1500.0


def test_claude_shape_writers_preserve_absent_blocks() -> None:
    attachment = Attachment(mime_descriptor="image/png", data=b"hi")
    block = {
        "type": "image",
        "source": {"type": "base64", "data": None, "$held": ["data"]},
    }
    assert claude._write_attachment_shape(block, None) == block
    assert (
        from_plain(
            claude._write_attachment_shape(block, attachment)["source"],
            dict[str, object],
        )["data"]
        == "aGk="
    )
    assert claude._write_thinking_shape({"type": "thinking"}, None) == {
        "type": "thinking",
    }
    assert claude._write_agent_blocks(
        ToolMessage(
            call_id="c",
            state="done",
            stdout=("hi",),
            result=AgentSpawnResult(),
        ),
        {},
    ) == [
        {"type": "text", "text": "hi"},
    ]
    assert (
        claude._result_values(
            ToolMessage(call_id="c", state="done", result=UncategorizedToolResult()),
            {},
        )
        == {}
    )
    assert claude._write_result_content(
        ToolMessage(
            call_id="c",
            state="done",
            stdout=("new",),
            result=UncategorizedToolResult(),
        ),
        [{"type": "text", "text": "old"}, {"type": "other"}],
    ) == [{"type": "text", "text": "new"}, {"type": "other"}]


def test_agent_blocks_replay_edited_prose_inside_their_boundaries() -> None:
    picture = {"type": "image", "id": 1}
    shape = {
        "agent_blocks": [
            {"type": "text", "text": "a\nb"},
            picture,
            None,
            {"type": "text", "text": "c"},
        ],
    }
    report = ToolMessage(
        call_id="c",
        state="done",
        stdout=("x\ny", "\nz"),
        result=AgentSpawnResult(),
    )

    assert claude._write_agent_blocks(report, shape) == [
        {"type": "text", "text": "x\ny"},
        picture,
        None,
        {"type": "text", "text": "z"},
    ]


def test_agent_blocks_keep_their_own_text_when_the_prose_no_longer_fits() -> None:
    shape = {
        "agent_blocks": [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
            {"type": "text", "text": None},
        ],
    }
    report = ToolMessage(
        call_id="c",
        state="done",
        stdout=("whole",),
        result=AgentSpawnResult(),
    )

    assert claude._write_agent_blocks(report, shape) == [
        {"type": "text", "text": "whole"},
        {"type": "text", "text": "b"},
        {"type": "text", "text": ""},
    ]


def test_a_crossed_agent_report_takes_the_finished_reports_order() -> None:
    """With no ``isAsync`` it is a finished report, which states its prompt second."""
    report = ToolMessage(
        call_id="c",
        state="done",
        stdout=("done",),
        result=AgentSpawnResult(agent_id="a1", state="completed", prompt="p", tokens=3),
    )
    payload = from_plain(claude._write_tool_payload(report, {}, {}), dict[str, object])
    assert list(payload) == ["status", "prompt", "agentId", "content", "totalTokens"]


def test_a_crossed_finished_agent_replays_with_claudes_name_and_order() -> None:
    report = ToolMessage(
        call_id="c",
        state="done",
        stdout=("done",),
        result=AgentSpawnResult(agent_id="a1", state="completed", prompt="p", tokens=3),
    )
    stream = StringIO()
    claude.denormalize(
        [
            SessionContext(encoding={"newline_terminated": False}),
            ToolCall(call_id="c", name="spawn_agent"),
            report,
        ],
        stream,
    )
    lines = [
        json_loads(line, dict[str, object]) for line in stream.getvalue().splitlines()
    ]
    assistant = next(line for line in lines if line.get("type") == "assistant")
    message = from_plain(assistant.get("message"), dict[str, object])
    blocks = from_plain(message.get("content"), list[object])
    call = from_plain(blocks[0], dict[str, object])
    result = next(line for line in lines if "toolUseResult" in line)
    payload = from_plain(result.get("toolUseResult"), dict[str, object])

    assert call["name"] == "Agent"
    assert list(payload) == ["status", "prompt", "agentId", "content", "totalTokens"]


def test_an_mcp_result_is_written_as_the_tool_claude_named_it() -> None:
    assert claude._claude_tool(
        ToolMessage(
            call_id="c",
            state="done",
            result=McpResult(server="docs", tool="search"),
        ),
    ) == ("mcp__docs__search")


def test_claude_writer_special_result_shapes() -> None:
    assert (
        claude._write_result_block(
            ToolMessage(
                call_id="c",
                state="done",
                result=UncategorizedToolResult(),
                extra={"$result": {"$text": True}},
            ),
        )["content"]
        == ""
    )
    assert (
        claude._write_tool_payload(
            ToolMessage(call_id="c", state="done", result=UncategorizedToolResult()),
            {"toolUseResult": "failed"},
            {},
        )
        == "failed"
    )
    assert claude._write_blocks(
        (Thinking(content="t"), ToolCall(call_id="c", name="x")),
        1,
        {},
    ) == [
        {"type": "thinking", "thinking": "t"},
        {"type": "tool_use", "id": "c", "name": "x", "input": {}},
    ]
    assert claude._write_thinking(
        Thinking(summary="summary", extra={"$signature_present": True}),
    ) == {
        "type": "thinking",
        "thinking": "summary",
        "signature": "",
    }
    assert claude._write_thinking(Thinking(encrypted="sig")) == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig",
    }


def test_a_line_context_reads_what_the_line_states_and_keeps_the_rest() -> None:
    previous = SessionContext(
        model="old",
        effort="low",
        permission="ask",
        extra={"cwd": "/w"},
    )

    kept = claude._read_line_context({}, previous)
    stated = claude._read_line_context(
        {"permissionMode": "plan", "effort": 7},
        previous,
    )

    assert (kept.model, kept.effort, kept.permission) == ("old", "low", "ask")
    assert dict(kept.extra) == {"cwd": "/w"}
    # A stated field replaces the prior one, even when its value is unreadable.
    assert (stated.permission, stated.effort) == ("plan", None)


def test_claude_reader_context_and_result_edge_cases() -> None:
    previous = SessionContext(model="old", effort="low", permission="ask")
    updated = claude._read_line_context(
        {"message": {"model": "new"}, "effort": "high"},
        previous,
    )
    assert (updated.model, updated.effort, updated.permission) == ("new", "high", "ask")
    assert claude._parse("{") is None
    assert claude._effort(7) is None


def test_a_line_stating_no_settings_keeps_the_previous_ones() -> None:
    previous = SessionContext(
        model="m",
        effort="low",
        permission="ask",
        extra={"gitBranch": "main"},
    )

    kept = claude._read_line_context({"type": "user"}, previous)

    assert kept == replace(previous, extra={"gitBranch": "main"})


def test_a_line_s_own_settings_replace_the_previous_ones() -> None:
    previous = SessionContext(model="m", effort="low", permission="ask")

    updated = claude._read_line_context(
        {"permissionMode": "plan", "effort": "max", "message": {"model": "n"}},
        previous,
    )

    assert (updated.model, updated.effort, updated.permission) == ("n", "max", "plan")


def test_a_null_model_clears_the_previous_one() -> None:
    previous = SessionContext(model="m")

    updated = claude._read_line_context({"message": {"model": None}}, previous)

    assert updated.model is None


@pytest.mark.parametrize(
    "record",
    [{"permissionMode": 7}, {"message": {"model": 7}}],
)
def test_a_malformed_setting_is_a_read_error(record: dict[str, object]) -> None:
    # The line's guard keeps it as its bytes; a guessed setting would be wrong.
    with pytest.raises(ReadError):
        claude._read_line_context(record, SessionContext(model="m"))


def test_a_line_with_malformed_settings_round_trips() -> None:
    # A non-string setting raised out of ``normalize`` and lost the file.
    native = _line(
        type='"user"',
        permissionMode="7",
        message='{"role":"user","content":"hi"}',
    ) + _line(
        type='"assistant"',
        message='{"role":"assistant","model":7,"content":[]}',
    )
    output = StringIO()

    claude.denormalize(claude.normalize(StringIO(native)), output)

    assert output.getvalue() == native


@pytest.mark.parametrize(
    "native",
    [
        _line(type='"assistant"', message='{"role":"assistant","content":7}'),
        _line(type='"assistant"', message='{"role":7,"content":[]}'),
        _line(type='"user"', message='{"role":"user","content":[7]}'),
        _line(type='"attachment"', attachment="[7]"),
        _line(type="7", message='{"role":"user","content":"x"}'),
        _line(
            type='"user"',
            message=(
                '{"role":"user","content":[{"type":"tool_result",'
                '"tool_use_id":"c","content":"x"}]}'
            ),
            toolUseResult='{"type":"text","file":7}',
        ),
    ],
    ids=["content", "role", "block", "attachment", "type", "tool-file"],
)
def test_a_line_with_a_malformed_field_round_trips(native: str) -> None:
    # One malformed field raised ``ReadError`` out of ``normalize`` -- or out
    # of the writer -- and lost the whole file.
    output = StringIO()

    claude.denormalize(claude.normalize(StringIO(native)), output)

    assert output.getvalue() == native


def test_a_system_line_reads_its_own_fields() -> None:
    (message,) = claude._read_record(
        {
            "type": "system",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "content": "c",
            "subtype": "s",
        },
        {},
    )

    assert isinstance(message, SystemMessage)
    assert (message.timestamp, message.content, message.subtype) == (
        datetime(2026, 1, 1, tzinfo=UTC),
        "c",
        "s",
    )
    assert not {"content", "subtype"} & set(message.extra)
    assert (
        claude._carries_a_compaction(
            UserMessage(extra={"isCompactSummary": True}),
        )
        is True
    )
    assert claude._numbered("a\nb") == "1\ta\n2\tb"


def test_a_system_line_reads_a_mistyped_field_as_absent() -> None:
    (message,) = claude._read_record(
        {"type": "system", "timestamp": 5, "content": 7, "subtype": ["s"]},
        {},
    )

    assert isinstance(message, SystemMessage)
    assert (message.timestamp, message.content, message.subtype) == (None, None, None)
    assert (message.extra["$content_value"], message.extra["$subtype_value"]) == (
        7,
        ("s",),
    )


@pytest.mark.parametrize(
    ("records", "cwd"),
    [
        (
            (
                SessionContext(extra={"payload": {"cwd": 7}}),
                SessionContext(extra={"cwd": "/work"}),
            ),
            "/work",
        ),
        ((SessionContext(cwd=Path("/sagent"), extra={"cwd": "/other"}),), "/sagent"),
        ((SessionContext(extra={"payload": {"cwd": 7}}),), ""),
    ],
    ids=["envelope", "sagent", "none"],
)
def test_the_first_context_naming_a_directory_names_the_cwd(
    records: tuple[SessionRecord, ...],
    cwd: str,
) -> None:
    assert claude._stated_cwd(records) == cwd


def test_claude_writer_handles_special_record_shapes(tmp_path: Path) -> None:
    assert claude._rename_calls({"message": 7}, {}) is None
    assert claude._rename_calls({"message": {"content": {}}}, {}) is None
    assert (
        claude._rename_calls(
            {"message": {"content": ["text", {"type": "tool_use", "id": "c"}]}},
            {"c": "Bash"},
        )
        is None
    )
    assert (
        claude._write_group(
            (TokenUsage(info={"input_tokens": 1}),),
            SessionContext(),
            {},
        )
        is None
    )
    assert (
        claude._write_group((AgentMessage(content="hi"),), SessionContext(), {}) is None
    )
    assert (
        claude._write_group((SystemMessage(content="bad"),), SessionContext(), {})
        is not None
    )
    assert (
        claude._write_group(
            (UncategorizedRecord(kind="foreign"),),
            SessionContext(),
            {},
        )
        == {}
    )
    assert claude._splice(
        ["type"],
        {"type": "user"},
        {"$source_order": True, "future": 1, "$sessionId": "s"},
        {},
    ) == {"type": "user", "future": 1, "sessionId": "s"}
    assert claude._write_tool_call_shape({"type": "tool_use", "id": "c"}, None) == {
        "type": "tool_use",
        "id": "c",
    }
    assert (
        from_plain(
            claude._write_attachment(
                Attachment(mime_descriptor="image/png", data=b"x"),
                media_first=True,
            )["source"],
            dict[str, object],
        )["media_type"]
        == "image/png"
    )
    assert (
        from_plain(
            claude._write_attachment(
                Attachment(mime_descriptor="image/png", data=b"x"),
                media_first=False,
            )["source"],
            dict[str, object],
        )["data"]
        == "eA=="
    )
    assert (
        from_plain(
            claude._write_user(
                UserMessage(content="hi", extra={"promptSource": "user"}),
                SessionContext(permission="ask"),
                {},
            ),
            dict[str, object],
        )["permissionMode"]
        == "ask"
    )
    assert claude._split("a\nb", 2) == ["a", "b"]
    assert claude._split("a\nb", 3) == ["a\nb"]
    assert claude._write_tool_call_shape(
        {
            "type": "tool_use",
            "id": "old",
            "name": "old",
            "input": {},
            "$held": ["id", "name", "input"],
        },
        ToolCall(
            call_id="new",
            name="Read",
            arguments={"path": str(tmp_path / "input")},
        ),
    ) == {
        "type": "tool_use",
        "id": "new",
        "name": "Read",
        "input": {"path": str(tmp_path / "input")},
    }


def test_only_a_line_that_escapes_unicode_carries_the_flag() -> None:
    """Raw UTF-8 is claude's own; an escaped line says so itself, on its own record."""
    raw = '{"type":"user","message":{"role":"user","content":"é"}}\n'
    plain = '{"type":"user","message":{"role":"user","content":"a\\u001b"}}\n'
    escaped = '{"type":"user","message":{"role":"user","content":"\\u00e9"}}\n'
    text = raw + plain * 9 + escaped + raw

    records = list(claude.normalize(StringIO(text)))
    out = StringIO()
    claude.denormalize(records, out)

    stated = [
        r.encoding for r in records if isinstance(r, SessionContext) and r.encoding
    ]
    users = [r for r in records if isinstance(r, UserMessage)]
    assert [dict(e) for e in stated] == [
        {
            "newline_terminated": True,
            "ascii_escaped": False,
            "ascii_escape_exceptions": "",
        },
    ]
    assert [i for i, r in enumerate(users) if "$ascii_escaped" in r.extra] == [10]
    assert users[10].extra["$ascii_escaped"] is True
    assert out.getvalue() == text


def test_an_all_escaped_transcript_states_its_spelling_once() -> None:
    escaped = '{"type":"user","message":{"role":"user","content":"\\u00e9"}}\n'
    text = escaped * 1000

    records = list(claude.normalize(StringIO(text)))
    out = StringIO()
    claude.denormalize(records, out)

    assert (
        len([r for r in records if isinstance(r, SessionContext) and r.encoding]) == 1
    )
    assert out.getvalue() == text


def test_an_unparsed_line_keeps_its_bytes_and_needs_no_mark() -> None:
    unparsed = IncompleteRecord(text="{torn\n")

    assert claude._marked_escaped(unparsed) is unparsed


def test_an_escaped_line_of_a_type_claude_alone_writes_keeps_its_spelling() -> None:
    escaped = '{"type":"summary","summary":"\\u00e9"}\n'
    raw = '{"type":"summary","summary":"é"}\n'

    records = list(claude.normalize(StringIO(escaped + raw)))
    out = StringIO()
    claude.denormalize(records, out)

    assert out.getvalue() == escaped + raw


def test_claude_writer_escapes_each_line_by_its_own_bit() -> None:
    """Every written line advances the bitmap index, not only the first."""
    stream = StringIO()
    writer = claude._Emitter(
        stream,
        escaped_default=False,
        exceptions=bytes([0b100]),
        newline_terminated=True,
    )
    for _ in range(3):
        writer.add({"t": "é"})
    writer.close()

    assert stream.getvalue().splitlines() == [
        '{"t":"é"}',
        '{"t":"é"}',
        '{"t":"\\u00e9"}',
    ]


def test_claude_writer_reads_a_bit_past_the_first_byte() -> None:
    stream = StringIO()
    writer = claude._Emitter(
        stream,
        escaped_default=False,
        exceptions=bytes([0, 0b1]),
        newline_terminated=True,
    )
    for _ in range(9):
        writer.add({"t": "é"})
    writer.close()

    assert [line.isascii() for line in stream.getvalue().splitlines()] == [
        *([False] * 8),
        True,
    ]


def test_releasing_one_line_keeps_another_line_s_open_call_editable() -> None:
    stream = StringIO()
    writer = claude._Emitter(
        stream,
        escaped_default=False,
        exceptions=b"",
        newline_terminated=True,
        lookahead=2,
    )
    read = ToolMessage(
        call_id="c",
        state="done",
        result=ReadResult(path="b.txt"),
        extra={"$shell": True},
    )
    edited = rewrite_shell_source("cat a.txt", ReadResult(path="b.txt"))
    call_line: dict[str, object] = {
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "c",
                    "name": "Bash",
                    "input": {"command": "cat a.txt"},
                },
            ],
        },
    }

    writer.add({"n": 0})
    writer.add(call_line, calls=(ToolCall(call_id="c", name="Bash"),))
    writer.add({"n": 1})
    writer.add({"n": 2}, record=read)
    writer.close()

    assert edited != "cat a.txt"
    assert json.dumps(edited) in stream.getvalue()


def test_claude_writer_keeps_lookahead_lines_editable() -> None:
    """``lookahead`` lines stay queued; only a line past them is released."""
    stream = StringIO()
    writer = claude._Emitter(
        stream,
        escaped_default=False,
        exceptions=b"",
        newline_terminated=True,
        lookahead=2,
    )
    for n in range(3):
        writer.add({"n": n})

    # Released lines are held one behind, so the first reaches the stream only
    # after a second is released: three adds with a window of two release one.
    writer.add({"n": 3})
    assert stream.getvalue() == '{"n":0}\n'


def test_claude_writer_skips_non_string_shell_commands() -> None:
    stream = StringIO()
    writer = claude._Emitter(
        stream,
        escaped_default=False,
        exceptions=b"",
        newline_terminated=True,
    )
    line: dict[str, object] = {
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "c",
                    "name": "Bash",
                    "input": {"command": 7},
                },
            ],
        },
    }
    call = ToolCall(call_id="c", name="Bash")
    writer.add(line, calls=(call,))
    writer._apply_shell_edit(
        ToolMessage(
            call_id="c",
            state="done",
            stdout=("Wrote x.",),
            result=WriteResult(path="x", content="new"),
            extra={"$shell": True},
        ),
    )
    assert line == {
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "c",
                    "name": "Bash",
                    "input": {"command": 7},
                },
            ],
        },
    }


def test_claude_user_shape_replays_results_and_stencils() -> None:
    result = ToolMessage(
        call_id="c",
        state="done",
        stdout=("done",),
        result=UncategorizedToolResult(),
    )
    shape = [
        {"type": "tool_result", "tool_use_id": "c", "$held": ["content"]},
        {"type": "text", "text": "old", "$held": ["text"]},
        {"type": "other"},
    ]
    assert claude._write_user_shape(UserMessage(content="new"), shape, (result,)) == [
        {"type": "tool_result", "tool_use_id": "c", "content": "done"},
        {"type": "text", "text": "new"},
        {"type": "other"},
    ]
    assert claude._write_user_shape(
        UserMessage(content="new"),
        [{"type": "other"}],
    ) == [
        {"type": "other"},
        {"type": "text", "text": "new"},
    ]
    assert claude._write_user_shape(
        UserMessage(content="new"),
        [{"type": "other", "$held": ["value"]}],
    ) == [{"type": "other"}, {"type": "text", "text": "new"}]
    assert claude._write_user_shape(UserMessage(content="new"), [7]) == [
        7,
        {"type": "text", "text": "new"},
    ]
    result_line = from_plain(
        claude._write_result(
            ToolMessage(
                call_id="c",
                state="done",
                stdout=("done",),
                result=UncategorizedToolResult(),
                extra={"$content_shape": [{"type": "other"}]},
            ),
            {},
        ),
        dict[str, object],
    )
    assert from_plain(result_line["message"], dict[str, object])["content"] == [
        {"type": "other"},
    ]


def test_a_foreign_call_restated_after_its_typed_result_is_written_once() -> None:
    """A real call arriving after its synthesized one is not a second ``tool_use``.

    Codex can state the same call twice -- once ahead of its result, once
    after -- and only the first sighting of a written-ahead id may register.
    """
    records = [
        _FOREIGN,
        ToolMessage(call_id="a", state="done", result=BashResult(command=("ls",))),
        ToolCall(call_id="a", name="shell"),
        ToolCall(call_id="a", name="shell"),
    ]

    assert _tool_uses(records) == ["a"]


def test_a_foreign_call_before_its_result_takes_the_claude_tool_name() -> None:
    """The call stays where it was said, ahead of the prose after it."""
    records = [
        _FOREIGN,
        ToolCall(call_id="a", name="shell"),
        AssistantMessage(content="running"),
        ToolMessage(call_id="a", state="done", result=BashResult(command=("ls",))),
    ]
    output = StringIO()

    claude.denormalize(records, output)

    written = output.getvalue()
    assert written.count('"type":"tool_use"') == 1
    assert '"name":"Bash"' in written
    assert written.index('"type":"tool_use"') < written.index('"running"')


def test_a_session_with_no_lines_writes_nothing() -> None:
    output = StringIO()

    claude.denormalize([SessionContext()], output)

    assert output.getvalue() == ""


def test_a_foreign_cwd_stated_on_the_launch_settings_names_the_project() -> None:
    records = [
        SessionContext(
            encoding={"newline_terminated": True},
            extra={"payload": {"cwd": "/launch"}},
        ),
        UserMessage(content="hi"),
    ]
    output = StringIO()

    claude.denormalize(records, output)

    assert '"cwd":"/launch"' in output.getvalue()


def test_a_long_session_streams_through_a_bounded_window() -> None:
    """More lines than the edit window flush as they pass, still in order."""
    native = "".join(
        _line(type='"user"', message=f'{{"role":"user","content":"m{index}"}}')
        for index in range(40)
    )

    assert _round_trip(native) == native


def test_a_shell_edit_for_a_call_that_left_the_window_replays_unedited() -> None:
    """A lifted result whose call already flushed cannot reach back to it."""
    call = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"Bash","input":{"command":"/bin/cat old.txt"}}]}'
        ),
    )
    filler = "".join(
        _line(type='"user"', message=f'{{"role":"user","content":"m{index}"}}')
        for index in range(40)
    )
    result = _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"body\\n"}]}'
        ),
        toolUseResult='{"stdout":"body\\n","stderr":"","interrupted":false}',
    )
    records = list(claude.normalize(StringIO(call + filler + result)))
    read = _result(_last_act(records))
    changed = [
        replace(read, result=replace(_typed_result(read, ReadResult), path="new.txt"))
        if record is read
        else record
        for record in records
    ]
    output = StringIO()

    claude.denormalize(changed, output)

    assert '"command":"/bin/cat old.txt"' in output.getvalue()


def test_another_providers_uncategorized_record_writes_no_claude_line() -> None:
    records = [UserMessage(content="hi"), UncategorizedRecord(kind="event_msg/x")]
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue().count("\n") == 1


def test_edited_text_beyond_the_stated_blocks_is_not_invented() -> None:
    """A line led by a result refills its text slots, and drops ones left empty."""
    native = _line(
        type='"user"',
        message=(
            '{"role":"user","content":['
            '{"type":"tool_result","tool_use_id":"c","content":"x"},'
            '{"type":"text","text":"a"}]}'
        ),
    )
    records = list(claude.normalize(StringIO(native)))
    edited = [
        replace(record, content=None) if isinstance(record, UserMessage) else record
        for record in records
    ]
    output = StringIO()

    claude.denormalize(edited, output)

    assert _round_trip(native) == native
    assert '"type":"text"' not in output.getvalue()


def test_search_groups_replay_prose_and_scalar_rows_around_groups() -> None:
    payload = (
        '{"query":"q","results":["summary",{"tool_use_id":"s1","content":'
        '[7,{"url":"u1"}]}]}'
    )
    native = _line(
        type='"assistant"',
        message=(
            '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
            '"name":"WebSearch","input":{}}]}'
        ),
    ) + _line(
        type='"user"',
        message=(
            '{"role":"user","content":[{"type":"tool_result",'
            '"tool_use_id":"c1","content":"ok"}]}'
        ),
        toolUseResult=payload,
    )

    assert _round_trip(native) == native


def test_a_search_shape_entry_without_a_template_replays_verbatim() -> None:
    search = ToolMessage(
        call_id="c1",
        state="done",
        result=WebSearchResult(query="q"),
        extra={"$result": {"tool_name": "WebSearch", "rows": [{"loose": 1}]}},
    )
    output = StringIO()

    claude.denormalize([search], output)

    assert '"results":[{"loose":1}]' in output.getvalue()


def test_the_turns_model_and_effort_fill_a_line_that_states_neither() -> None:
    records = [
        SessionContext(model="claude-opus-5", effort="high"),
        AssistantMessage(content="hi"),
    ]
    output = StringIO()

    claude.denormalize(records, output)

    line = json_loads(output.getvalue(), dict[str, object])
    assert line["effort"] == "high"
    assert from_plain(line["message"], dict[str, object])["model"] == "claude-opus-5"


def test_a_compact_summary_line_opens_a_window_carrying_it() -> None:
    native = _line(type='"user"', message='{"role":"user","content":"hi"}') + _line(
        type='"user"',
        isCompactSummary="true",
        message='{"role":"user","content":"the summary"}',
        timestamp='"2026-08-24T00:00:00.000Z"',
    )

    records = list(claude.normalize(StringIO(native)))

    clears = [record for record in records if isinstance(record, ContextClear)]
    [compaction] = [r for r in records if isinstance(r, ContextCompaction)]
    # Both are in the summary's context and at its time.
    at = datetime(2026, 8, 24, tzinfo=UTC)
    kept = {"$in_place": True, "$timestamp_as_written": "2026-08-24T00:00:00.000Z"}
    assert compaction == ContextCompaction(context_id=2, timestamp=at, extra=kept)
    assert clears[-1] == ContextClear(
        context_id=2,
        timestamp=at,
        summary="the summary",
        extra=kept,
    )
    # Each is stored as the line spelled its time.
    assert [stored_plain(r)["timestamp"] for r in (compaction, clears[-1])] == [
        "2026-08-24T00:00:00.000Z",
        "2026-08-24T00:00:00.000Z",
    ]
    assert _round_trip(native) == native


def test_an_automatic_compaction_reads_as_its_boundary_then_the_window_it_opened() -> (
    None
):
    """Claude 2.1.283's lines for an automatic compaction, anonymised: the boundary
    names how it began and how long it took, the summary opens the next window.
    """
    native = (_CWD / "testdata" / "compaction" / "claude_auto.jsonl").read_text()

    records = list(claude.normalize(StringIO(native)))

    [boundary] = [r for r in records if isinstance(r, SystemMessage)]
    assert (boundary.subtype, boundary.content) == (
        "compact_boundary",
        "Conversation compacted",
    )
    metadata = from_plain(boundary.extra["compactMetadata"], dict[str, object])
    assert (metadata["trigger"], metadata["durationMs"]) == ("auto", 100_076)
    acts = [r for r in records if not isinstance(r, SessionContext | ContextState)]
    assert [type(r).__name__ for r in acts] == [
        "ContextClear",
        "SystemMessage",
        "ContextCompaction",
        "ContextClear",
        "UserMessage",
    ]
    clear = acts[3]
    assert isinstance(clear, ContextClear)
    assert clear.summary is not None
    assert clear.summary.endswith("1. Primary Request: fix the flaky test.")
    assert _round_trip(native) == native


@pytest.mark.parametrize("native", ["\n", "[1]\n"], ids=["blank", "non-object"])
def test_a_line_holding_no_record_survives_verbatim(native: str) -> None:
    records = list(claude.normalize(StringIO(native)))

    assert IncompleteRecord(text=native) in records
    assert _round_trip(native) == native


@pytest.mark.parametrize(
    ("parts", "blocks"),
    [
        (2, [{"type": "text", "text": "a b"}, {"type": "text", "text": "c"}]),
        (1, [{"type": "text", "text": "a b\nc"}]),
        (3, [{"type": "text", "text": "a b\nc"}]),
    ],
)
def test_prose_splits_into_its_stated_blocks_on_line_breaks_only(
    parts: int,
    blocks: list[dict[str, str]],
) -> None:
    """A block count that does not match the line breaks keeps the prose whole."""
    output = StringIO()

    claude.denormalize(
        [AssistantMessage(content="a b\nc", extra={"$parts": parts})],
        output,
    )

    message = json_loads(output.getvalue(), dict[str, object])["message"]
    assert from_plain(message, dict[str, object])["content"] == blocks


def test_an_assistant_line_writing_keys_out_of_order_round_trips() -> None:
    native = _line(
        type='"assistant"',
        message='{"content":[{"type":"text","text":"hi"}],"role":"assistant"}',
    )

    assert _round_trip(native) == native


def test_claude_user_reader_preserves_unknown_blocks() -> None:
    records = claude._read_user(
        {"type": "user", "timestamp": "t"},
        {"role": "user", "content": {"future": 1}},
        {},
    )
    assert isinstance(records[0], UserMessage)
    assert records[0].extra["$content_value"] == {"future": 1}


# ``A`` is a line that spells its text in ``\u`` escapes, ``U`` one in raw UTF-8, and
# ``M`` raw UTF-8 beside a control character's escape, which every encoder writes: the
# escaped lines are the exceptions, however many there are and wherever they come.
@pytest.mark.parametrize(
    "pattern",
    ["AUUAAAAUUUUUUAU", "UAAUUUUAAAAAAUA", "AAAAAAAAAU", "UA", "AMU"],
)
def test_escaped_and_raw_lines_each_write_back_as_they_were_read(pattern: str) -> None:
    escaped = "caf" + chr(92) + "u00e9"  # codespell:ignore caf.
    raw = "caf" + chr(0xE9)  # codespell:ignore caf.
    content = {"A": escaped, "U": raw, "M": raw + chr(92) + "u001b[0m"}
    native = "".join(
        _line(
            type='"user"',
            message=f'{{"role":"user","content":"{content[kind]}"}}',
        )
        for kind in pattern
    )

    records = list(claude.normalize(StringIO(native)))
    output = StringIO()
    claude.denormalize(records, output)

    users = [r for r in records if isinstance(r, UserMessage)]
    assert output.getvalue() == native
    assert [i for i, r in enumerate(users) if "$ascii_escaped" in r.extra] == [
        i for i, kind in enumerate(pattern) if kind == "A"
    ]


def test_a_prefix_of_a_transcript_reads_as_a_prefix_of_its_records() -> None:
    """An upload resumes a grown file at the record count it stored."""
    escaped = "caf" + chr(92) + "u00e9"  # codespell:ignore caf.
    lines = [
        _line(
            type='"user"',
            message=f'{{"role":"user","content":"{escaped if n in (3, 5) else n}"}}',
        )
        for n in range(8)
    ]
    whole = list(claude.normalize(StringIO("".join(lines))))

    for cut in range(1, len(lines)):
        part = list(claude.normalize(StringIO("".join(lines[:cut]))))
        assert whole[: len(part)] == part, cut


def test_a_spelling_stored_escaped_by_default_writes_its_raw_lines_raw() -> None:
    """Records stored before lines carried their own flag: the file's bitmap says."""
    escaped = "caf" + chr(92) + "u00e9"  # codespell:ignore caf.
    raw = "caf" + chr(0xE9)  # codespell:ignore caf.
    native = "".join(
        _line(type='"user"', message=f'{{"role":"user","content":"{content}"}}')
        for content in (escaped, raw, escaped)
    )
    records = [
        replace(
            record,
            encoding={
                "newline_terminated": True,
                "ascii_escaped": True,
                "ascii_escape_exceptions": base64.b64encode(b"\x02").decode(),
            },
        )
        if isinstance(record, SessionContext) and record.encoding
        else replace(
            record,
            extra={k: v for k, v in record.extra.items() if k != "$ascii_escaped"},
        )
        if isinstance(record, UserMessage)
        else record
        for record in claude.normalize(StringIO(native))
    ]
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue() == native


# The spelling is stated before the first act; a line that escapes says so on its own
# record, so nothing is restated for it. Lines 2 and 9 escape here.
def test_the_spelling_is_stated_first_and_an_escaped_line_restates_nothing() -> None:
    escaped = "caf" + chr(92) + "u00e9"  # codespell:ignore caf.
    native = "".join(
        _line(
            type='"user"',
            message=f'{{"role":"user","content":"{escaped if n in (2, 9) else n}"}}',
        )
        for n in range(10)
    )

    records = list(claude.normalize(StringIO(native)))

    encodings = [
        r.encoding for r in records if isinstance(r, SessionContext) and r.encoding
    ]
    assert [dict(e) for e in encodings] == [
        {
            "newline_terminated": True,
            "ascii_escaped": False,
            "ascii_escape_exceptions": "",
        },
    ]
    assert isinstance(records[0], SessionContext)
    assert records[0].encoding == encodings[0]
    assert isinstance(records[1], ContextClear)
    assert records[1].extra == {"$opens": True}
    settings = [r for r in records if isinstance(r, SessionContext) and not r.encoding]
    assert len(settings) == 1


def test_a_transcript_without_a_final_newline_says_so_and_writes_back() -> None:
    native = _line(type='"user"', message='{"role":"user","content":"a"}') + _line(
        type='"user"',
        message='{"role":"user","content":"b"}',
    ).removesuffix("\n")

    records = list(claude.normalize(StringIO(native)))
    output = StringIO()
    claude.denormalize(records, output)

    encodings = [
        r.encoding for r in records if isinstance(r, SessionContext) and r.encoding
    ]
    assert encodings[-1]["newline_terminated"] is False
    assert output.getvalue() == native


def test_a_tailed_file_restates_its_newline_only_when_it_changes() -> None:
    lines = [
        _line(type='"user"', message=f'{{"role":"user","content":"{n}"}}')
        for n in range(4)
    ]
    tail = Tail(claude.normalize)

    records = [
        record
        for chunk in (lines[0].removesuffix("\n"), *lines[1:])
        for record in tail.feed(chunk)
    ]

    assert [
        r.encoding["newline_terminated"]
        for r in records
        if isinstance(r, SessionContext) and r.encoding
    ] == [True, False, True]


def test_what_the_harness_gives_at_the_head_is_the_opening_clears_prompt() -> None:
    native = (
        json.dumps(
            {
                "type": "attachment",
                "attachment": {"type": "skill_listing", "content": "- tdd: write it"},
                "uuid": "11111111-1111-1111-1111-111111111111",
                "timestamp": "2026-01-01T00:00:00Z",
            },
            separators=(",", ":"),
        )
        + "\n"
        # One that gives no prose, then one more that does.
        + _line(type='"attachment"', attachment='{"type":"todo","content":[]}')
        + _line(
            type='"attachment"',
            attachment='{"type":"agent_listing","content":"- scout: look"}',
        )
        + _line(type='"user"', message='{"role":"user","content":"hi"}')
    )

    records = list(claude.normalize(StringIO(native)))

    opening = records[1]
    assert isinstance(opening, ContextClear)
    assert opening.system_prompt == "- tdd: write it\n- scout: look"


def test_an_assistant_line_reads_as_one_record_for_each_block_it_holds() -> None:
    message: dict[str, object] = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "hm", "signature": "sig"},
            {"type": "thinking", "thinking": "so"},
            {"type": "tool_use", "caller": {"type": "direct"}},
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
            {"type": "server_tool_use", "id": "s1"},
        ],
    }

    read = claude._read_assistant({"type": "assistant", "message": message}, message)

    assert read == [
        Thinking(
            content="hm",
            encrypted="sig",
            extra={
                "$keys": ["type", "message"],
                "$content_shape": [
                    {
                        "type": "thinking",
                        "thinking": None,
                        "signature": None,
                        "$held": ["thinking", "signature"],
                    },
                    {"type": "thinking", "thinking": None, "$held": ["thinking"]},
                    {"type": "tool_use", "caller": {"type": "direct"}},
                    {"type": "text", "text": None, "$held": ["text"]},
                    {"type": "text", "text": None, "$held": ["text"]},
                    {"type": "server_tool_use", "id": None, "$held": ["id"]},
                ],
                "$parts": 2,
                "$blocks": [{"type": "server_tool_use", "id": "s1"}],
                "$signature_present": True,
            },
        ),
        Thinking(content="so"),
        ToolCall(
            call_id="",
            name="",
            arguments={},
            extra={"$call": {"caller": {"type": "direct"}}},
        ),
        # The words sit where the line said them: after what it thought and called.
        AssistantMessage(content="one\ntwo"),
    ]


def test_an_assistant_line_of_one_text_block_keeps_no_shape_of_it() -> None:
    message: dict[str, object] = {
        "model": "claude-opus-5",
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 1},
    }

    read = claude._read_assistant({"type": "assistant", "message": message}, message)

    assert read == [
        AssistantMessage(
            content="hi",
            extra={
                "$keys": ["type", "message"],
                "message": {"model": "claude-opus-5"},
            },
        ),
        TokenUsage(info={"input_tokens": 1}),
    ]


def test_an_assistant_line_with_no_content_is_still_a_turn() -> None:
    message: dict[str, object] = {"role": "assistant"}

    read = claude._read_assistant({"type": "assistant", "message": message}, message)

    assert read == [
        AssistantMessage(extra={"$keys": ["type", "message"], "$content_shape": []}),
    ]


def test_a_user_line_reads_as_its_words_and_what_it_attached() -> None:
    message: dict[str, object] = {
        "role": "user",
        "content": [
            {"type": "text", "text": "one"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
            },
            {"type": "text", "text": "two"},
            "junk",
        ],
    }

    read = claude._read_user({"type": "user", "message": message}, message, {})

    assert read == [
        UserMessage(
            content="one\ntwo",
            attachments=(Attachment(mime_descriptor="image/png", data=b"hi"),),
            extra={
                "$keys": ["type", "message"],
                "$content_shape": [
                    {"type": "text", "text": None, "$held": ["text"]},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": None,
                            "data": None,
                            "$held": ["data", "media_type"],
                        },
                    },
                    {"type": "text", "text": None, "$held": ["text"]},
                    "junk",
                ],
                "$parts": 2,
                "$media_first": True,
            },
        ),
    ]


def test_a_user_line_of_one_block_counts_no_parts() -> None:
    message: dict[str, object] = {
        "role": "user",
        "content": [{"type": "text", "text": "one"}],
    }

    read = claude._read_user({"type": "user", "message": message}, message, {})

    assert read == [
        UserMessage(
            content="one",
            extra={
                "$keys": ["type", "message"],
                "$content_shape": [{"type": "text", "text": None, "$held": ["text"]}],
            },
        ),
    ]


@pytest.mark.parametrize(
    ("blocks", "expected"),
    [
        (
            [
                {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
                {"type": "other"},
            ],
            [ToolMessage],
        ),
        (
            [
                {"type": "tool_result", "tool_use_id": "c1", "content": "ok"}
                | {"text": "t"},
            ],
            [ToolMessage],
        ),
        (
            [
                {"type": "image", "source": {"type": "base64", "data": "aGk="}},
                {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
            ],
            [UserMessage, ToolMessage],
        ),
    ],
    ids=["beside-a-block-with-no-words", "holding-words", "after-an-image"],
)
def test_a_tool_answer_is_a_message_only_beside_words_or_an_image(
    blocks: list[object],
    expected: list[type[object]],
) -> None:
    message: dict[str, object] = {"role": "user", "content": blocks}

    read = claude._read_user({"type": "user", "message": message}, message, {})

    assert [type(act) for act in read] == expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            {"type": "attachment", "attachment": {"type": "hook", "text": "t"}},
            ContextState(
                kind="hook",
                content="t",
                extra={"$keys": ["type", "attachment"], "$prose": ["text", 1]},
            ),
        ),
        ({"type": "attachment"}, ContextState(kind="", extra={"$keys": ["type"]})),
    ],
    ids=["text", "no-attachment"],
)
def test_an_attachment_line_reads_as_the_context_it_gave(
    record: dict[str, object],
    expected: ContextState,
) -> None:
    assert claude._read_attachment_record(record) == expected


def test_a_line_of_no_type_keeps_its_fields_under_no_kind() -> None:
    assert claude._read_by_type({"other": 1}, {}) == [
        UncategorizedRecord(kind="", payload={"other": 1}),
    ]


def test_a_call_s_later_messages_from_sagent_are_user_text_one_result_each() -> None:
    """Claude takes one result per call: a stub answers it, its news and result follow."""
    records: list[SessionRecord] = [
        SessionContext(model="m"),
        UserMessage(content="go"),
        ToolCall(call_id="c1", name="Bash", arguments={"command": "sleep 9"}),
        ToolMessage(call_id="c1", state="pending", shown="[detached]"),
        AssistantMessage(content="waiting"),
        ToolMessage(
            call_id="c1",
            state="pending",
            stdout=("[Bash c1 still running]\nhalf",),
        ),
        AssistantMessage(content="still"),
        ToolMessage(call_id="c1", state="done", stdout=("all",)),
        AssistantMessage(content="done"),
    ]
    output = StringIO()
    claude.denormalize(records, output)
    back = list(claude.normalize(StringIO(output.getvalue())))
    assert [r.call_id for r in back if isinstance(r, ToolMessage)] == ["c1"]
    assert [r.content for r in back if isinstance(r, UserMessage)] == [
        "go",
        "[Bash c1 still running]\nhalf",
        "[call c1 done]\nall",
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
