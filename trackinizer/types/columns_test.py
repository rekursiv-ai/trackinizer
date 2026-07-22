"""Tests for the flat storage-name derivation."""

from __future__ import annotations

from trackinizer.types.columns import (
    column_specs,
    storage_column_specs,
    storage_name,
)
from trackinizer.types.inquiries import (
    Belief,
    Inquiry,
    Issue,
    Paper,
    WebSearch,
)


class TestStorageName:
    def test_base_field_stays_bare(self) -> None:
        specs = column_specs(Inquiry)
        assert storage_name("status", specs["status"]) == "status"
        assert storage_name("owner", specs["owner"]) == "owner"
        assert storage_name("marginal_cost", specs["marginal_cost"]) == "marginal_cost"

    def test_kind_specific_field_is_prefixed(self) -> None:
        assert storage_name("source", column_specs(Paper)["source"]) == "paper_source"
        assert (
            storage_name("publication_type", column_specs(Paper)["publication_type"])
            == "paper_publication_type"
        )
        assert storage_name("venue", column_specs(Paper)["venue"]) == "paper_venue"
        assert (
            storage_name("judgement", column_specs(Belief)["judgement"])
            == "belief_judgement"
        )
        assert (
            storage_name("query", column_specs(WebSearch)["query"]) == "websearch_query"
        )

    def test_field_already_leading_with_owner_is_not_stuttered(self) -> None:
        assert (
            storage_name("issue_kind", column_specs(Issue)["issue_kind"])
            == "issue_kind"
        )

    def test_storage_specs_rekeyed_to_flat_name(self) -> None:
        keys = set(storage_column_specs(Paper))
        assert "paper_source" in keys
        assert "paper_publication_type" in keys
        assert "paper_venue" in keys
        assert "paper_source_kind" not in keys
        assert "source" not in keys
        # base columns still bare
        assert "status" in keys


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
