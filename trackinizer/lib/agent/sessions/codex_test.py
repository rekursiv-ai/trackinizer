"""Tests for Codex rollout streams."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from io import StringIO

import json

import pytest

from trackinizer.lib.agent.sessions import codex
from trackinizer.lib.agent.sessions.codex import _grouped
from trackinizer.lib.agent.sessions.udiff import parse_udiff, render_udiff
from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    Attachment,
    ContextClear,
    ContextCompaction,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
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
    WebSearchResult,
    WebSearchResults,
)
from trackinizer.lib.custom_json import DictCodec, MutableJSONValue, json_unfreeze


META = '{"type":"session_meta","payload":{"session_id":"s1","cwd":"/workspace"}}\n'
CONTEXT = (
    '{"type":"turn_context","payload":{"turn_id":"t1","cwd":"/workspace",'
    '"model":"gpt-5.6-sol","effort":"medium","summary":"auto",'
    '"approval_policy":"never"}}\n'
)


def _item(payload: str) -> str:
    """Return one response-item line carrying ``payload``."""
    return '{"type":"response_item","payload":' + payload + "}\n"


def _transcript(records: Iterable[SessionRecord]) -> tuple[TranscriptItem, ...]:
    """Return the conversation and tool records among ``records``."""
    return tuple(
        record
        for record in records
        if isinstance(
            record, UserMessage | AssistantMessage | Thinking | ToolCall | ToolResult
        )
    )


def test_a_session_declares_its_context_and_identity() -> None:
    records = list(codex.normalize(StringIO(META + CONTEXT)))

    # The launch line IS settings, so it opens the stream as a ``TurnContext``
    # rather than as metadata beside it, and the ``ContextClear`` it opens
    # follows. The per-turn settings then supersede it.
    launch = records[0]
    assert isinstance(launch, TurnContext)
    declaration = DictCodec.coerce(launch.extra.get("payload"))
    assert declaration["cwd"] == "/workspace"
    assert declaration["session_id"] == "s1"
    assert isinstance(records[1], ContextClear)
    context = records[2]
    assert isinstance(context, TurnContext)
    assert context.model == "gpt-5.6-sol"
    assert context.effort == "medium"
    assert context.summary_kind == "auto"
    assert context.permission == "never"


def test_context_replay_preserves_null_and_malformed_semantic_fields() -> None:
    native = META + (
        '{"type":"turn_context","payload":{"approval_policy":7,'
        '"model":null,"effort":"future"}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    output = StringIO()

    codex.denormalize(records, output)

    assert output.getvalue() == native


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"turn_context","payload":{"$provider":{"x":1}}}\n',
        (
            '{"type":"event_msg","payload":{"type":"token_count",'
            '"info":{},"rate_limits":{},"$provider":{"x":1}}}\n'
        ),
    ],
    ids=["turn-context", "token-count"],
)
def test_provider_dollar_keys_round_trip(line: str) -> None:
    native = META + line
    output = StringIO()

    codex.denormalize(codex.normalize(StringIO(native)), output)

    assert output.getvalue() == native


def test_order_table_does_not_create_a_stamp_module_global() -> None:
    assert "_STAMP" not in vars(codex)


def test_template_precedence_does_not_consume_legacy_blocks() -> None:
    legacy: list[MutableJSONValue] = [{"type": "future", "value": 1}]
    extra: dict[str, MutableJSONValue] = {
        "$templates": [{"type": "input_text"}],
        "$blocks": legacy,
        "$order": ["text"],
    }

    blocks = codex._write_content("hello", (), extra, text_key="input_text")

    assert blocks == [{"type": "input_text", "text": "hello"}]
    assert extra["$blocks"] == legacy


def test_a_record_names_the_context_that_applied() -> None:
    # Codex writes a turn context per turn, so a record points at the one in
    # force rather than copying its settings.
    native = (
        META
        + CONTEXT
        + _item(
            '{"type":"message","id":"u1","role":"user",'
            '"content":[{"type":"input_text","text":"Hi."}]}'
        )
    )

    records = list(codex.normalize(StringIO(native)))

    message = _transcript(records)[0]
    # Index 2: the launch settings open the stream and the ``ContextClear``
    # they open follows, so the turn's own context sits third.
    assert message.context_id == 2
    assert isinstance(records[2], TurnContext)


def test_reasoning_normalizes_to_a_summary_beside_its_sealed_half() -> None:
    # The readable half is a gloss of reasoning that stays encrypted, so it
    # is not the reasoning itself and does not go in ``text``.
    native = META + _item(
        '{"type":"reasoning","id":"r1",'
        '"summary":[{"type":"summary_text","text":"Inspecting."}],'
        '"encrypted_content":"sealed"}'
    )

    records = list(codex.normalize(StringIO(native)))

    thinking = _transcript(records)[0]
    assert isinstance(thinking, Thinking)
    assert thinking.summary == "Inspecting."
    assert thinking.content is None
    assert thinking.encrypted == "sealed"


def test_a_function_call_decodes_its_nested_arguments() -> None:
    native = META + _item(
        '{"type":"function_call","call_id":"c1","name":"Read",'
        '"arguments":"{\\"path\\":\\"/a\\"}"}'
    )

    records = list(codex.normalize(StringIO(native)))

    call = _transcript(records)[0]
    assert isinstance(call, ToolCall)
    assert call.name == "Read"
    assert call.arguments == {"path": "/a"}


def test_a_malformed_argument_string_does_not_abort_the_file() -> None:
    # ``arguments`` is JSON nested in JSON, so a truncated write leaves a
    # valid record carrying an invalid string. It is named unmapped rather
    # than read as an empty argument list, which would claim the call was made
    # with input it never had.
    native = META + _item(
        '{"type":"function_call","call_id":"c1","name":"Read","arguments":"{"}'
    )

    records = list(codex.normalize(StringIO(native)))

    unmapped = records[-1]
    assert isinstance(unmapped, UncategorizedRecord)
    assert unmapped.kind == "response_item/function_call"


def test_a_custom_tool_call_keeps_its_free_form_input() -> None:
    native = META + _item(
        '{"type":"custom_tool_call","id":"i1","status":"completed",'
        '"call_id":"c1","name":"shell","input":"ls"}'
    )

    records = list(codex.normalize(StringIO(native)))

    call = _transcript(records)[0]
    assert isinstance(call, ToolCall)
    assert call.arguments == {"input": "ls"}
    assert call.extra["status"] == "completed"


def test_an_image_in_a_tool_result_decodes_to_its_bytes() -> None:
    native = META + _item(
        '{"type":"function_call_output","call_id":"c1","output":'
        '[{"type":"input_image","image_url":"data:image/png;base64,aGk="}]}'
    )

    records = list(codex.normalize(StringIO(native)))

    result = _transcript(records)[0]
    assert isinstance(result, UncategorizedToolResult)
    assert result.attachments == (Attachment(mime_descriptor="image/png", data=b"hi"),)


def test_a_developer_message_is_not_a_model_reply() -> None:
    # ``developer`` and ``system`` are provider-injected priming; calling
    # them assistant turns would attribute them to the model.
    native = META + _item(
        '{"type":"message","id":"d1","role":"developer",'
        '"content":[{"type":"input_text","text":"Be brief."}]}'
    )

    records = list(codex.normalize(StringIO(native)))

    message = records[-1]
    assert isinstance(message, SystemMessage)
    assert message.subtype == "developer"
    assert message.content == "Be brief."


def test_a_foreign_subtype_is_not_written_as_a_codex_role() -> None:
    """A role codex cannot send is a 400 from the API, not a stored oddity.

    Claude closes each turn with ``system`` / ``turn_duration``, which crosses
    into the IR as a :class:`SystemMessage` naming that subtype. Written
    through as the role, the resumed session failed on its FIRST request:
    ``[invalid_enum_value] Invalid value: 'turn_duration'. Supported values
    are: 'assistant', 'system', 'developer', and 'user'.`` Measured across
    1449 captured rollouts, codex writes only ``assistant``, ``user``, and
    ``developer`` -- so a subtype outside that set is another CLI's word and
    must fall back rather than be echoed.

    The subtype is not discarded: it rides in the residual, so a rollout that
    came from codex still round-trips to the role it was written with.
    """
    out = StringIO()

    codex.denormalize([SystemMessage(content="", subtype="turn_duration")], out)

    payload = DictCodec.coerce(json.loads(out.getvalue().splitlines()[0])["payload"])
    assert payload["role"] == "system"


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        (
            (
                '{"type":"event_msg","payload":{"type":"token_count",'
                '"info":{"total":7},"rate_limits":{}}}'
            ),
            TokenUsage,
        ),
        (
            '{"type":"event_msg","payload":{"type":"task_started","turn_id":"t1"}}',
            UncategorizedRecord,
        ),
        ('{"type":"world_state","payload":{"full":true}}', ContextState),
        ('{"type":"compacted","payload":{"message":"summary"}}', ContextCompaction),
        (
            (
                '{"type":"event_msg","payload":{"type":"item_completed","item":'
                '{"type":"CommandExecution","id":"e1","command":["ls"]}}}'
            ),
            ShellCommandResult,
        ),
        (
            (
                '{"type":"event_msg","payload":{"type":"item_completed","item":'
                '{"type":"FileChange","id":"e2","changes":{}}}}'
            ),
            FileEditResult,
        ),
        (
            (
                '{"type":"event_msg","payload":{"type":"item_completed","item":'
                '{"type":"AgentMessage","content":[{"type":"Text","text":"hi"}]}}}'
            ),
            UncategorizedRecord,
        ),
    ],
    ids=["usage", "task", "world", "compaction", "command", "patch", "echo"],
)
def test_an_event_maps_to_its_own_record_type(
    native: str, expected: type[SessionRecord]
) -> None:
    records = list(codex.normalize(StringIO(META + native + "\n")))

    # Index 2: the launch line's settings and the clear they open lead every
    # stream, so the event's own record is the one after them. Not ``[-1]`` --
    # a compaction is FOLLOWED by the window it opened, and that trailing
    # ``ContextClear`` is a derived record, not what the event mapped to.
    assert isinstance(records[2], expected)


def test_a_peer_message_is_a_message_not_a_tool_result() -> None:
    # Nothing is waiting on it the way a call waits on a result, so it is
    # shaped like the other messages: prose plus a named pair of ends.
    native = META + _item(
        '{"type":"agent_message","id":"m1","author":"/root",'
        '"recipient":"/root/reviewer",'
        '"content":[{"type":"input_text","text":"Take a look."}]}'
    )

    records = list(codex.normalize(StringIO(native)))

    message = records[-1]
    assert isinstance(message, AgentToAgentMessage)
    assert message.content == "Take a look."
    assert message.sender == "/root"
    assert message.recipient == "/root/reviewer"


def test_a_completed_command_keeps_its_argument_list() -> None:
    # The provider writes a command as its argv; joining it loses the split.
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"CommandExecution","id":"e1","command":["/bin/sh","-c","ls"],'
        '"exit_code":0,"stdout":"a\\n"}}}\n'
    )

    records = list(codex.normalize(StringIO(native)))

    result = records[-1]
    assert isinstance(result, ShellCommandResult)
    assert result.command == ("/bin/sh", "-c", "ls")
    assert result.exit_code == 0
    assert result.stdout == "a\n"


@pytest.mark.parametrize(
    ("command", "stdout", "expected"),
    [
        ('["/bin/bash","-lc","/bin/cat a.txt"]', '"body\\n"', FileReadResult),
        (
            '["/bin/bash","-lc","/usr/bin/printf body > a.txt"]',
            '""',
            FileWriteResult,
        ),
    ],
    ids=["read", "write"],
)
def test_a_successful_command_file_operation_lifts_to_its_specific_type(
    command: str, stdout: str, expected: type[SessionRecord]
) -> None:
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"CommandExecution","id":"e1","command":'
        + command
        + ',"exit_code":0,"stdout":'
        + stdout
        + "}}}\n"
    )

    records = list(codex.normalize(StringIO(native)))

    result = records[-1]
    assert type(result) is expected
    assert isinstance(result, FileReadResult | FileWriteResult | FileEditResult)
    assert result.path == "a.txt"
    if isinstance(result, FileReadResult):
        assert result.content == "body\n"
    if isinstance(result, FileWriteResult):
        assert result.content == "body"
    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_a_completed_in_place_edit_names_its_file_without_a_diff() -> None:
    """``sed -i`` rewrites the file and prints nothing.

    The record names the edited file and states no splice: the new bytes were
    never printed, so an edit here would be invented.
    """
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"CommandExecution","id":"e1","command":'
        '["/bin/bash","-lc","/bin/sed -i s/old/new/ a.txt"],'
        '"exit_code":0,"stdout":""}}}\n'
    )

    records = list(codex.normalize(StringIO(native)))
    result = records[-1]

    assert isinstance(result, FileEditResult)
    assert result.path == "a.txt"
    assert result.edits == ()
    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_an_unparsable_line_survives_verbatim() -> None:
    native = '{"type":"response_item","payload":{"type":"messa\n'

    records = list(codex.normalize(StringIO(native)))

    assert records[-1] == IncompleteRecord(text=native)


@pytest.mark.parametrize(
    "native",
    [
        META,
        META + '{"type":"session_meta","payload":{"session_id":"s2"}}\n',
        META
        + _item(
            '{"type":"message","role":"user",'
            '"content":[{"type":"input_text","text":"Hi."}]}'
        ),
        META + _item('{"type":"reasoning","summary":[],"encrypted_content":""}'),
        META
        + _item(
            '{"type":"message","role":"user","content":['
            '{"type":"input_image","image_url":"data:image/png;base64,!"}]}'
        ),
        META + _item('{"type":"message","role":"user","content":[{}]}'),
    ],
    ids=[
        "timestamp-less launch line",
        "timestamp-less repeated launch line",
        "timestamp-less record",
        "empty encrypted content",
        "malformed base64",
        "empty content block",
    ],
)
def test_native_bytes_round_trip(native: str) -> None:
    records = list(codex.normalize(StringIO(native)))
    out = StringIO()

    codex.denormalize(records, out)

    assert out.getvalue() == native


def test_content_without_native_order_is_not_dropped() -> None:
    records = (
        SystemMessage(content="instructions", subtype="developer"),
        UserMessage(
            content="look",
            attachments=(Attachment(mime_descriptor="image/png", data=b"hi"),),
        ),
    )
    out = StringIO()

    codex.denormalize(records, out)

    assert '"content":[{"type":"input_text","text":"instructions"}]' in out.getvalue()
    assert (
        '"content":[{"type":"input_text","text":"look"},'
        '{"type":"input_image","image_url":"data:image/png;base64,aGk="}]'
        in out.getvalue()
    )


def test_a_synthesized_session_denormalizes_to_codex_records() -> None:
    records = (
        TurnContext(context_id=0, extra={"turn_id": "t1"}),
        UserMessage(context_id=0, content="Hi."),
        AssistantMessage(context_id=0, content="Here."),
        ToolCall(context_id=0, call_id="c1", name="Read", arguments={"p": "/a"}),
        UncategorizedToolResult(context_id=0, call_id="c1", content="ok"),
    )
    out = StringIO()

    codex.denormalize(records, out)

    rebuilt = codex.normalize(StringIO(out.getvalue()))
    assert [type(r).__name__ for r in _transcript(rebuilt)] == [
        "UserMessage",
        "AssistantMessage",
        "ToolCall",
        "UncategorizedToolResult",
    ]


@pytest.mark.parametrize(
    ("role", "expected"),
    [("assistant", AssistantMessage), ("developer", SystemMessage)],
)
def test_non_user_message_images_survive_normalization(
    role: str, expected: type[SessionRecord]
) -> None:
    native = META + _item(
        f'{{"type":"message","role":"{role}","content":['
        '{"type":"input_image","image_url":"data:image/png;base64,aGk="}]}'
    )

    message = list(codex.normalize(StringIO(native)))[-1]

    assert isinstance(message, expected)
    assert isinstance(message, AssistantMessage | SystemMessage)
    assert message.attachments == (Attachment(mime_descriptor="image/png", data=b"hi"),)


def test_new_message_attachments_are_appended() -> None:
    native = META + _item(
        '{"type":"message","role":"user","content":['
        '{"type":"input_image","image_url":"data:image/png;base64,b2xk"}]}'
    )
    records = list(codex.normalize(StringIO(native)))
    message = records[-1]
    assert isinstance(message, UserMessage)
    edited = replace(
        message,
        attachments=(
            *message.attachments,
            Attachment(mime_descriptor="image/png", data=b"new"),
        ),
    )
    output = StringIO()

    codex.denormalize([edited], output)

    assert "data:image/png;base64,bmV3" in output.getvalue()


def test_new_search_rows_are_appended() -> None:
    native = META + (
        '{"type":"event_msg","payload":{"type":"web_search_end",'
        '"call_id":"c","results":[{"url":"https://old"}]}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    result = records[-1]
    assert isinstance(result, WebSearchResults)
    edited = replace(
        result,
        content=(*result.content, WebSearchResult(url="https://new")),
    )
    output = StringIO()

    codex.denormalize([edited], output)

    assert "https://new" in output.getvalue()


def test_content_edits_do_not_fabricate_empty_blocks() -> None:
    native = META + _item(
        '{"type":"message","role":"user","content":['
        '{"type":"input_text","text":"remove"},{"type":"future","x":1}]}'
    )
    records = list(codex.normalize(StringIO(native)))
    message = records[-1]
    assert isinstance(message, UserMessage)
    output = StringIO()

    codex.denormalize([replace(message, content=None, extra={})], output)

    assert '"content":[]' in output.getvalue()
    assert '"text":""' not in output.getvalue()
    assert "{}" not in output.getvalue()


def test_foreign_provider_residuals_do_not_enter_codex_wire() -> None:
    output = StringIO()

    codex.denormalize([UserMessage(content="hi", extra={"$claude": {"x": 1}})], output)

    assert "$claude" not in output.getvalue()


def test_a_read_crosses_as_the_command_codex_reads_with() -> None:
    """A read reaches codex's wire, in codex's own vocabulary.

    Codex has no read TOOL -- across 400 captured rollouts it names only
    ``exec_command``, ``apply_patch``, and ``write_stdin`` -- so it reads by
    running one. Emitting nothing dropped the record entirely, which is what
    made claude->codex lose 22 reads over a 60-file sample; emitting a
    ``WebSearch`` would name a different act (axiom 9).
    """
    output = StringIO()

    codex.denormalize([FileReadResult(call_id="c", path="/a", content="x")], output)

    written = output.getvalue()
    assert '"type":"CommandExecution"' in written
    assert '"stdout":"x"' in written
    assert "WebSearch" not in written


def test_file_edit_diff_is_emitted_when_path_is_known() -> None:
    output = StringIO()

    codex.denormalize(
        [
            FileEditResult(
                call_id="c",
                path="a.py",
                edits=parse_udiff("@@ -1 +1 @@\n-old\n+new\n"),
            )
        ],
        output,
    )

    assert '"changes":{"a.py":{"unified_diff":"@@ -1 +1 @@\\n-old\\n+new\\n"}}' in (
        output.getvalue()
    )


@pytest.mark.parametrize("payload", ["null", "7", "[]"])
def test_non_object_payloads_and_outer_fields_round_trip(payload: str) -> None:
    native = META + (
        '{"trace":{"id":1},"type":"response_item","payload":' + payload + "}\n"
    )
    output = StringIO()

    codex.denormalize(codex.normalize(StringIO(native)), output)

    assert output.getvalue() == native


def test_arbitrary_outer_fields_round_trip_with_object_payloads() -> None:
    native = META + (
        '{"trace":{"id":1},"type":"response_item",'
        '"payload":{"type":"future"},"tail":null}\n'
    )
    output = StringIO()

    codex.denormalize(codex.normalize(StringIO(native)), output)

    assert output.getvalue() == native


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"message","role":null,"content":[]}',
        '{"type":"message","role":7,"content":[]}',
        '{"type":"web_search_call","id":null,"action":7}',
        ('{"type":"item_completed","item":{"type":"WebSearch","id":null,"query":7}}'),
        (
            '{"type":"item_completed","item":{"type":"FileChange",'
            '"id":null,"changes":{}}}'
        ),
    ],
)
def test_malformed_semantic_fields_round_trip(payload: str) -> None:
    outer = "event_msg" if "item_completed" in payload else "response_item"
    native = META + f'{{"type":"{outer}","payload":{payload}}}\n'
    output = StringIO()

    codex.denormalize(codex.normalize(StringIO(native)), output)

    assert output.getvalue() == native


def test_search_results_preserve_scalar_and_empty_object_members() -> None:
    native = META + (
        '{"type":"event_msg","payload":{"type":"web_search_end",'
        '"call_id":"c","results":[7,{},'
        '{"url":"https://example.com","rank":1}]}}\n'
    )
    output = StringIO()

    codex.denormalize(codex.normalize(StringIO(native)), output)

    assert output.getvalue() == native


def test_every_ordinal_state_round_trips() -> None:
    native = (
        '{"ordinal":4,"type":"session_meta","payload":{"session_id":"s"}}\n'
        '{"ordinal":9,"type":"world_state","payload":{}}\n'
        '{"type":"world_state","payload":{}}\n'
        '{"ordinal":"bad","type":"world_state","payload":{}}\n'
        '{"ordinal":null,"type":"world_state","payload":{}}\n'
    )
    output = StringIO()

    codex.denormalize(codex.normalize(StringIO(native)), output)

    assert output.getvalue() == native


def test_inserting_a_record_does_not_move_timestamp_replay_state() -> None:
    malformed = (
        '{"timestamp":7,"type":"response_item","payload":{"type":"message",'
        '"role":"user","content":[{"type":"input_text","text":"first"}]}}\n'
    )
    absent = _item(
        '{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"second"}]}'
    )
    records = list(codex.normalize(StringIO(META + malformed + absent)))
    inserted = UserMessage(content="inserted")
    output = StringIO()

    codex.denormalize([inserted, *records], output)

    assert (
        output.getvalue()
        == META
        + _item(
            '{"type":"message","role":"user",'
            '"content":[{"type":"input_text","text":"inserted"}]}'
        )
        + malformed
        + absent
    )


def test_deleting_a_record_does_not_move_timestamp_replay_state() -> None:
    malformed = (
        '{"timestamp":7,"type":"response_item","payload":{"type":"message",'
        '"role":"user","content":[{"type":"input_text","text":"remove"}]}}\n'
    )
    absent = _item(
        '{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"keep"}]}'
    )
    records = list(codex.normalize(StringIO(META + malformed + absent)))
    output = StringIO()

    # Drops the malformed record, keeping the launch settings and the opening
    # ``ContextClear`` -- both are derived and write no line of their own.
    codex.denormalize([*records[:2], *records[3:]], output)

    assert output.getvalue() == META + absent


def test_a_line_timestamp_is_stored_once() -> None:
    # The outer line's ``timestamp`` IS the record's :attr:`timestamp`, and the
    # writer reads it from the field. Keeping the outer copy too stored every
    # stamp twice -- 1241 of 1470 records on one captured rollout, 2% of its
    # whole normalized size, for a value already on the record.
    native = META + (
        '{"timestamp":"2026-07-13T00:34:02.346Z","type":"response_item",'
        '"payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, UserMessage)

    assert record.timestamp == "2026-07-13T00:34:02.346Z"
    assert "$codex_line" not in record.extra, "the stamp is on the record already"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_a_patch_diff_is_stored_once() -> None:
    # :attr:`FileEditResult.edits` IS the diff, and the writer rebuilds the
    # per-path entry by rendering them. Keeping the source copy too stored
    # every diff twice -- 129 KB on one captured rollout, 4.7% of its size.
    diff = "@@ -1,2 +1,2 @@\n-old\n+new\n"
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"FileChange","id":"c1","changes":{"/w/a.py":'
        '{"type":"update","unified_diff":' + json.dumps(diff) + "}}}}}\n"
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, FileEditResult)

    assert render_udiff(record.edits) == diff
    assert diff not in json.dumps(json_unfreeze(record.extra)), (
        "the diff is on the record already"
    )

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_the_launch_payload_is_stored_once() -> None:
    """The launch line's payload is the opening settings, so ``$outer`` keeps none.

    ``_with_line_state`` already states the rule for every other line -- "the
    payload is the record ... keeping a copy stored every line's body twice" --
    and the launch line was the one path that skipped it. Worst case, too:
    ``session_meta`` carries ``base_instructions``, the whole system prompt, so
    the checked-in golden held ~19 KB of it twice.
    """
    native = (
        '{"timestamp":"2026-08-24T19:34:39.215Z","type":"session_meta",'
        '"payload":{"session_id":"01a03544-88de-71e2-981c-c8433de27ddc",'
        '"id":"01a03544-88de-71e2-981c-c8433de27ddc",'
        '"base_instructions":"XYZZY"}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    launch = records[0]
    assert isinstance(launch, TurnContext)
    extra = json_unfreeze(launch.extra)

    assert "payload" not in DictCodec.coerce(extra.get("$outer"))
    # The stamp the opening context's own field already carries.
    assert "$launch_timestamp_raw" not in extra

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_a_canonical_outer_line_is_not_stored_per_record() -> None:
    # The outer line is ``{timestamp, type, payload}`` on all but a handful of
    # lines, and every part of it is already known: the stamp is the record's
    # field, the kind is what the writer emits, and the payload sits third.
    # Measured: 1268 records stored one of FIVE values, 62 KB.
    native = META + (
        '{"timestamp":"2026-07-13T00:00:00.000Z","type":"response_item",'
        '"payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, UserMessage)

    assert "$codex_line" not in record.extra, "the writer rebuilds this"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_an_unusual_outer_line_keeps_its_own_state() -> None:
    # A line whose outer keys the writer cannot rebuild -- here the payload
    # written FIRST, before the type -- keeps its own copy.
    native = META + (
        '{"payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]},'
        '"type":"response_item","timestamp":"2026-07-13T00:00:00.000Z"}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    output = StringIO()

    codex.denormalize(records, output)

    assert output.getvalue() == native


def test_a_token_count_stores_no_envelope_the_table_holds() -> None:
    # ``_ORDER["token_count"]`` already names this payload's key order, and
    # ``$nulls`` already names which field codex wrote as null. The replay
    # envelope repeats both: measured ONE distinct envelope written 490 times
    # on a single captured rollout -- 93 KB, 3.6% of that file's whole IR.
    native = META + (
        '{"type":"event_msg","payload":{"type":"token_count",'
        '"info":{"total_tokens":7},"rate_limits":null}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, TokenUsage)

    assert "$__custom_json_fields__" not in record.extra, "the table already holds it"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_a_legacy_patch_diff_is_stored_once() -> None:
    # The pre-0.149 ``patch_apply_end`` spelling put the whole ``changes`` map
    # in the residual, so each entry's ``unified_diff`` sat beside the
    # :attr:`edits` built from it: 95% of one captured rollout's 91 KB of diff
    # text was stored twice, which is half that file's entire IR growth.
    diff = "@@ -1,2 +1,2 @@\n-old\n+new\n"
    native = META + (
        '{"type":"event_msg","payload":{"type":"patch_apply_end",'
        '"call_id":"c1","stdout":"","stderr":"","success":true,'
        '"changes":{"/w/a.py":{"type":"update","unified_diff":'
        + json.dumps(diff)
        + ',"move_path":null}}}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, FileEditResult)

    assert render_udiff(record.edits) == diff
    # Compared VALUE to value. A substring search over serialized JSON cannot
    # answer this: the needle holds a real newline and the haystack holds the
    # escaped ``\\n``, so the search misses a diff that is plainly there --
    # which is how this assertion passed against a 100%-duplicated corpus.
    stored = DictCodec.coerce(json_unfreeze(record.extra).get("changes"))
    assert [
        DictCodec.coerce(entry).get("unified_diff") for entry in stored.values()
    ] == [None], "the diff is on the record already"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_an_added_file_is_a_write_not_an_edit() -> None:
    """``*** Add File`` states a file's WHOLE bytes, which is a write.

    :class:`FileWriteResult` is the record for exactly that, and codex's own
    writer maps one back to this shape -- so typing it as an edit made the IR
    unable to round-trip a write, and put an edit-shaped act on claude's wire
    where a write belonged.
    """
    native = META + (
        '{"type":"event_msg","payload":{"type":"patch_apply_end",'
        '"call_id":"c1","changes":{"/w/new.py":'
        '{"type":"add","content":"fresh text"}}}}\n'
    )
    records = list(codex.normalize(StringIO(native)))

    record = records[-1]
    assert isinstance(record, FileWriteResult)
    assert record.path == "/w/new.py"
    assert record.content == "fresh text"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_a_multi_path_patch_names_every_file_it_changed() -> None:
    """One event, several files: one record each, per axiom 10.

    ``changes`` is keyed by path, and folding every path's splices into ONE
    record left it naming no file at all -- 44 of 451 captured events touch
    more than one. The line's residual rides on the FIRST record, which is the
    same rule ``_read_user`` follows when one line carries several acts.
    """
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"FileChange","id":"c1","changes":'
        '{"/w/a.py":{"type":"update","unified_diff":"@@ -1 +1 @@\\n-a\\n+A\\n"},'
        '"/w/b.py":{"type":"update","unified_diff":"@@ -5 +5 @@\\n-b\\n+B\\n"}'
        "}}}}\n"
    )
    records = list(codex.normalize(StringIO(native)))

    edits = [r for r in records if isinstance(r, FileEditResult)]
    assert [(r.path, len(r.edits)) for r in edits] == [("/w/a.py", 1), ("/w/b.py", 1)]

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_an_entry_carrying_both_a_diff_and_content_round_trips() -> None:
    r"""The reader records WHICH key its field fills, so the writer agrees.

    Guessing by key presence picked different keys on the two sides: the
    reader emptied ``unified_diff`` and the writer rebuilt ``content``, so the
    diff came back ``null`` and the content was overwritten with the splice's
    rendering -- ``"whole"`` became ``"A\n"``.
    """
    native = META + (
        '{"type":"event_msg","payload":{"type":"patch_apply_end","call_id":"c1",'
        '"changes":{"/w/a.py":{"type":"add",'
        '"unified_diff":"@@ -1 +1 @@\\n-a\\n+A\\n","content":"whole"}}}}\n'
    )
    records = list(codex.normalize(StringIO(native)))

    output = StringIO()
    codex.denormalize(records, output)

    assert output.getvalue() == native


def test_a_mixed_patch_harvests_the_add_beside_the_update() -> None:
    """One event may both add a file and edit another; neither may be lost.

    Each is its own record, named for its own path -- 9 of 451 captured events
    mix the two forms, and one record could name only one of the files.
    """
    diff = "@@ -1,2 +1,2 @@\n-old\n+new\n"
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"FileChange","id":"c1","changes":{'
        '"/w/a.py":{"type":"update","unified_diff":' + json.dumps(diff) + "},"
        '"/w/new.py":{"type":"add","content":"fresh\\n"}}}}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    acts = [r for r in records if isinstance(r, FileEditResult | FileWriteResult)]

    # The update is an edit, the add is a write: one act each, named for its
    # own file and typed by what it did.
    assert [(type(r).__name__, r.path) for r in acts] == [
        ("FileEditResult", "/w/a.py"),
        ("FileWriteResult", "/w/new.py"),
    ]
    added = acts[-1]
    assert isinstance(added, FileWriteResult)
    assert added.content == "fresh\n"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_an_edited_patch_diff_still_replays_its_entry() -> None:
    # The stencil is not a cache: a diff edited on the record must reach the
    # wire, and the per-path structure around it must survive.
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"FileChange","id":"c1","changes":{"/w/a.py":'
        '{"type":"update","unified_diff":"@@ -1 +1 @@"}}}}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, FileEditResult)
    output = StringIO()

    codex.denormalize(
        [*records[:-1], replace(record, edits=parse_udiff("-EDIT\n"))],
        output,
    )

    assert '"unified_diff":"-EDIT\\n"' in output.getvalue()
    assert '"type":"update"' in output.getvalue()


def test_a_multi_path_patch_restores_each_diff_to_its_own_entry() -> None:
    # The field joins every path's diff with a newline, so splitting it back
    # has to land each piece under the path it came from.
    native = META + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"FileChange","id":"c1","changes":{'
        '"/w/a.py":{"unified_diff":"@@ -1 +1 @@\\n-a\\n+A"},'
        '"/w/b.py":{"unified_diff":"@@ -2 +2 @@\\n-b\\n+B"}}}}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    output = StringIO()

    codex.denormalize(records, output)

    assert output.getvalue() == native


def test_a_canonical_key_order_is_not_stored_per_record() -> None:
    # ``_ORDER`` already names the order codex writes each payload type in, and
    # the writer falls back to it. Storing the same list on every record cost
    # 40 KB on one captured rollout -- 425 copies of 13 distinct orders.
    native = META + _item(
        '{"type":"reasoning","id":"r1","summary":[],"encrypted_content":"s"}'
    )
    records = list(codex.normalize(StringIO(native)))
    record = records[-1]
    assert isinstance(record, Thinking)

    assert "$native_order" not in record.extra, "the table already holds it"

    output = StringIO()
    codex.denormalize(records, output)
    assert output.getvalue() == native


def test_a_key_order_the_table_misses_is_still_stored() -> None:
    # The table is not a guess: a line whose keys the table cannot reproduce --
    # here ``id`` written AFTER the summary -- keeps its own order.
    native = META + _item(
        '{"type":"reasoning","summary":[],"id":"r1","encrypted_content":"s"}'
    )
    records = list(codex.normalize(StringIO(native)))
    output = StringIO()

    codex.denormalize(records, output)

    assert output.getvalue() == native


def test_a_malformed_line_timestamp_is_still_replayed() -> None:
    # Only a stamp the FIELD can hold is dropped from the outer copy: a
    # non-string one has no field to live on, so the outer stencil is the only
    # place it survives.
    native = META + (
        '{"timestamp":7,"type":"response_item","payload":{"type":"message",'
        '"role":"user","content":[{"type":"input_text","text":"hi"}]}}\n'
    )
    records = list(codex.normalize(StringIO(native)))
    output = StringIO()

    codex.denormalize(records, output)

    assert output.getvalue() == native


def test_reasoning_summary_preserves_every_member_and_block_metadata() -> None:
    native = META + _item(
        '{"type":"reasoning","summary":['
        '{"type":"summary_text","text":"Inspecting.","metadata":{"x":1}},'
        '7,{},{"type":"future","x":1}],"encrypted_content":"sealed"}'
    )
    output = StringIO()

    records = list(codex.normalize(StringIO(native)))
    thinking = records[-1]
    assert isinstance(thinking, Thinking)
    assert thinking.encrypted == "sealed"
    codex.denormalize(records, output)

    assert output.getvalue() == native


_COMPACTED = (
    '{"type":"compacted","payload":{"message":"","replacement_history":['
    '{"type":"message","role":"user","content":[{"type":"input_text",'
    '"text":"keep me"}]},'
    '{"type":"message","role":"developer","content":[{"type":"input_text",'
    '"text":"rules"}]},'
    '{"type":"compaction","encrypted_content":"sealed"}]}}\n'
)


def test_a_session_opens_with_a_clear_stating_its_instructions() -> None:
    """A session BEGINS with the context sent before any assistant response.

    Codex states the system prompt on its launch payload, claude as its own
    lines, so the same fact was stated one way for one provider and another
    way for the other -- and a consumer asking "what did the model start from"
    had to know which file it was reading. The clear is where the IR says it,
    which is the sibling of what a compaction already says.
    """
    native = (
        '{"type":"session_meta","payload":{"session_id":"s1","id":"s1",'
        '"base_instructions":"XYZZY"}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}}\n'
    )

    records = list(codex.normalize(StringIO(native)))

    opening = records[1]
    assert isinstance(opening, ContextClear)
    assert opening.system_prompt == "XYZZY"


def test_an_opening_clear_costs_no_bytes() -> None:
    """The clear is derived from the launch line, so it writes none of its own."""
    native = (
        '{"type":"session_meta","payload":{"session_id":"s1","id":"s1",'
        '"base_instructions":"XYZZY"}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}}\n'
    )

    out = StringIO()
    codex.denormalize(codex.normalize(StringIO(native)), out)

    assert out.getvalue() == native


def test_a_compaction_carries_the_context_that_replaced_the_history() -> None:
    """``history`` is the POST-compaction context, and the field exists for it.

    Codex states it as ``replacement_history``: the turns the CLI kept, plus a
    trailing sealed ``compaction`` marker. Measured over 1355 rollouts, all 8
    compacted lines carry one (11-40 entries) -- and the reader left every one
    in the residual, so 343 KB of structured records were invisible to a
    consumer while every byte check passed. The same shape as the
    ``apply_patch`` adds and the ``detail`` images.
    """
    records = list(codex.normalize(StringIO(_COMPACTED)))

    # The event, then the window it opened: the history rides the clear, which
    # is the one record every context window opens with.
    assert isinstance(records[-2], ContextCompaction)
    window = records[-1]
    assert isinstance(window, ContextClear)
    assert [type(item).__name__ for item in window.history] == [
        "UserMessage",
        "SystemMessage",
    ]
    kept = window.history[0]
    assert isinstance(kept, UserMessage)
    assert kept.content == "keep me"


def test_a_compaction_states_the_summary_the_next_turn_reads() -> None:
    """The summary is what the model sees after compacting, so it is a field.

    Codex seals it in the trailing ``compaction`` entry rather than in
    ``message`` -- measured: 0 of 8 captured lines populate ``message``, and
    all 8 carry ``encrypted_content``. Reading only ``message`` therefore left
    ``summary`` unset on every real compaction in the corpus.
    """
    records = list(codex.normalize(StringIO(_COMPACTED)))

    window = records[-1]
    assert isinstance(window, ContextClear)
    assert window.summary == "sealed"


def test_a_compaction_rewrites_to_the_bytes_it_was_read_from() -> None:
    """Harvesting must not cost byte-exactness."""
    out = StringIO()

    codex.denormalize(codex.normalize(StringIO(_COMPACTED)), out)

    assert out.getvalue() == _COMPACTED


def test_grouping_holds_only_the_line_being_written() -> None:
    """Axiom 11: a writer holds neither stream, so grouping cannot be a list.

    ``_grouped`` returned one list per record -- a structure proportional to
    the session, measured at ~73 bytes per record and growing linearly
    (15096 / 29496 / 58776 bytes for 200 / 400 / 800 records). Only a patch's
    followers ever share a line, so the window is bounded and the groups are
    yielded rather than accumulated.
    """
    records: tuple[SessionRecord, ...] = tuple(
        UserMessage(content=f"line {index}") for index in range(400)
    )

    groups = _grouped(records)

    assert not isinstance(groups, list)
    assert [len(group) for group in groups] == [1] * 400


def test_grouping_still_joins_a_patch_that_touched_several_paths() -> None:
    """The bound must not cost the property grouping exists for.

    One ``apply_patch`` event reads as one record per path (axiom 10), and the
    writer rebuilds ONE event from them -- so the followers still have to
    arrive with the record that led their line.
    """
    lead = FileEditResult(call_id="c1", path="/w/a.py", extra={"$echoes": "x"})
    follower = FileEditResult(call_id="c1", path="/w/b.py")
    other = FileEditResult(call_id="c2", path="/w/c.py", extra={"$echoes": "x"})

    groups = [list(group) for group in _grouped((lead, follower, other))]

    assert groups == [[lead, follower], [other]]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
