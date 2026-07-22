"""Tests for value-shape helpers."""

from __future__ import annotations

from trackinizer.server.values import (
    byline_strs,
    canonical_strs,
    vec_to_text,
)


class TestPureFunctions:
    def testvec_to_text_round_trip(self) -> None:
        assert vec_to_text([1.0, -2.5, 0.0]).startswith("[")
        assert vec_to_text([1.0, 2.0]).endswith("]")
        assert "," in vec_to_text([1.0, 2.0])


class TestModels:
    def testcanonical_strs_strip_dedup_preserve_order(self) -> None:
        assert canonical_strs(["a", "b", "a", "  c  ", "b", ""]) == (
            "a",
            "b",
            "c",
        )
        assert canonical_strs([]) == ()
        assert canonical_strs(["   "]) == ()

    def testbyline_strs_strips_drops_blank_keeps_order_and_dups(self) -> None:
        # Unlike canonical_strs, a byline keeps order AND duplicates; it only
        # strips per element and drops blanks. One contract for paper authors
        # on both the submit and the edit path.
        assert byline_strs([" Ada ", "Ada", "", "  ", "Grace"]) == (
            "Ada",
            "Ada",
            "Grace",
        )
        assert byline_strs([]) == ()
        assert byline_strs(["   "]) == ()


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
