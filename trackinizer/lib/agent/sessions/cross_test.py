"""An act crossing providers keeps its meaning (axiom 9).

Byte-exactness proves a session survives ITS OWN adapter. It says nothing
about the property the IR exists for: a record read from one provider has to
reach the other's wire. The two are independent, and the gap between them is
where records go missing silently -- a value round-trips through ``extra``,
so every byte check passes, while no consumer of the record can see it.

Each test here converts and then reads the OUTPUT back, because a writer that
drops a record still emits a well-formed file.
"""

from __future__ import annotations

from io import StringIO
from types import ModuleType

import json

import pytest

from trackinizer.lib.agent.sessions import claude, codex, gemini
from trackinizer.lib.agent.sessions.convert import detect_format
from trackinizer.lib.agent.sessions.fuse import chain
from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    SessionRecord,
    ToolResult,
    WebFetchResult,
    WebSearchResults,
)


def _claude_read_line() -> str:
    """Return the two claude lines one ``Read`` call and its answer occupy."""
    envelope = '"userType":"external","cwd":"/w","sessionId":"s1","version":"2.1.241"'
    call = (
        '{"parentUuid":null,"isSidechain":false,"type":"assistant","message":'
        '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
        '"name":"Read","input":{"file_path":"/w/a.py"}}]},'
        '"uuid":"u1","timestamp":"2026-09-02T00:00:00.000Z",' + envelope + "}\n"
    )
    answer = (
        '{"parentUuid":"u1","isSidechain":false,"type":"user","message":'
        '{"role":"user","content":[{"type":"tool_result","tool_use_id":"c1",'
        '"content":"1\\tbody"}]},'
        '"uuid":"u2","timestamp":"2026-09-02T00:00:01.000Z",'
        '"toolUseResult":{"type":"text","file":{"filePath":"/w/a.py",'
        '"content":"body","numLines":1,"startLine":1,"totalLines":1}},'
        + envelope
        + "}\n"
    )
    return call + answer


def _claude_write_line() -> str:
    """Return the two claude lines one ``Write`` call and its answer occupy."""
    envelope = '"userType":"external","cwd":"/w","sessionId":"s1","version":"2.1.241"'
    call = (
        '{"parentUuid":null,"isSidechain":false,"type":"assistant","message":'
        '{"role":"assistant","content":[{"type":"tool_use","id":"c1",'
        '"name":"Write","input":{"file_path":"/w/a.py"}}]},'
        '"uuid":"u1","timestamp":"2026-09-02T00:00:00.000Z",' + envelope + "}\n"
    )
    answer = (
        '{"parentUuid":"u1","isSidechain":false,"type":"user","message":'
        '{"role":"user","content":[{"type":"tool_result","tool_use_id":"c1",'
        '"content":"ok"}]},'
        '"uuid":"u2","timestamp":"2026-09-02T00:00:01.000Z",'
        '"toolUseResult":{"type":"create","filePath":"/w/a.py",'
        '"content":"body"},' + envelope + "}\n"
    )
    return call + answer


