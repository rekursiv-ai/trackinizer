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

from typing import TYPE_CHECKING, Final

import re

from trackinizer.sagent.session.types import (
    ContextClear,
    TokenUsage,
    TurnContext,
    UncategorizedRecord,
)
from trackinizer.types.session_records import _BY_KIND


if TYPE_CHECKING:
    from trackinizer.types.streams import TraxRecord


__all__ = [
    "SESSION_RECORD_FIELDS",
    "column_record_kind",
    "snake_case",
]


# The record kinds with no prose: settings, a window's start, token counts, and an
# untyped payload. ``session_record_fields_test`` checks the rest against
# ``search_text``, so a kind added upstream is filterable unless it is listed here.
_NO_TEXT: Final[frozenset[type[TraxRecord]]] = frozenset(
    (TurnContext, ContextClear, TokenUsage, UncategorizedRecord),
)

# Every record class whose ``text`` projection can be non-empty.
_TEXT_BEARING: Final[tuple[type[TraxRecord], ...]] = tuple(
    cls for cls in _BY_KIND.values() if cls not in _NO_TEXT
)


_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_case(name: str) -> str:
    """``ToolCall`` -> ``tool_call``, the spelling a filter names.

    Only the lower-to-upper boundary splits, so ``WebSearchResult`` becomes
    ``web_search_result`` and an acronym run stays together.

    Args:
      name: CamelCase identifier to convert.

    Returns:
      result: Converted snake_case string.

    """
    return _CAMEL_BOUNDARY.sub("_", name).lower()


# Filter field -> the ``session_records.kind`` it selects. The field is the
# snake_cased class name, which is what makes ``agentsession tool_call re bar``
# read as the record it names.
SESSION_RECORD_FIELDS: Final[dict[str, str]] = {
    snake_case(cls.__name__): cls.__name__ for cls in _TEXT_BEARING
}


def column_record_kind(column: str) -> str | None:
    """Return the record kind ``column`` filters on, or ``None`` if it is not one.

    Args:
      column: Snake_case field name (e.g., 'tool_call', 'web_search').

    Returns:
      result: CamelCase record kind if it matches a text-bearing record; None.

    """
    return SESSION_RECORD_FIELDS.get(column)
