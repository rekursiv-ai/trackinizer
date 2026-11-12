from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from trackinizer.server import sql_fragments
from trackinizer.server.sql_fragments import (
    CLAIM_NEXT_ISSUE_SQL,
    NEXT_ISSUE_SQL,
    PROVES_BELIEF_SQL,
    PROVING_EDGES_SQL,
)
from trackinizer.types.edges import EDGE_POLICIES, EdgeKindPolicy


if TYPE_CHECKING:
    import pytest


_SUPERSEDED_ISSUE = (
    "AND NOT EXISTS (SELECT 1 FROM edges p WHERE p.to_id = issue.id "
    "AND p.edge_kind IN ('supersedes'))"
)


def test_scheduler_exclusion_follows_the_registry() -> None:
    """Only ``supersedes`` hides an endpoint, and it hides the to-side."""
    clause = sql_fragments._policy_exclude_clauses(
        subject_alias="issue.id",
        policy_attr="skips_scheduler_on",
    )
    assert clause == _SUPERSEDED_ISSUE


def test_currency_exclusion_follows_the_registry() -> None:
    clause = sql_fragments._policy_exclude_clauses(
        subject_alias="t.id",
        policy_attr="invalidates_currency_on",
    )
    assert clause == (
        "AND NOT EXISTS (SELECT 1 FROM edges p WHERE p.to_id = t.id "
        "AND p.edge_kind IN ('supersedes'))"
    )


def test_exclusion_renders_one_sorted_clause_per_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A from-side policy gets its own ``from_id`` clause; kinds sort within a side."""
    base = EDGE_POLICIES["requires"]
    policies = {
        "supersedes": EdgeKindPolicy(
            description=base.description,
            forward_label="x",
            inverse_label="y",
            from_kinds="issue",
            to_kinds="issue",
            skips_scheduler_on="to",
        ),
        "requires": EdgeKindPolicy(
            description=base.description,
            forward_label="x",
            inverse_label="y",
            from_kinds="issue",
            to_kinds="issue",
            skips_scheduler_on="from",
        ),
        "narrows": EdgeKindPolicy(
            description=base.description,
            forward_label="x",
            inverse_label="y",
            from_kinds="issue",
            to_kinds="issue",
            skips_scheduler_on="from",
        ),
    }
    monkeypatch.setattr(sql_fragments, "EDGE_POLICIES", MappingProxyType(policies))
    clause = sql_fragments._policy_exclude_clauses(
        subject_alias="s.id",
        policy_attr="skips_scheduler_on",
    )
    assert clause == (
        "AND NOT EXISTS (SELECT 1 FROM edges p WHERE p.from_id = s.id "
        "AND p.edge_kind IN ('narrows', 'requires')) "
        "AND NOT EXISTS (SELECT 1 FROM edges p WHERE p.to_id = s.id "
        "AND p.edge_kind IN ('supersedes'))"
    )


def test_preview_and_claim_share_one_eligibility_predicate() -> None:
    """The preview and the claim must agree on what "available" means."""
    for sql in (NEXT_ISSUE_SQL, CLAIM_NEXT_ISSUE_SQL):
        assert "issue.owner IS NULL AND NOT issue.locked" in sql
        assert "prerequisite.status = 'active'" in sql
        assert _SUPERSEDED_ISSUE in sql
        assert "ORDER BY issue.issue_priority, issue.created" in sql
    assert NEXT_ISSUE_SQL.startswith("SELECT issue.* FROM inquiries issue WHERE ")
    assert "FOR UPDATE" not in NEXT_ISSUE_SQL
    assert "FOR UPDATE OF issue SKIP LOCKED) RETURNING *" in CLAIM_NEXT_ISSUE_SQL


def test_proof_queries_drop_superseded_citers() -> None:
    for sql in (PROVES_BELIEF_SQL, PROVING_EDGES_SQL):
        assert "p.to_id = t.id AND p.edge_kind IN ('supersedes')" in sql
        assert "{readable}" in sql
    assert PROVES_BELIEF_SQL.endswith(" ORDER BY t.created")
    assert "e.valence" in PROVING_EDGES_SQL


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
