"""The stored session record: its identity, its payload, and what it indexes.

Two properties matter here and nowhere else. The ``text`` projection decides
what a filter can ever match, so a record kind absent from it is invisible
forever; and ``encrypted`` must never reach it, since search would then leak
what the ciphertext table exists to isolate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from typing import Final, get_args
from uuid import uuid4

from treekle import PlainTree, from_plain, immutable, json_loads, mutable

import pytest

from trackinizer.sagent.session.jsonl import record_line
from trackinizer.sagent.session.types import (
    AgentSpawnResult,
    AgentToAgentMessage,
    AssistantMessage,
    Attachment,
    BashResult,
    ContextClear,
    ContextCompaction,
    ContextState,
    EditResult,
    IncompleteRecord,
    ReadResult,
    SessionRecord,
    Splice,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    ToolResult,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchHit,
    WebSearchResult,
    WriteResult,
)
from trackinizer.types.session_records import (
    _BY_KIND,
    MAX_SEARCH_TEXT_BYTES,
    SessionRecordRow,
    record_kind,
    record_time,
    search_text,
    served_payload,
    stored_payload,
)
from trackinizer.types.streams import Stdout, TraxRecord


_CIPHERTEXT: Final = "gAAAAABqPBiCY9-vjMraAiiOTNS8xKmaodTJ4D2l6XR2pMszVFyz"


def test_thinking_indexes_plaintext_and_summary() -> None:
    """``Thinking`` contributes its readable halves."""
    text = search_text(Thinking(content="the plan", summary="a summary"))

    assert "the plan" in text
    assert "a summary" in text


def test_thinking_never_indexes_ciphertext() -> None:
    """The one projection rule the ciphertext table depends on.

    ``encrypted`` moves to ``session_ciphertext`` so retention can drop it and
    search cannot reach it. A projection that included it would make the
    separation cosmetic -- the bytes would still be in the tsvector.
    """
    text = search_text(Thinking(content="visible", encrypted=_CIPHERTEXT))

    assert "visible" in text
    assert _CIPHERTEXT not in text


def test_tool_call_indexes_name_and_string_arguments() -> None:
    """A call is findable by its tool and by its string-valued arguments."""
    text = search_text(
        ToolCall(
            call_id="c1",
            name="Bash",
            arguments={"command": "rm -rf /tmp/x", "timeout": 5},
        ),
    )

    assert "Bash" in text
    assert "rm -rf /tmp/x" in text


def test_shell_result_indexes_command_and_both_streams() -> None:
    """A failed command is findable by what it printed on either stream."""
    text = search_text(
        ToolResult(
            call_id="c1",
            state="done",
            stderr=("No such file or directory",),
            result=BashResult(command=("ls", "/nope"), exit_code=2),
        ),
    )

    # The exit code is a result field, not output text, so it is not indexed.
    assert text == "ls /nope\nNo such file or directory"


def test_file_edit_indexes_each_splice() -> None:
    """An edit is findable by the text it replaced and the text it wrote.

    ``EditResult`` carries no diff field -- a diff IS these splices -- so
    the projection reads them directly rather than rendering one.
    """
    text = search_text(
        ToolResult(
            call_id="c1",
            state="done",
            stdout=("Edited a/b.py.", Splice(before="old_name", after="new_name")),
            result=EditResult(path="a/b.py"),
        ),
    )

    assert "a/b.py" in text
    assert "old_name" in text
    assert "new_name" in text


def test_web_search_indexes_query_and_every_row() -> None:
    """Each result row's title and snippet is matchable, not just the query."""
    text = search_text(
        ToolResult(
            call_id="c1",
            state="done",
            stdout=(
                WebSearchHit(title="TOAST", snippet="oversized attributes"),
                WebSearchHit(title="Storage", snippet="EXTERNAL vs EXTENDED"),
            ),
            result=WebSearchResult(query="postgres toast"),
        ),
    )

    assert "postgres toast" in text
    assert "oversized attributes" in text
    assert "EXTERNAL vs EXTENDED" in text


