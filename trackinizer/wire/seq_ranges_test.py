"""Tests for the shared seq-range interval parser/formatter."""

from __future__ import annotations

import pytest

from trackinizer.wire.seq_ranges import (
    SeqRange,
    format_interval,
    parse_interval,
    parse_seq_range,
)


def test_parse_interval_open_and_closed_bounds() -> None:
    assert parse_interval("222..260") == SeqRange(start=222, stop=260)
    assert parse_interval("279..") == SeqRange(start=279)
    assert parse_interval("..10") == SeqRange(stop=10)


def test_parse_interval_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="invalid seq range"):
        parse_interval("1..2..3")
    with pytest.raises(ValueError, match="requires a start or stop"):
        parse_interval("..")
    with pytest.raises(ValueError, match="invalid seq range start"):
        parse_interval("foo..5")


def test_format_interval_round_trips_through_parse() -> None:
    for text in ("222..260", "279..", "..10", "7..7"):
        assert format_interval(parse_interval(text)) == text


def test_parse_seq_range_enforces_min_seq() -> None:
    """The one min-bound check both routes share, keyed only on ``min_seq``."""
    # Inquiry seq starts at 1: 0 is out of range, 1 is in.
    with pytest.raises(ValueError, match="start must be >= 1"):
        parse_seq_range("0..5", min_seq=1)
    assert parse_seq_range("1..5", min_seq=1) == SeqRange(start=1, stop=5)
    # Event seq starts at 0: 0 is in range under min_seq=0.
    assert parse_seq_range("0..5", min_seq=0) == SeqRange(start=0, stop=5)
    # The stop bound is checked too: stop 0 is below an inquiry min of 1.
    with pytest.raises(ValueError, match="stop must be >= 1"):
        parse_seq_range("..0", min_seq=1)


def test_seq_range_rejects_both_bounds_open() -> None:
    # A fully-open ``SeqRange(None, None)`` lowers to an empty bound list and
    # ``seq_range_clause`` would build ``()`` -- a SQL syntax error for a
    # direct Store caller that constructs the range itself (the wire parser
    # rejects bare ``..``, but a programmatic caller bypasses it). Reject at
    # construction so the invariant holds everywhere a SeqRange exists.
    with pytest.raises(ValueError, match="at least one bound"):
        SeqRange()
    with pytest.raises(ValueError, match="at least one bound"):
        SeqRange(start=None, stop=None)
    # A single present bound is still valid.
    assert SeqRange(start=5).start == 5
    assert SeqRange(stop=5).stop == 5


def test_parse_seq_range_rejects_inverted_interval() -> None:
    # A fat-fingered ``260..222`` selects nothing; reject it (400) rather than
    # return a silent empty 200 that reads as "no data".
    with pytest.raises(ValueError, match="start 260 exceeds stop 222"):
        parse_seq_range("260..222", min_seq=0)
    # An equal closed interval (one row) is still valid.
    assert parse_seq_range("7..7", min_seq=0) == SeqRange(start=7, stop=7)
    # Open-ended intervals have nothing to invert.
    assert parse_seq_range("260..", min_seq=0) == SeqRange(start=260)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
