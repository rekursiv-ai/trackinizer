"""Unit tests for the log-odds confidence fold."""

from __future__ import annotations

import math

import pytest

from trackinizer.types.belief_confidence import fold_confidence, logistic


def test_no_evidence_is_neutral() -> None:
    assert fold_confidence(0.0) == 0.5


def test_support_lifts_above_neutral() -> None:
    assert fold_confidence(1.0) > 0.5


def test_attack_lowers_below_neutral() -> None:
    assert fold_confidence(-1.0) < 0.5


def test_is_symmetric_about_neutral() -> None:
    """Equal-magnitude support and attack are mirror images across 0.5."""
    assert fold_confidence(2.3) - 0.5 == pytest.approx(0.5 - fold_confidence(-2.3))


def test_range_is_open_zero_one_for_moderate_evidence() -> None:
    """Strictly inside (0, 1) until float saturation at the extreme tails."""
    for log_odds in (-30.0, -1.0, 0.0, 1.0, 30.0):
        value = fold_confidence(log_odds)
        assert 0.0 < value < 1.0

    # At the extreme tails the double rounds to the closed endpoint; that is
    # correct saturation, not a range violation.
    assert 0.0 <= fold_confidence(-800.0) < 1.0
    assert 0.0 < fold_confidence(800.0) <= 1.0


def test_large_support_approaches_one_without_overflow() -> None:
    """A heavily-supported node returns ~1.0, not OverflowError."""
    assert fold_confidence(10_000.0) == pytest.approx(1.0)


def test_large_attack_approaches_zero_without_overflow() -> None:
    assert fold_confidence(-10_000.0) == pytest.approx(0.0)


def test_logistic_matches_the_reference_form_where_it_computes() -> None:
    """The overflow-safe logistic equals the naive form on the safe range."""
    for x in (-9.0, -0.5, 0.0, 0.5, 9.0):
        assert logistic(x) == pytest.approx(1.0 / (1.0 + math.exp(-x)))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