def _codex_meta() -> str:
    """Return the launch line every codex rollout opens with."""
    return (
        json.dumps(
            {
                "timestamp": "2026-08-24T19:34:39.215Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "01a03544-88de-71e2-981c-c8433de27ddc",
                    "id": "01a03544-88de-71e2-981c-c8433de27ddc",
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )


@pytest.mark.parametrize(
    ("native", "source", "expected"),
    [
        (_claude_read_line(), FileReadResult, FileReadResult),
        # Codex has no write TOOL, but its ``apply_patch`` add entry states a
        # file's whole bytes -- which IS a write, so the act keeps its type.
        (_claude_write_line(), FileWriteResult, FileWriteResult),
    ],
    ids=["read", "write"],
)
def test_a_claude_file_result_reaches_the_codex_wire(
    native: str, source: type[object], expected: type[object]
) -> None:
    """A typed file result must not vanish crossing to codex.

    ``_write_result`` routed a typed result only when it carried an
    ``$echoes`` marker or was one of three classes, so a claude-native
    ``Read``/``Write`` -- which carries neither -- fell through to ``None``
    and was dropped. The byte check never caught it: claude->claude never
    calls the codex writer at all.
    """
    records = list(claude.normalize(StringIO(native)))
    assert any(isinstance(record, source) for record in records)

    out = StringIO()
    codex.denormalize(records, out)

    crossed = list(codex.normalize(StringIO(out.getvalue())))
    assert any(isinstance(record, expected) for record in crossed)


@pytest.mark.parametrize(
    ("native", "path"),
    [
        (_claude_read_line(), "/w/a.py"),
        (_claude_write_line(), "/w/a.py"),
    ],
    ids=["read", "write"],
)
def test_a_crossed_file_result_keeps_the_path_it_named(native: str, path: str) -> None:
    """Crossing must preserve the act's subject, not merely its type.

    A ``FileChange`` is keyed BY path, and the reader took its call id and its
    diff while leaving :attr:`path` unset -- so the record named an edit to no
    file, and every codex-sourced edit crossed back anonymous.
    """
    records = list(claude.normalize(StringIO(native)))
    out = StringIO()
    codex.denormalize(records, out)

    crossed = list(codex.normalize(StringIO(out.getvalue())))
    found = [
        record
        for record in crossed
        if isinstance(record, FileReadResult | FileWriteResult | FileEditResult)
    ]

    assert [record.path for record in found] == [path]


def test_a_single_path_codex_edit_names_the_file_it_changed() -> None:
    """``changes`` is keyed by path, so the record must carry it.

    Measured on the corpus: 190 of 190 codex edits from ``item_completed``
    arrived with a diff and no path, and 407 of 451 change events name exactly
    one path -- so the key is unambiguous for the overwhelming majority.
    """
    native = _codex_meta() + (
        '{"type":"event_msg","payload":{"type":"item_completed","item":'
        '{"type":"FileChange","id":"c1","changes":{"/w/a.py":'
        '{"type":"update","unified_diff":"@@ -1 +1 @@\\n-a\\n+b\\n"}}}}}\n'
    )

    records = list(codex.normalize(StringIO(native)))

    record = records[-1]
    assert isinstance(record, FileEditResult)
    assert record.path == "/w/a.py"


@pytest.mark.parametrize(
    "record",
    [
        WebFetchResult(call_id="c1", url="https://x/", content="page"),
        AgentStatusResult(call_id="c1", agent_id="a1", content="report"),
    ],
    ids=["fetch", "agent"],
)
def test_an_act_codex_has_no_event_for_still_crosses(record: SessionRecord) -> None:
    """A record codex cannot type must still reach its wire.

    Codex writes no fetch and no subagent event -- neither appears in 400
    captured rollouts -- but every tool it does not model answers with a plain
    ``function_call_output``, so that is what these become. Returning nothing
    dropped the act entirely, leaving no trace of something the source
    recorded.
    """
    out = StringIO()

    codex.denormalize([record], out)

    written = out.getvalue()
    assert '"type":"function_call_output"' in written
    assert '"call_id":"c1"' in written


def _codex_edit_lines() -> str:
    """Return the codex call and event one ``apply_patch`` edit occupies."""
    return (
        '{"timestamp":"t","type":"response_item","payload":{"type":'
        '"custom_tool_call","call_id":"c1","name":"apply_patch","input":"p"}}\n'
        '{"timestamp":"t","type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"FileChange","id":"c1","changes":{"/w/a.py":'
        '{"type":"update","unified_diff":"@@ -1 +1 @@\\n-a\\n+A\\n"}}}}}\n'
    )


def _codex_search_line() -> str:
    """Return the codex event one web search occupies, which states no call."""
    return (
        '{"timestamp":"t","type":"event_msg","payload":{"type":"web_search_end",'
        '"call_id":"c3","query":"q","results":[{"url":"https://x/",'
        '"title":"T"}]}}\n'
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_codex_edit_lines(), FileEditResult),
        (_codex_search_line(), WebSearchResults),
    ],
    ids=["edit", "search"],
)
def test_a_codex_act_keeps_its_type_crossing_to_claude(
    payload: str, expected: type[object]
) -> None:
    """A typed act must still be typed on the other side (axiom 9).

    Claude types a tool result by the NAME of the call that opened it --
    ``Read``, ``Write``, ``Edit``, ``Bash`` -- and by a ``toolUseResult`` in
    that tool's own shape. A codex-sourced record carries neither: its call is
    named ``apply_patch`` or ``shell``, and its payload was built from a
    ``$result`` shape only a claude-read record has.

    So every typed act decayed to :class:`UncategorizedToolResult` on the way,
    measured over 150 captured rollouts as 42 files losing an act -- and the
    byte check never fired, because codex->codex never runs the claude writer.
    """
    native = _codex_meta() + payload
    records = list(codex.normalize(StringIO(native)))
    assert any(isinstance(record, expected) for record in records)

    out = StringIO()
    claude.denormalize(records, out)

    crossed = list(claude.normalize(StringIO(out.getvalue())))
    assert any(isinstance(record, expected) for record in crossed)


