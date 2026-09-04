"""IR record kinds as filter fields on the AgentSession row.

``trax agentsession tool_call re bar`` asks which SESSIONS mention something,
which is a filter on the list -- not a second query surface. A record field is
a LIST-shaped column whose elements happen to live in a side table
(``session_records``), so it inherits the membership semantics ``labels is
backend`` already has: any element matching satisfies the clause.

Which kinds qualify is DERIVED, not listed. A class is a field iff its
``text`` projection is non-empty (``types/session_records.py::search_text``):
a filter on ``TurnContext`` could never match, because nothing is indexed for
it. Deriving it is what keeps a record class added upstream from needing a
parallel edit here.
"""

from __future__ import annotations

from typing import Final

import re

from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextCompaction,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    ShellCommandResult,
    SystemMessage,
    Thinking,
    ToolCall,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResults,
)
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


__all__ = [
    "SESSION_RECORD_FIELDS",
    "record_kind_for",
    "snake_case",
]


# Every record class whose ``text`` projection can be non-empty. Listed here
# rather than walked from the union because the union is a type alias, not a
# runtime registry; ``session_record_fields_test`` asserts this agrees with
# ``search_text`` over every member, so a class added upstream fails that test
# rather than silently becoming unfilterable.
_TEXT_BEARING: Final[tuple[type[TraxRecord], ...]] = (
    UserMessage,
    AssistantMessage,
    SystemMessage,
    AgentToAgentMessage,
    Thinking,
    ToolCall,
    ShellCommandResult,
    FileReadResult,
    FileWriteResult,
    FileEditResult,
    WebSearchResults,
    WebFetchResult,
    AgentStatusResult,
    UncategorizedToolResult,
    ContextState,
    ContextCompaction,
    IncompleteRecord,
    # A captured stream is all prose, so every one of the three is filterable
    # -- ``agentsession std_err re 'Traceback'`` finds the runs that failed.
    Stdin,
    Stdout,
    Stderr,
)


_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_case(name: str) -> str:
    """``ToolCall`` -> ``tool_call``, the spelling a filter names.

    Only the lower-to-upper boundary splits, so ``WebSearchResults`` becomes
    ``web_search_results`` and an acronym run stays together.
    """
    return _CAMEL_BOUNDARY.sub("_", name).lower()


# Filter field -> the ``session_records.kind`` it selects. The field is the
# snake_cased class name, which is what makes ``agentsession tool_call re bar``
# read as the record it names.
SESSION_RECORD_FIELDS: Final[dict[str, str]] = {
    snake_case(cls.__name__): cls.__name__ for cls in _TEXT_BEARING
}


def record_kind_for(column: str) -> str | None:
    """The record kind ``column`` filters on, or ``None`` if it is not one."""
    return SESSION_RECORD_FIELDS.get(column)
