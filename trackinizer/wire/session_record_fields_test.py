"""Which IR record kinds are filter fields, and why that set is derived."""

from __future__ import annotations

from typing import Final

from trackinizer.sagent.session import types
from trackinizer.sagent.session.types import (
    AgentSpawnResult,
    AgentToAgentMessage,
    AssistantMessage,
    BashResult,
    ContextClear,
    ContextCompaction,
    ContextState,
    EditResult,
    IncompleteRecord,
    ReadResult,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    ToolResult,
    TurnContext,
    UncategorizedRecord,
    UserMessage,
    WebFetchResult,
    WebSearchResult,
    WriteResult,
)
from trackinizer.types.session_records import (
    _BY_KIND,
    record_class,
    record_kind,
    search_text,
)
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord
from trackinizer.wire.column_shapes import COLUMN_SHAPES, ColumnShape, sql_template
from trackinizer.wire.session_record_fields import (
    SESSION_RECORD_FIELDS,
    column_record_kind,
    snake_case,
)


class TestTheFieldSetIsDerived:
    """A class is a field iff its ``text`` projection can be non-empty."""

    def test_a_zero_text_record_class_is_not_a_field(self) -> None:
        """A filter on one could never match, so offering it would mislead.

        ``TurnContext`` and friends project to ``''``: nothing is indexed for
        them, so ``turn_context re x`` would answer "no sessions" for every
        corpus rather than "that is not a thing you can ask".
        """
        for cls in (TurnContext, TokenUsage, ContextClear, UncategorizedRecord):
            assert snake_case(cls.__name__) not in SESSION_RECORD_FIELDS

    def test_every_field_names_a_real_record_class(self) -> None:
        """The kind baked into each template must decode a stored row."""
        for field, kind in SESSION_RECORD_FIELDS.items():
            assert record_class(kind) in _BY_KIND.values(), (
                f"{field} names no record class"
            )

    def test_a_retired_kind_is_still_a_field(self) -> None:
        """``FunctionResult`` rows exist, so a filter must still reach them."""
        assert column_record_kind("function_result") == "FunctionResult"
        assert "FunctionResult" not in _BY_KIND

    def test_the_set_agrees_with_the_projection(self) -> None:
        """The rule, checked against ``search_text`` rather than restated.

        This is what makes the set derived: a record class added upstream
        fails HERE if it is text-bearing and unlisted, instead of silently
        becoming unfilterable.

        Each class is probed with a POPULATED sample, because ``search_text``
        of a default record is ``''`` whether or not the class has text to
        project -- an empty instance cannot tell the two apart.
        """
        assert set(_SAMPLES) == set(_BY_KIND), (
            "every record class needs a sample here, or the rule goes unchecked"
        )
        for kind, sample in _SAMPLES.items():
            assert record_kind(sample) == kind
            projects = bool(search_text(sample))
            listed = SESSION_RECORD_FIELDS.get(snake_case(kind)) == kind
            assert listed == projects, (
                f"{kind}: listed={listed} but projects text={projects}"
            )

    def test_all_but_the_four_qualify(self) -> None:
        """The count the design predicted, pinned so a drift is visible.

        Four never qualify, and they are the same four: the state records
        (``TurnContext``, ``ContextClear``), the accounting one
        (``TokenUsage``), and the one whose payload is untyped
        (``UncategorizedRecord``). Everything else carries prose.
        """
        assert len(_BY_KIND) == 43
        assert len(SESSION_RECORD_FIELDS) == 40