def test_a_crossed_codex_edit_keeps_the_file_and_the_change() -> None:
    """Type alone is not enough: the path and the splice have to arrive."""
    records = list(codex.normalize(StringIO(_codex_meta() + _codex_edit_lines())))

    out = StringIO()
    claude.denormalize(records, out)

    crossed = list(claude.normalize(StringIO(out.getvalue())))
    edits = [r for r in crossed if isinstance(r, FileEditResult)]
    assert [(r.path, len(r.edits)) for r in edits] == [("/w/a.py", 1)]


def test_an_image_carrying_detail_is_still_an_attachment() -> None:
    """``detail`` is metadata ABOUT an image, not a different kind of block.

    Both the ordering and decoding paths refused any block carrying it, so a
    valid data URL produced no attachment at all -- the image was invisible to
    the IR and to every consumer, while the bytes round-tripped through the
    residual and no byte check ever fired.
    """
    native = _codex_meta() + (
        '{"timestamp":"t","type":"response_item","payload":{"type":"message",'
        '"role":"user","content":[{"type":"input_image",'
        '"image_url":"data:image/png;base64,aGk=","detail":"high"}]}}\n'
    )

    records = list(codex.normalize(StringIO(native)))

    attachments = [
        found for record in records for found in getattr(record, "attachments", ())
    ]
    assert [found.data for found in attachments] == [b"hi"]
    out = StringIO()
    codex.denormalize(records, out)
    assert out.getvalue() == native


def test_an_unset_content_does_not_cross_as_an_empty_one() -> None:
    """Axiom 2: ``None`` is unset, an empty string is a value.

    A fetch that returned nothing and one that returned the empty string are
    different facts, and collapsing them invented a result the source never
    recorded.
    """
    out = StringIO()

    codex.denormalize([WebFetchResult(call_id="c1", url="u", content=None)], out)

    crossed = list(codex.normalize(StringIO(out.getvalue())))
    # Filtered to the results: every stream now opens with a ``TurnContext``,
    # which carries no content at all and would read as one more entry.
    results = [record for record in crossed if isinstance(record, ToolResult)]
    assert [getattr(record, "content", "") for record in results] == [None]


def test_a_path_beginning_with_a_dash_is_named_not_flagged() -> None:
    """A synthesized read must name its file, whatever the file is called.

    ``["/bin/cat", "-n"]`` runs a FLAG, so the act crossed as a command that
    reads nothing and the record lost its type on the way back.
    """
    out = StringIO()

    codex.denormalize([FileReadResult(call_id="c1", path="-n", content="x")], out)

    crossed = list(codex.normalize(StringIO(out.getvalue())))
    reads = [r for r in crossed if isinstance(r, FileReadResult)]
    assert [r.path for r in reads] == ["-n"]


def test_a_claude_only_record_does_not_fabricate_a_codex_line() -> None:
    """A record with no codex representation is dropped, not invented.

    Codex emits every uncategorized record by splitting its kind on ``/``, so
    a claude-only line -- ``queue-operation`` -- became a codex outer record of
    a type codex never writes. The mirror of the leak the claude writer guards.
    """
    envelope = '"userType":"external","cwd":"/w","sessionId":"s1","version":"2.1.241"'
    native = (
        '{"type":"queue-operation","operation":"enqueue","timestamp":"t",'
        + envelope
        + "}\n"
    )
    records = list(claude.normalize(StringIO(native)))

    out = StringIO()
    codex.denormalize(records, out)

    assert "queue-operation" not in out.getvalue()


@pytest.mark.parametrize(
    "blocks",
    [
        (
            '{"type":"text","text":"note"},'
            '{"type":"tool_result","tool_use_id":"c1","content":"ok"}'
        ),
        (
            '{"type":"tool_result","tool_use_id":"c1","content":"ok"},'
            '{"type":"text","text":"note"}'
        ),
    ],
    ids=["text-first", "result-first"],
)
def test_prose_and_a_tool_result_on_one_line_both_survive(blocks: str) -> None:
    """One line carries two acts, and the writer must fill both.

    ``_write_group`` dispatches on the group's HEAD, so whichever act came
    second was written from a stencil nothing filled: with prose first the
    result's content became ``null``, and with the result first the prose did.
    """
    envelope = '"userType":"external","cwd":"/w","sessionId":"s1","version":"2.1.241"'
    native = (
        '{"parentUuid":null,"isSidechain":false,"type":"user","message":'
        '{"role":"user","content":[' + blocks + "]},"
        '"uuid":"u1","timestamp":"t",' + envelope + "}\n"
    )
    records = list(claude.normalize(StringIO(native)))

    out = StringIO()
    claude.denormalize(records, out)

    assert out.getvalue() == native


