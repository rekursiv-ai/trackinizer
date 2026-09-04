"""Tests for Claude session streams."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

import base64
import json

import pytest

from trackinizer.lib.agent.sessions import claude
from trackinizer.lib.agent.sessions.claude import _group
from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AssistantMessage,
    Attachment,
    ContextClear,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    Splice,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    ToolResult,
    TranscriptItem,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResult,
    WebSearchResults,
)
from trackinizer.lib.custom_json import DictCodec, ListCodec


ENVELOPE = (
    '"userType":"external","cwd":"/workspace","sessionId":"s1","version":"2.1.241"'
)


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
            record, UserMessage | AssistantMessage | Thinking | ToolCall | ToolResult
        )
    )


def _last_act(records: Sequence[SessionRecord]) -> SessionRecord:
    """Return the last record that is not a restatement of the settings.

    Claude's escaping convention is a majority over the lines read, so the
    reader restates it as a trailing :class:`TurnContext` whenever it moves --
    the final record of a stream is that state, not the act a line carried.
    """
    return next(
        record for record in reversed(records) if not isinstance(record, TurnContext)
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
        type='"user"', message='{"role":"user","content":"Hi."}', uuid='"u1"'
    )

    records = list(claude.normalize(StringIO(native)))

    # Index 2: every session opens with the settings in force and the
    # ``ContextClear`` that states the context it begins from, and the turn's
    # own settings follow them.
    assert isinstance(records[1], ContextClear)
    context = records[2]
    assert isinstance(context, TurnContext)
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
    assert isinstance(context, TurnContext)
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

    result = _transcript(records)[0]
    assert isinstance(result, UncategorizedToolResult)
    assert result.call_id == "c1"
    assert result.content == "boom"


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

    result = _transcript(records)[0]
    assert isinstance(result, UncategorizedToolResult)
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
    native: str, expected: type[SessionRecord]
) -> None:
    # A line only claude writes -- its queue, its identity lines -- stays
    # uncategorized: a type for it would be one no other provider can fill.
    records = list(claude.normalize(StringIO(native + "\n")))

    assert isinstance(_last_act(records), expected)


@pytest.mark.parametrize(
    ("tool", "result", "expected"),
    [
        ("Bash", '{"stdout":"hi","stderr":"","interrupted":false}', ShellCommandResult),
        ("Read", '{"file":{"filePath":"/a"},"type":"text"}', FileReadResult),
        ("Write", '{"filePath":"/a","content":"body"}', FileWriteResult),
        ("Edit", '{"filePath":"/a","replaceAll":false}', FileEditResult),
        ("WebSearch", '{"query":"q","durationSeconds":1.5}', WebSearchResults),
        ("WebFetch", '{"url":"https://x","result":"body"}', WebFetchResult),
        ("Agent", '{"agentId":"a1","status":"done"}', AgentStatusResult),
        ("Skill", '{"commandName":"tdd","success":true}', UncategorizedToolResult),
    ],
    ids=["bash", "read", "write", "edit", "search", "fetch", "agent", "uncategorized"],
)
def test_a_result_is_typed_by_what_its_tool_did(
    tool: str, result: str, expected: type[SessionRecord]
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

    assert type(_last_act(records)) is expected


@pytest.mark.parametrize(
    ("command", "content", "result_payload", "expected"),
    [
        (
            "/bin/cat a.txt",
            '"body\\n"',
            '{"stdout":"body\\n","stderr":"","interrupted":false}',
            FileReadResult,
        ),
        (
            "/usr/bin/printf body > a.txt",
            '""',
            '{"stdout":"","stderr":"","interrupted":false}',
            FileWriteResult,
        ),
    ],
    ids=["read", "write"],
)
def test_a_successful_bash_file_operation_lifts_to_its_specific_type(
    command: str,
    content: str,
    result_payload: str,
    expected: type[SessionRecord],
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

    result = _last_act(records)
    assert type(result) is expected
    assert isinstance(result, FileReadResult | FileWriteResult | FileEditResult)
    assert result.path == "a.txt"
    if isinstance(result, FileReadResult):
        assert result.content == "body\n"
    if isinstance(result, FileWriteResult):
        assert result.content == "body"
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
    result = _last_act(records)

    assert isinstance(result, FileEditResult)
    assert result.path == "a.txt"
    # A regex program names no text it replaced, so it states no splice
    # rather than inventing a before and after.
    assert result.edits == ()
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
    result = _last_act(records)

    assert isinstance(result, FileEditResult)
    assert result.path == "/w/a.py"
    assert result.edits == (
        Splice(before="price - pct", after="price * (1 - pct / 100)"),
    )
    assert result.edits[0].start is None
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
    edit = _last_act(records)
    assert isinstance(edit, FileEditResult)

    output = StringIO()
    claude.denormalize(
        [
            replace(record, edits=(Splice(before="old", after="changed"),))
            if record is edit
            else record
            for record in records
        ],
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
    lifted = _last_act(records)
    assert isinstance(lifted, FileReadResult)

    output = StringIO()
    claude.denormalize(
        [
            replace(record, path="renamed.txt") if record is lifted else record
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
    lifted = _last_act(records)
    assert isinstance(lifted, FileReadResult)
    assert lifted.call_id == "c2"

    output = StringIO()
    claude.denormalize(
        [
            replace(record, path="renamed.txt") if record is lifted else record
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
    record = DictCodec.coerce(json.loads(native.splitlines()[0]))
    message = DictCodec.coerce(record.get("message"))
    return list(ListCodec.mappings(message.get("content")))


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

    assert isinstance(_last_act(records), ShellCommandResult)
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

    assert isinstance(result, UncategorizedToolResult)


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

    assert isinstance(result, ShellCommandResult)


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
            StringIO(first_call + first_result + second_call + second_result)
        )
        if isinstance(record, FileReadResult)
    ]

    assert [(result.path, result.content) for result in results] == [
        ("a.txt", "A\n"),
        ("b.txt", "B\n"),
    ]


def test_a_foreign_lifted_read_uses_its_semantic_content() -> None:
    records = (
        FileReadResult(
            call_id="c1",
            path="a.txt",
            content="body\n",
            extra={
                "$shell": {
                    "command": ["/bin/bash", "-lc", "/bin/cat a.txt"],
                    "exit_code": 0,
                }
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
    result = records[-1]
    assert isinstance(result, FileReadResult)
    changed = [*records[:-1], replace(result, path="new.txt")]
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


@pytest.mark.parametrize("payload", ["{}", "null"], ids=["empty", "null"])
def test_an_explicit_tool_payload_round_trips(payload: str) -> None:
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
    payload: str, expected_payload: str
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
    result = records[-1]
    assert isinstance(result, FileWriteResult)
    changed = [*records[:-1], replace(result, content="new")]
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

    typed_result = _last_act(records)
    assert isinstance(typed_result, WebSearchResults | WebFetchResult)
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
        TurnContext(context_id=0, extra={"sessionId": "s1"}),
        UncategorizedToolResult(context_id=0, call_id="c1", content="ok"),
    )
    out = StringIO()

    claude.denormalize(records, out)

    assert (
        out.getvalue() == '{"type":"user","message":{"role":"user","content":['
        '{"type":"tool_result","tool_use_id":"c1","content":"ok"}]},'
        '"sessionId":"s1"}\n'
    )


def test_a_synthesized_session_denormalizes_to_claude_records() -> None:
    records = (
        TurnContext(context_id=0, extra={"sessionId": "s1", "cwd": "/w"}),
        UserMessage(context_id=0, content="Hi.", extra={"uuid": "u1"}),
        Thinking(context_id=0, content="Reading.", encrypted="sig"),
        ToolCall(
            context_id=0,
            call_id="c1",
            name="Read",
            arguments={"path": "/a"},
            extra={"uuid": "a2"},
        ),
        UncategorizedToolResult(
            context_id=0,
            call_id="c1",
            content="ok",
            extra={"uuid": "u2", "$result": {"keys": ["type", "tool_use_id"]}},
        ),
    )
    out = StringIO()

    claude.denormalize(records, out)

    rebuilt = claude.normalize(StringIO(out.getvalue()))
    # The result comes back as what the tool DID, not as a bare result: the
    # ``Read`` call two records earlier is what names it.
    assert [type(r).__name__ for r in _transcript(rebuilt)] == [
        "UserMessage",
        "Thinking",
        "ToolCall",
        "FileReadResult",
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
    # ``TurnContext``, and those state no turn for a record to name.
    settings = [
        record for record in records if isinstance(record, TurnContext) and record.extra
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
    result = records[-1]
    assert isinstance(result, WebSearchResults)
    changed = [
        *records[:-1],
        replace(
            result,
            content=(
                *result.content,
                WebSearchResult(url="u3", title="t3", snippet="s3"),
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
    result = records[-1]
    assert isinstance(result, AgentStatusResult)
    changed = [*records[:-1], replace(result, content="X\nY\nZ")]
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
    result = _last_act(records)
    assert isinstance(result, UncategorizedToolResult)
    changed = [
        replace(record, content="X\nY\nZ") if record is result else record
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
        TurnContext(context_id=0, extra={"sessionId": "s1"}),
        UserMessage(
            context_id=0,
            content="hi",
            extra={"uuid": "u1", "id": "foreign", "$payload": {"x": 1}},
        ),
    )
    output = StringIO()

    claude.denormalize(records, output)

    assert output.getvalue() == (
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"text","text":"hi"}]},"uuid":"u1","sessionId":"s1"}\n'
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
    result = records[-1]
    assert isinstance(result, FileEditResult)
    changed = [
        *records[:-1],
        replace(result, edits=(Splice(before="a", after="c"),)),
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
    escaped = "caf\\u00e9"  # codespell:ignore caf
    native = _line(
        type='"user"', message=f'{{"role":"user","content":"{escaped}"}}'
    ) + _line(type='"user"', message='{"role":"user","content":"café"}')

    records = list(claude.normalize(StringIO(native)))

    # Encoding is RESTATED as the majority moves, so the spelling in force is
    # the one the LAST context to state it carries.
    encodings = [
        record.encoding
        for record in records
        if isinstance(record, TurnContext) and record.encoding
    ]
    assert isinstance(encodings[-1]["ascii_escape_exceptions"], str)
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

    assert [type(record).__name__ for record in records] == [
        "UserMessage",
        "UncategorizedToolResult",
        "UncategorizedToolResult",
    ]
    message = records[0]
    assert isinstance(message, UserMessage)
    assert message.content == "before\nafter"
    assert message.attachments == (Attachment(mime_descriptor="image/png", data=b"hi"),)
    assert [record.call_id for record in records if isinstance(record, ToolResult)] == [
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
