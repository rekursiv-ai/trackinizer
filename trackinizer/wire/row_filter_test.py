from __future__ import annotations

from trackinizer.wire.filters import Filter
from trackinizer.wire.row_filter import match_filter


def test_re_matches_substring() -> None:
    row = {"owner": "Dan Smith"}
    assert match_filter(row, Filter(field="owner", op="re", value="Dan")) is True


def test_nre_is_negation_of_re() -> None:
    """``nre`` matches exactly the rows ``re`` rejects, and vice versa."""
    match = {"owner": "Dan Smith"}
    miss = {"owner": "Josh"}
    assert match_filter(match, Filter(field="owner", op="re", value="Dan")) is True
    assert match_filter(match, Filter(field="owner", op="nre", value="Dan")) is False
    assert match_filter(miss, Filter(field="owner", op="re", value="Dan")) is False
    assert match_filter(miss, Filter(field="owner", op="nre", value="Dan")) is True


def test_negated_ops_include_null() -> None:
    """A NULL column is *absent*, so a negated predicate over it is true.

    An unset field is trivially ``!= "question"`` and does not match
    ``/Dan/``; the negations must therefore keep the row. The affirmative
    ops still exclude it (there is no present value to equal or match).
    """
    row: dict[str, object] = {"owner": None}
    assert match_filter(row, Filter(field="owner", op="ne", value="Dan")) is True
    assert match_filter(row, Filter(field="owner", op="nre", value="Dan")) is True
    assert match_filter(row, Filter(field="owner", op="is", value="Dan")) is False
    assert match_filter(row, Filter(field="owner", op="re", value="Dan")) is False


def test_order_ops_exclude_null() -> None:
    """Ordering over an absent value is false: NULL sorts out of every range.

    Without this the string fallback would make ``"None" > "5"`` true
    (``'N' > '5'`` in ASCII), so ``priority gt 5`` would wrongly keep every
    NULL-priority row.
    """
    row: dict[str, object] = {"issue_priority": None}
    for op in ("lt", "le", "gt", "ge"):
        assert match_filter(row, Filter(field="priority", op=op, value="5")) is False


def test_isnull_matches_only_absent() -> None:
    """``isnull`` is true exactly when the column is NULL; value is ignored."""
    absent: dict[str, object] = {"owner": None}
    present: dict[str, object] = {"owner": "Dan"}
    assert match_filter(absent, Filter(field="owner", op="isnull", value="")) is True
    assert match_filter(present, Filter(field="owner", op="isnull", value="")) is False


def test_notnull_matches_only_present() -> None:
    """``notnull`` is the exact complement of ``isnull``."""
    absent: dict[str, object] = {"owner": None}
    present: dict[str, object] = {"owner": "Dan"}
    assert match_filter(absent, Filter(field="owner", op="notnull", value="")) is False
    assert match_filter(present, Filter(field="owner", op="notnull", value="")) is True


def test_nre_over_list_value_negates_any_match() -> None:
    """``nre`` over a list is true only when no element matches the pattern."""
    has_match: dict[str, object] = {"labels": ["backend", "hotfix"]}
    no_match: dict[str, object] = {"labels": ["frontend", "docs"]}
    assert (
        match_filter(has_match, Filter(field="labels", op="nre", value="hot")) is False
    )
    assert match_filter(no_match, Filter(field="labels", op="nre", value="hot")) is True