def test_a_tool_result_carrying_text_does_not_abort_the_line() -> None:
    """A malformed block degrades; it does not kill the file.

    A ``tool_result`` that also carries a ``text`` key was indexed twice --
    once as the line's message, once as its result -- and the duplicate index
    made ``sorted`` fall through to comparing the records themselves. They
    define no ordering, so the whole file died on ``TypeError`` while every
    other malformed shape in the corpus survives.
    """
    envelope = '"userType":"external","cwd":"/w","sessionId":"s1","version":"2.1.241"'
    native = (
        '{"parentUuid":null,"isSidechain":false,"type":"user","message":'
        '{"role":"user","content":[{"type":"tool_result","tool_use_id":"c1",'
        '"text":"hi","content":"ok"}]},'
        '"uuid":"u1","timestamp":"2026-09-02T00:00:00.000Z",' + envelope + "}\n"
    )

    records = list(claude.normalize(StringIO(native)))

    out = StringIO()
    claude.denormalize(records, out)
    assert out.getvalue() == native


def test_a_session_whose_fork_link_cycles_is_still_ordered() -> None:
    """``chain`` is total: a component with no root still has to come back.

    The walk starts only from roots, and a cycle has none -- so those sessions
    were dropped silently. A rollout that forks from ITSELF emptied the list
    entirely, and the caller indexes ``ordered[0]``, so one malformed file
    aborted a whole corpus run with ``IndexError``.
    """
    native = (
        '{"type":"session_meta","payload":{'
        '"id":"01a03544-88de-71e2-981c-c8433de27ddc",'
        '"session_id":"01a03544-88de-71e2-981c-c8433de27ddc",'
        '"forked_from_id":"01a03544-88de-71e2-981c-c8433de27ddc"}}\n'
    )
    records = list(codex.normalize(StringIO(native)))

    assert chain([records]) == [records]


@pytest.mark.parametrize(
    ("target", "reader"),
    [("claude", claude), ("codex", codex)],
    ids=["claude", "codex"],
)
def test_a_gemini_session_crosses_to_the_other_providers(
    target: str, reader: object
) -> None:
    """Gemini is a source like any other, so its acts must reach both wires.

    The pairs above cover claude<->codex only, which is what let a gemini
    conversion regress unseen: the format was added alongside them and shares
    every writer, but nothing asserted a record leaving it survives.
    """
    del target
    native = json.dumps(
        {
            "sessionId": "s1",
            "messages": [
                {"type": "user", "content": "do it"},
                {
                    "type": "gemini",
                    "content": "running",
                    "toolCalls": [
                        {"id": "t1", "name": "read_file", "args": {"p": "x"}}
                    ],
                },
            ],
        },
        separators=(",", ":"),
    )
    records = list(gemini.normalize(StringIO(native)))
    assert isinstance(reader, ModuleType)

    out = StringIO()
    reader.denormalize(records, out)

    crossed = list(reader.normalize(StringIO(out.getvalue())))
    kinds = [type(record).__name__ for record in crossed]
    assert "UserMessage" in kinds
    assert "AssistantMessage" in kinds
    assert "ToolCall" in kinds


def test_a_codex_session_crosses_to_a_file_claude_can_read() -> None:
    """The claude writer must emit claude, not a mix of both formats.

    A codex record with no claude representation was written VERBATIM -- an
    ``UncategorizedRecord`` replays its own payload -- so the output opened
    with a codex line and was detected as neither format. A converted file
    that no reader recognizes is not a conversion.
    """
    native = _codex_meta() + (
        '{"timestamp":"2026-08-24T19:34:40.000Z","type":"response_item",'
        '"payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}}\n'
        '{"timestamp":"2026-08-24T19:34:41.000Z","type":"event_msg",'
        '"payload":{"type":"task_started","turn_id":"t1"}}\n'
    )
    records = list(codex.normalize(StringIO(native)))

    out = StringIO()
    claude.denormalize(records, out)

    assert detect_format(out.getvalue()) == "claude"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
