"""Tests for value-shape helpers."""

from __future__ import annotations

from trackinizer.server.values import (
    byline_strs,
    canonical_strs,
    manifest_bound,
    vec_to_text,
)


class TestPureFunctions:
    def testvec_to_text_round_trip(self) -> None:
        assert vec_to_text([1.0, -2.5, 0.0]).startswith("[")
        assert vec_to_text([1.0, 2.0]).endswith("]")
        assert "," in vec_to_text([1.0, 2.0])

    def test_manifest_bound_joins_and_predicates_on_the_alias(self) -> None:
        join, predicate = manifest_bound("r")
        assert "JOIN session_manifests m" in join
        assert "m.session_id = r.session_id AND m.part = r.part" in join
        assert predicate == "r.idx < m.records"

    def test_manifest_bound_binds_a_different_alias(self) -> None:
        join, predicate = manifest_bound("e")
        assert "m.session_id = e.session_id AND m.part = e.part" in join
        assert predicate == "e.idx < m.records"


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