@pytest.mark.parametrize(
    ("result", "stdout", "expected"),
    [
        pytest.param(
            ReadResult(path="/a.py"),
            ("out1 ", "out2"),
            "/a.py\nout1 out2\nerr1 err2",
            id="read",
        ),
        pytest.param(ReadResult(), ("out",), "out\nerr1 err2", id="read, no path"),
        pytest.param(
            WriteResult(path="/a.py", content="body"),
            ("Wrote /a.py.",),
            "/a.py\nbody\nerr1 err2",
            id="write",
        ),
        pytest.param(
            WriteResult(),
            ("Wrote the file.",),
            "err1 err2",
            id="write, no path or content",
        ),
        pytest.param(
            EditResult(),
            ("Edited the file.", Splice()),
            "err1 err2",
            id="edit, no path or text",
        ),
        pytest.param(
            WebSearchResult(query="q"),
            ("t\nu\ns", WebSearchHit(url="u", title="t", snippet="s")),
            "q\nt\ns\nerr1 err2",
            id="search",
        ),
        pytest.param(
            WebSearchResult(),
            (WebSearchHit(),),
            "err1 err2",
            id="search, no query or hit text",
        ),
        pytest.param(
            WebFetchResult(url="https://x"),
            ("page",),
            "https://x\npage\nerr1 err2",
            id="fetch",
        ),
        pytest.param(
            WebFetchResult(),
            ("page",),
            "page\nerr1 err2",
            id="fetch, no url",
        ),
        pytest.param(
            AgentSpawnResult(prompt="do it"),
            ("done",),
            "do it\ndone\nerr1 err2",
            id="spawn",
        ),
        pytest.param(
            AgentSpawnResult(),
            ("done",),
            "done\nerr1 err2",
            id="spawn, no prompt",
        ),
    ],
)
def test_a_tool_result_indexes_its_result_then_its_output(
    result: object,
    stdout: tuple[object, ...],
    expected: str,
) -> None:
    record = ToolResult(
        call_id="c",
        state="done",
        stdout=stdout,
        stderr=("err1 ", "err2"),
        result=result,
    )

    assert search_text(record) == expected


def test_a_placeholder_with_no_note_indexes_nothing() -> None:
    assert search_text(ToolResult(call_id="c", state="pending")) == ""


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(TurnContext(model="opus"), id="TurnContext"),
        pytest.param(TokenUsage(), id="TokenUsage"),
        pytest.param(ContextClear(cleared_session_id="x"), id="ContextClear"),
        pytest.param(UncategorizedRecord(kind="k"), id="UncategorizedRecord"),
    ],
)
def test_metadata_records_index_nothing(record: object) -> None:
    """Four kinds carry no prose, so they project to the empty string.

    They are deliberately NOT filter fields either: a clause on one could
    never match, so offering it would be a dead spelling.
    """
    assert search_text(record) == ""


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(UserMessage(content="hello"), id="UserMessage"),
        pytest.param(AssistantMessage(content="hello"), id="AssistantMessage"),
        pytest.param(SystemMessage(content="hello"), id="SystemMessage"),
        pytest.param(AgentToAgentMessage(content="hello"), id="AgentToAgent"),
        pytest.param(ContextState(content="hello"), id="ContextState"),
        pytest.param(ContextCompaction(summary="hello"), id="ContextCompaction"),
        pytest.param(IncompleteRecord(text="hello"), id="IncompleteRecord"),
        pytest.param(
            ToolResult(
                call_id="c",
                state="done",
                stdout=("hello",),
                result=UncategorizedToolResult(),
            ),
            id="UncategorizedToolResult",
        ),
        pytest.param(
            ToolResult(
                call_id="c",
                state="done",
                stdout=("hello",),
                result=ReadResult(path="p"),
            ),
            id="FileRead",
        ),
        pytest.param(
            ToolResult(
                call_id="c",
                state="done",
                stdout=("Wrote p.",),
                result=WriteResult(path="p", content="hello"),
            ),
            id="FileWrite",
        ),
        pytest.param(
            ToolResult(
                call_id="c",
                state="done",
                stdout=("hello",),
                result=WebFetchResult(url="u"),
            ),
            id="WebFetch",
        ),
        pytest.param(
            ToolResult(
                call_id="c",
                state="done",
                stdout=("hello",),
                result=AgentSpawnResult(prompt="p"),
            ),
            id="AgentStatus",
        ),
        pytest.param(
            ToolResult(call_id="c", state="pending", shown="hello"),
            id="ToolResult",
        ),
    ],
)
def test_prose_records_index_their_content(record: object) -> None:
    """Every prose-bearing kind reaches the index."""
    assert "hello" in search_text(record)


def test_projection_truncates_on_a_byte_bound() -> None:
    """The cap is BYTES, because the generated column is STORED.

    An oversized value aborts the INSERT rather than degrading, and a
    character cap would let 4-byte codepoints past a byte limit.
    """
    text = search_text(UserMessage(content="\U0001f600" * MAX_SEARCH_TEXT_BYTES))

    assert len(text.encode()) <= MAX_SEARCH_TEXT_BYTES


