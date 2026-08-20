"""A regex operand is translated and compiled once, not once per row.

``match_filter`` evaluates one row, so the store calls it once per row of a
page with the same filter. ``re`` caches compilations, but its cache holds 512
entries while the list route admits ``MAX_LIST_LIMIT`` filters -- measured with
600 distinct operands over 200 rows, every lookup missed and each evaluation
cost 9.34us against 1.00us cached.

The cache is bounded because its key is a caller's operand.
"""

from __future__ import annotations

import re
import warnings

from trackinizer.wire.filters import Filter
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.row_filter import _compiled, match_filter


def test_repeated_rows_compile_the_pattern_once() -> None:
    _compiled.cache_clear()
    filt = Filter(field="title", op="re", value="^item-[0-9]+$")

    for index in range(50):
        match_filter({"title": f"item-{index}"}, filt)

    assert _compiled.cache_info().misses == 1


def test_the_cache_is_bounded_and_outsizes_one_request() -> None:
    # Bounded because the key is caller input: unbounded, the process would
    # retain every distinct pattern it is ever sent. Larger than the route's
    # filter cap because ``re``'s 512 entries are NOT, which is how a single
    # request came to evict its own patterns and recompile every row.
    maxsize = _compiled.cache_info().maxsize
    assert maxsize is not None
    assert maxsize > MAX_LIST_LIMIT


def test_compiling_a_warned_about_pattern_does_not_raise() -> None:
    # ``[[]`` is the literal-``[`` class: valid to both engines (live PG16
    # matches ``'a[b'``) and merely WARNED about by Python. Under this repo's
    # ``filterwarnings = ["error"]`` an unsuppressed warning IS an exception,
    # so compiling it here raised. It only appeared safe because the validator
    # had populated ``re``'s cache first -- an accident, and one that already
    # produced a bypass elsewhere in this module's history.
    # ``re.purge()`` as well as our own cache: measured, without it this
    # test passes with the production suppression REMOVED whenever a sibling
    # test compiled the pattern first, which is the very masking it exists to
    # catch.
    _compiled.cache_clear()
    re.purge()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _compiled("[[]").search("a[b") is not None


def test_distinct_patterns_still_match_independently() -> None:
    # A cache keyed on the wrong thing would answer one pattern with another.
    row = {"title": "alpha"}
    assert match_filter(row, Filter(field="title", op="re", value="^alpha$")) is True
    assert match_filter(row, Filter(field="title", op="re", value="^beta$")) is False


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
