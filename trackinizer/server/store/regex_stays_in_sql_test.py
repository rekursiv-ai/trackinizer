r"""No request may route a regex into the Python evaluator.

Postgres bounds a pathological pattern: its hybrid NFA/DFA does not backtrack,
and the statement timeout catches what remains. Python has neither -- ``(a+)+$``
over 30 characters measured **79.89 seconds** through ``re``, doubling per
character, holding the event loop for every concurrent request.

Two mechanisms keep a caller's regex away from that: a column with SQL lowers
its regex into ``~``, and a column without SQL is refused outright. Both are
exercised elsewhere; what this file pins is that the two together leave NO
remainder. The invariant is what makes the pattern's cost a non-issue
server-side -- lose it, and a regex reaching ``match_filter`` is a hang, not a
slow query.
"""

from __future__ import annotations

from trackinizer.server.api.query import _filter_columns_for
from trackinizer.server.store.read import _partition_filters
from trackinizer.types.errors import ValidationError
from trackinizer.types.inquiries import KIND_TO_CLASS
from trackinizer.wire.filters import Filter


def test_no_filterable_column_routes_a_regex_to_python() -> None:
    """Every column a CALLER may filter either lowers its regex or refuses it.

    Enumerated from what the route accepts, not from the classified columns:
    the set of classified columns excludes by construction the JSONB ones that
    cannot lower -- precisely the columns at risk. Iterating it asked whether
    the safe columns are safe.
    """
    columns = {column for kind in KIND_TO_CLASS for column in _filter_columns_for(kind)}
    assert "experiment_config" in columns, "the JSONB column must be in scope"

    escaped: list[tuple[str, str]] = []
    for column in sorted(columns):
        for op in ("re", "nre"):
            try:
                _, remaining = _partition_filters(
                    (Filter(field=column, op=op, value="x"),), []
                )
            except ValidationError:
                continue
            if remaining:
                escaped.append((column, op))
    assert not escaped, f"regex reaches the Python evaluator for {escaped}"


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