# One populated instance per stored kind, spelled out rather than reflected: the
# question is what ``search_text`` READS, and a hand-written sample states the
# text each kind carries where a generic filler only asserts that some field was
# set. A kind added upstream fails the membership assert above.
_SAMPLES: Final[dict[str, TraxRecord]] = {
    "UserMessage": UserMessage(content="probe"),
    "ToolErrorResult": ToolResult(call_id="c", state="failed", stderr=("probe",)),
    "ToolResult": ToolResult(call_id="c", state="pending", shown="probe"),
    "AssistantMessage": AssistantMessage(content="probe"),
    "SystemMessage": SystemMessage(content="probe"),
    "AgentToAgentMessage": AgentToAgentMessage(content="probe"),
    "Thinking": Thinking(content="probe"),
    "ToolCall": ToolCall(call_id="c", name="probe"),
    "BashResult": ToolResult(
        call_id="c",
        state="done",
        result=BashResult(command=("probe",)),
    ),
    "ReadResult": ToolResult(
        call_id="c",
        state="done",
        result=ReadResult(path="probe"),
    ),
    "WriteResult": ToolResult(
        call_id="c",
        state="done",
        result=WriteResult(path="probe"),
    ),
    "EditResult": ToolResult(
        call_id="c",
        state="done",
        result=EditResult(path="probe"),
    ),
    "WebSearchResult": ToolResult(
        call_id="c",
        state="done",
        result=WebSearchResult(query="probe"),
    ),
    "WebFetchResult": ToolResult(
        call_id="c",
        state="done",
        result=WebFetchResult(url="probe"),
    ),
    "WebBrowserResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.WebBrowserResult(),
    ),
    "AgentSpawnResult": ToolResult(
        call_id="c",
        state="done",
        result=AgentSpawnResult(prompt="probe"),
    ),
    "UncategorizedToolResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.UncategorizedToolResult(),
    ),
    "GrepResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.GrepResult(),
    ),
    "GlobResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.GlobResult(),
    ),
    "ListResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.ListResult(),
    ),
    "SkillResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.SkillResult(),
    ),
    "AgentSendResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.AgentSendResult(),
    ),
    "BackgroundTaskResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.BackgroundTaskResult(),
    ),
    "AgentSelfResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.AgentSelfResult(),
    ),
    "SessionResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.SessionResult(),
    ),
    "MemoryResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.MemoryResult(),
    ),
    "PlayAudioResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.PlayAudioResult(),
    ),
    "PaperSearchResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.PaperSearchResult(),
    ),
    "PaperDetailsResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.PaperDetailsResult(),
    ),
    "PaperAuthorResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.PaperAuthorResult(),
    ),
    "PaperFetchResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.PaperFetchResult(),
    ),
    "RemoteToolResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.RemoteToolResult(),
    ),
    "McpResult": ToolResult(
        call_id="c",
        state="done",
        stdout=("probe",),
        result=types.McpResult(),
    ),
    "ContextState": ContextState(content="probe"),
    "ContextCompaction": ContextCompaction(summary="probe"),
    "IncompleteRecord": IncompleteRecord(text="probe"),
    "Stdin": Stdin(text="probe"),
    "Stdout": Stdout(text="probe"),
    "Stderr": Stderr(text="probe"),
    # The four with no prose to project, populated as fully as they can be.
    "TurnContext": TurnContext(model="probe"),
    "TokenUsage": TokenUsage(),
    "ContextClear": ContextClear(),
    "UncategorizedRecord": UncategorizedRecord(kind="probe"),
}


class TestSnakeCase:
    """The spelling a filter names is the class name, snake_cased."""

    def test_splits_on_the_lower_to_upper_boundary(self) -> None:
        assert snake_case("ToolCall") == "tool_call"
        assert snake_case("WebSearchResult") == "web_search_result"
        assert snake_case("Thinking") == "thinking"

    def test_the_field_round_trips_to_its_kind(self) -> None:
        assert column_record_kind("tool_call") == ToolCall.__name__
        assert column_record_kind("web_search_result") == WebSearchResult.__name__
        assert column_record_kind("incomplete_record") == IncompleteRecord.__name__

    def test_a_non_record_field_is_not_one(self) -> None:
        assert column_record_kind("title") is None
        assert column_record_kind("turn_context") is None


class TestTheSqlBakesTheKindIn:
    """A record kind is never an operand; it is part of the template."""

    def test_every_field_carries_the_session_record_shape(self) -> None:
        for field in SESSION_RECORD_FIELDS:
            assert COLUMN_SHAPES[field] is ColumnShape.SESSION_RECORD

    def test_the_template_names_the_kind_literally(self) -> None:
        """Formatted from the closed derived set, so it cannot be injected."""
        template = sql_template("tool_call", "re")
        assert template is not None
        assert "r.kind = 'ToolCall'" in template
        assert "{col}" not in template, "the kind was left for the caller to format"

    def test_the_operand_placeholder_survives_for_the_caller(self) -> None:
        template = sql_template("thinking", "re")
        assert template is not None
        assert "{p}" in template

    def test_presence_ops_take_no_operand(self) -> None:
        """``context_compaction notnull`` asks only whether the kind occurs."""
        template = sql_template("context_compaction", "notnull")
        assert template is not None
        assert "{p}" not in template

    def test_the_subquery_correlates_on_the_row_being_filtered(self) -> None:
        """Uncorrelated, it would answer "any session at all", not this one."""
        template = sql_template("user_message", "is")
        assert template is not None
        assert "r.session_id = inquiries.id" in template

    def test_no_tsvector_prefilter(self) -> None:
        """A tsvector matches LEXEMES; a regex matches substrings.

        Measured on PG16: a path embedded in a line lexes as ONE token, which
        the tsquery for a word inside it misses while the regex matches. A
        prefilter would drop rows the regex keeps -- a wrong answer, not a
        slower one.
        """
        template = sql_template("tool_call", "re")
        assert template is not None
        assert "tsquery" not in template


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
