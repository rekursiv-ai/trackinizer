"""The stored session record: its identity, its payload, and what it indexes.

Two properties matter here and nowhere else. The ``text`` projection decides
what a filter can ever match, so a record kind absent from it is invisible
forever; and ``encrypted`` must never reach it, since search would then leak
what the ciphertext table exists to isolate.
"""

from __future__ import annotations

from typing import Final, get_args
from uuid import uuid4

import pytest

from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
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
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResult,
    WebSearchResults,
)
from trackinizer.types.session_records import (
    _BY_KIND,
    MAX_SEARCH_TEXT_BYTES,
    SessionRecordRow,
    search_text,
)
from trackinizer.types.streams import TraxRecord


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
        )
    )

    assert "Bash" in text
    assert "rm -rf /tmp/x" in text


def test_shell_result_indexes_command_and_both_streams() -> None:
    """A failed command is findable by what it printed on either stream."""
    text = search_text(
        ShellCommandResult(
            call_id="c1",
            command=("ls", "/nope"),
            stdout="",
            stderr="No such file or directory",
            exit_code=2,
        )
    )

    assert "ls" in text
    assert "No such file or directory" in text


def test_file_edit_indexes_each_splice() -> None:
    """An edit is findable by the text it replaced and the text it wrote.

    ``FileEditResult`` carries no diff field -- a diff IS these splices -- so
    the projection reads them directly rather than rendering one.
    """
    text = search_text(
        FileEditResult(
            call_id="c1",
            path="a/b.py",
            edits=(Splice(before="old_name", after="new_name"),),
        )
    )

    assert "a/b.py" in text
    assert "old_name" in text
    assert "new_name" in text


def test_web_search_indexes_query_and_every_row() -> None:
    """Each result row's title and snippet is matchable, not just the query."""
    text = search_text(
        WebSearchResults(
            call_id="c1",
            query="postgres toast",
            content=(
                WebSearchResult(title="TOAST", snippet="oversized attributes"),
                WebSearchResult(title="Storage", snippet="EXTERNAL vs EXTENDED"),
            ),
        )
    )

    assert "postgres toast" in text
    assert "oversized attributes" in text
    assert "EXTERNAL vs EXTENDED" in text


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
            UncategorizedToolResult(call_id="c", content="hello"),
            id="UncategorizedToolResult",
        ),
        pytest.param(
            FileReadResult(call_id="c", path="p", content="hello"), id="FileRead"
        ),
        pytest.param(
            FileWriteResult(call_id="c", path="p", content="hello"), id="FileWrite"
        ),
        pytest.param(
            WebFetchResult(call_id="c", url="u", content="hello"), id="WebFetch"
        ),
        pytest.param(
            AgentStatusResult(call_id="c", prompt="p", content="hello"),
            id="AgentStatus",
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


def test_row_carries_the_records_context_and_timestamp() -> None:
    """``context_id`` and ``timestamp`` are columns, read off the record."""
    record = UserMessage(content="hi", context_id=2, timestamp="2026-09-02T00:00:00Z")
    row = SessionRecordRow.of(session_id=uuid4(), part=0, idx=7, record=record)

    assert row.context_id == 2
    assert row.timestamp is not None


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
        session_id=uuid4(), part=0, idx=0, record=UserMessage(content="hi")
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

    assert members == set(_BY_KIND)


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