def test_truncation_never_splits_a_codepoint() -> None:
    """A cut lands on a character boundary, so the result still encodes."""
    text = search_text(UserMessage(content="\U0001f600" * MAX_SEARCH_TEXT_BYTES))

    assert text == text.encode().decode()


def test_row_round_trips_through_payload() -> None:
    """A row's payload decodes back to the record it was built from."""
    record = ToolCall(call_id="c1", name="Read", arguments={"path": "x"})
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=3, record=record)

    assert row.kind == "ToolCall"
    assert row.record() == record


def test_a_rows_json_fields_decode_frozen_as_readers_build_them() -> None:
    """Readers freeze each ``JSON`` field, so a stored record must decode frozen too."""
    record = ToolCall(
        call_id="c1",
        name="Read",
        arguments=immutable({"paths": ["a", "b"]}),
        extra=immutable({"seen": [1]}),
    )
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)

    decoded = row.record()

    assert decoded == record
    assert isinstance(decoded, ToolCall)
    assert isinstance(decoded.arguments, MappingProxyType)


def test_a_rows_payload_is_the_line_a_sagent_journal_writes() -> None:
    """Classes are named as the journal names them, nested ones and union members too."""
    record = ContextClear(
        summary="s",
        history=(
            UserMessage(
                content="u",
                attachments=(Attachment(mime_descriptor="image/png", data=b"\0"),),
            ),
            AssistantMessage(content="a"),
            ToolResult(
                call_id="c1",
                state="done",
                stdout=("Edited a.py.", Splice(before="x")),
                result=EditResult(path="a.py"),
            ),
        ),
    )
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)

    assert mutable(row.payload) == json_loads(record_line(record))
    assert row.record() == record


def test_a_stored_payload_keeps_the_ciphertext_a_row_strips() -> None:
    """The bare encoder is the row's, less the strip: a rewrite keeps what it read."""
    record = Thinking(content="visible", encrypted=_CIPHERTEXT)
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)

    assert stored_payload(record) == {**row.payload, "encrypted": _CIPHERTEXT}


def test_a_row_stored_under_a_former_kind_reads_as_the_record_now() -> None:
    """A row ingested before a record type was renamed carries its old name."""
    old = SessionRecordRow(
        session_id=uuid4(),
        idx=0,
        kind="ShellCommandResult",
        payload={"py/object": "ShellCommandResult", "call_id": "c", "stdout": "x"},
    )

    assert old.record() == ToolResult(
        call_id="c",
        state="done",
        stdout=("x",),
        result=BashResult(),
    )


def test_a_frozen_legacy_payload_decodes_its_lists() -> None:
    """A row's payload is frozen, so a legacy payload's lists arrive as tuples."""
    searched = SessionRecordRow(
        session_id=uuid4(),
        idx=0,
        kind="WebSearchResult",
        payload=immutable(
            {
                "py/object": "WebSearchResult",
                "call_id": "c",
                "query": "q",
                "content": [{"title": "T", "url": "u", "snippet": "s"}],
            },
        ),
    )
    edited = SessionRecordRow(
        session_id=uuid4(),
        idx=1,
        kind="EditResult",
        payload=immutable(
            {
                "py/object": "EditResult",
                "call_id": "c",
                "path": "a.py",
                "edits": [{"before": "x", "after": "y"}],
                "attachments": [
                    {"mime_descriptor": "image/png", "data": {"py/b64": "aW1hZ2U="}},
                ],
            },
        ),
    )

    assert searched.record() == ToolResult(
        call_id="c",
        state="done",
        stdout=("T\nu\ns", WebSearchHit(url="u", title="T", snippet="s")),
        result=WebSearchResult(query="q"),
    )
    assert edited.record() == ToolResult(
        call_id="c",
        state="done",
        stdout=(
            "Edited a.py.",
            Splice(before="x", after="y"),
            Attachment(mime_descriptor="image/png", data=b"image"),
        ),
        result=EditResult(path="a.py"),
    )


def test_a_row_of_an_unknown_kind_names_it() -> None:
    """A kind no record class carries is refused by name, not decoded as nothing."""
    row = SessionRecordRow(session_id=uuid4(), idx=0, kind="Nope", payload={})

    with pytest.raises(ValueError, match="unknown session record kind 'Nope'"):
        _ = row.record()


def test_a_stream_records_payload_names_its_class_alone() -> None:
    """Scrape records carry no import path either."""
    row = SessionRecordRow.of(
        session_id=uuid4(),
        part=0,
        idx=0,
        record=Stdout(text="ok\n"),
    )

    assert row.payload["py/object"] == "Stdout"
    assert row.record() == Stdout(text="ok\n")


def test_row_carries_the_records_context_and_timestamp() -> None:
    """``context_id`` and ``timestamp`` are columns, read off the record."""
    record = UserMessage(content="hi", context_id=2, timestamp="2026-09-02T00:00:00Z")
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=7, record=record)

    assert row.context_id == 2
    assert row.timestamp is not None


def test_an_unparseable_timestamp_stores_null_and_keeps_the_string() -> None:
    """A provider timestamp that is not ISO-8601 nulls the column, not the batch."""
    record = UserMessage(content="hi", timestamp="yesterday")
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)

    assert row.timestamp is None
    assert row.record() == record


def test_a_records_time_is_its_iso_timestamp_read_and_none_without_one() -> None:
    assert record_time(UserMessage(timestamp="2026-09-02T01:02:03+00:00")) == (
        datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)
    )
    assert record_time(UserMessage(timestamp="yesterday")) is None
    assert record_time(UserMessage()) is None
    assert record_time(Stdout(text="ok\n")) is None
    # What states no time, or states one that is no text, has none.
    assert record_time(object()) is None
    assert record_time(SimpleNamespace(timestamp=5)) is None


def test_row_strips_ciphertext_from_its_payload() -> None:
    """The stored payload holds ``encrypted=""``; the bytes ride elsewhere.

    No marker string: a plaintext field equal to one would be silently
    replaced on read. The ciphertext row's EXISTENCE is the splice signal.
    """
    record = Thinking(content="visible", encrypted=_CIPHERTEXT)
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)

    assert row.ciphertext == _CIPHERTEXT
    assert _CIPHERTEXT not in str(row.payload)


def test_a_record_without_ciphertext_reports_none() -> None:
    """Only a ``Thinking`` carrying ciphertext yields a ciphertext row."""
    row = SessionRecordRow.of(
        session_id=uuid4(),
        part=0,
        idx=0,
        record=UserMessage(content="hi"),
    )

    assert row.ciphertext is None


def test_every_record_kind_decodes() -> None:
    """The kind registry covers the whole ``TraxRecord`` union.

    The registry is hand-listed so a member added upstream fails HERE rather
    than at decode time on a stored row -- which would be a session that
    ingested fine and cannot be read back.

    ``TraxRecord``, not ``SessionRecord``: the store holds everything ``trax
    run`` can capture, which is the shared IR plus the stream records only a
    scrape emits.
    """
    members = {cls.__name__ for cls in _union_members(TraxRecord)}

    assert members == {cls.__name__ for cls in _BY_KIND.values()}
    assert members <= set(_BY_KIND)


def test_the_stream_records_widen_the_shared_ir() -> None:
    """``TraxRecord`` ADDS to ``SessionRecord`` rather than replacing it.

    Every CLI adapter still yields the shared IR unchanged; only the scrape
    contributes members, and only because a raw stream names no act. Asserted
    so a future edit cannot quietly fork the two vocabularies.
    """
    shared = {cls.__name__ for cls in _union_members(SessionRecord)}
    every = {cls.__name__ for cls in _union_members(TraxRecord)}

    assert shared < every
    assert every - shared == {"Stdin", "Stdout", "Stderr"}


def _union_members(alias: object) -> list[type]:
    """Flatten a possibly-nested type alias into its concrete classes."""
    out: list[type] = []
    stack: list[object] = [alias]
    while stack:
        entry = stack.pop()
        value = getattr(entry, "__value__", None)
        if value is None:
            assert isinstance(entry, type)
            out.append(entry)
            continue
        stack.extend(get_args(value))
    return out


@pytest.mark.parametrize(
    ("record", "kind"),
    [
        pytest.param(UserMessage(content="hi"), "UserMessage", id="message"),
        pytest.param(
            ToolResult(call_id="c", state="done", result=BashResult(exit_code=0)),
            "BashResult",
            id="shell call",
        ),
        pytest.param(
            ToolResult(call_id="c", state="done", result=ReadResult(path="p")),
            "ReadResult",
            id="read call",
        ),
        pytest.param(
            ToolResult(call_id="c", state="failed", stderr=("boom",)),
            "ToolErrorResult",
            id="failed call",
        ),
        pytest.param(
            ToolResult(call_id="c", state="pending", shown="[running]"),
            "ToolResult",
            id="pending call",
        ),
        pytest.param(
            ToolResult(call_id="c", state="cancelled"),
            "ToolResult",
            id="cancelled call",
        ),
        pytest.param(
            ToolResult(
                call_id="c",
                state="done",
                stdout=("ok",),
                result=UncategorizedToolResult(),
            ),
            "UncategorizedToolResult",
            id="tool without a result class",
        ),
    ],
)
def test_a_row_is_stored_under_its_record_kind(
    record: TraxRecord,
    kind: str,
) -> None:
    """SQL policy is keyed on ``kind``, so a tool-call row keeps its legacy name."""
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)

    assert record_kind(record) == kind
    assert row.kind == kind
    assert row.record() == record


def test_a_legacy_tool_call_payload_is_converted_to_the_current_shape() -> None:
    legacy = {
        "py/object": "BashResult",
        "call_id": "c",
        "command": ["ls"],
        "stdout": "a.py\n",
        "exit_code": 0,
    }
    current = ToolResult(
        call_id="c",
        state="done",
        stdout=("a.py\n",),
        result=BashResult(command=("ls",), exit_code=0),
    )

    payload = served_payload("BashResult", immutable(legacy))

    assert payload == stored_payload(current)
    # A kind that was renamed before it was retired converts the same way.
    assert served_payload("ShellCommandResult", immutable(legacy)) == payload
    # A payload already in the current shape is returned as is.
    assert served_payload("BashResult", payload) is payload
    assert served_payload("ToolResult", payload) is payload


def test_a_legacy_payload_that_looks_current_is_still_converted() -> None:
    """Neither a ``ToolResult`` tag nor a ``state`` field marks the current shape."""
    legacy_pending = immutable(
        {
            "py/object": "ToolResult",
            "call_id": "c",
            "stub": "pending",
            "shown": "[running]",
        },
    )
    legacy_spawn = immutable(
        {
            "py/object": "AgentSpawnResult",
            "call_id": "c",
            "content": "Found 3 uses.",
            "state": "completed",
        },
    )

    assert served_payload("ToolResult", legacy_pending) == stored_payload(
        ToolResult(call_id="c", state="pending", shown="[running]"),
    )
    assert served_payload("AgentSpawnResult", legacy_spawn) == stored_payload(
        ToolResult(
            call_id="c",
            state="done",
            stdout=("Found 3 uses.",),
            result=AgentSpawnResult(state="completed"),
        ),
    )


def test_a_bare_exit_code_result_is_converted_to_a_result_class() -> None:
    """An earlier ``ToolResult`` payload stored a bare exit code as ``result``."""
    stored = immutable(
        {
            "py/object": "ToolResult",
            "call_id": "c",
            "state": "done",
            "stdout": ["posted"],
            "result": 1,
        },
    )

    assert served_payload("UncategorizedToolResult", stored) == stored_payload(
        ToolResult(
            call_id="c",
            state="done",
            stdout=("posted",),
            result=UncategorizedToolResult(exit_code=1),
        ),
    )


@pytest.mark.parametrize(
    "kind",
    ["ReadResult", "ShellCommandResult", "UncategorizedToolResult", "ToolResult"],
)
def test_an_offloaded_stub_is_returned_unchanged(kind: str) -> None:
    """An offloaded body's row holds a stub, which is not a record payload."""
    stub = immutable({"$body": "offloaded"})

    assert served_payload(kind, stub) is stub


@pytest.mark.parametrize(
    "payload",
    [
        {"call_id": "c", "stdout": "ok"},
        {"py/object": "UserMessage", "content": "hi"},
    ],
    ids=["untagged", "tagged as another record"],
)
def test_a_tool_call_payload_that_does_not_decode_is_returned_unchanged(
    payload: dict[str, PlainTree],
) -> None:
    """Ingest does not validate payloads; one bad row must not fail a read."""
    stored = immutable(payload)

    assert served_payload("BashResult", stored) is stored


def test_a_payload_of_another_kind_is_returned_unchanged() -> None:
    payload = stored_payload(UserMessage(content="hi"))
    legacy_shell = immutable({"py/object": "BashResult", "call_id": "c"})

    assert served_payload("UserMessage", payload) is payload
    # An unknown kind is not a tool-call kind, whatever its payload holds.
    assert served_payload("Nope", legacy_shell) is legacy_shell


def test_templates_decode_from_both_frozen_and_mutable_payloads() -> None:
    record = SystemMessage(
        content="instructions",
        extra=immutable(
            {"$templates": [{"role": "developer", "content": ["instructions"]}]},
        ),
    )
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record)
    for decoded in (
        from_plain(row.payload, SystemMessage),
        from_plain(mutable(row.payload), SystemMessage),
        row.record(),
    ):
        assert isinstance(decoded, SystemMessage)
        assert decoded.content == record.content
        assert mutable(decoded.extra) == mutable(record.extra)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
