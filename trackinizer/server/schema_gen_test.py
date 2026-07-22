"""Tests for schema generation helpers."""

from __future__ import annotations

from dataclasses import fields
from typing import Literal

import pytest

from trackinizer.server.schema_gen import (
    CHANGE_LOG_COLUMN_ORDER,
    SEQ_FOR_KIND,
    generate_edge_metadata_columns,
    generate_edge_metadata_mirror_new,
    generate_edge_metadata_mirror_old,
    quote_literal,
    substitute_schema_placeholders,
)
from trackinizer.server.sql import load_sql, schema_migrations
from trackinizer.types.change_log import Snapshot


# Snapshot fields generated OUTSIDE ``CHANGE_LOG_COLUMN_ORDER``: the edge-metadata
# mirror (peer triple + edge_* columns) and the composite ``marginal_cost`` (its
# own ``marginal_cost_*`` columns). Every OTHER Snapshot field is an audited
# inquiry column and MUST appear in the order, or its old/new value is silently
# dropped from the change_log INSERT (the AgentSession audit-loss bug).
_NON_COLUMN_ORDER_SNAPSHOT_FIELDS = frozenset(
    {
        "peer_id",
        "peer_kind",
        "peer_edge_kind",
        "edge_priority",
        "edge_note",
        "edge_valence",
        "edge_labels",
        "marginal_cost",
    }
)


def test_change_log_column_order_covers_every_audited_snapshot_field() -> None:
    """Every audited ``Snapshot`` field is in ``CHANGE_LOG_COLUMN_ORDER``.

    The change_log INSERT (``Store.emit_change``) and the schema mirror columns
    both derive from this order. A ``Snapshot`` field missing here means the
    audit records a change kind but drops the old/new value -- exactly the
    AgentSession bug, where ``set_cli`` / ``set_rooms`` etc. logged an event
    with NULL mirrors. This pins coverage so a new audited field cannot be
    forgotten.
    """
    audited = {
        f.name
        for f in fields(Snapshot)
        if f.name not in _NON_COLUMN_ORDER_SNAPSHOT_FIELDS
    }
    missing = audited - set(CHANGE_LOG_COLUMN_ORDER)
    assert not missing, (
        f"Snapshot fields missing from CHANGE_LOG_COLUMN_ORDER (audit silently "
        f"drops their old/new value): {sorted(missing)}"
    )


class TestPureFunctions:
    def test_seq_for_kind_covers_every_kind(self) -> None:
        kinds = {
            "Artifact",
            "Experiment",
            "Paper",
            "Belief",
            "Issue",
            "CodeChange",
            "WebResult",
            "WebSearch",
            "AgentSession",
        }
        assert set(SEQ_FOR_KIND.keys()) == kinds


class TestSchema:
    def test_schema_has_expected_tables(self) -> None:
        sql = load_sql("schema")
        for table in ("inquiries", "edges", "change_log", "inquiry_embeddings"):
            assert f"TABLE IF NOT EXISTS {table}" in sql

    def test_schema_inquiry_embeddings_pk_and_dim(self) -> None:
        sql = load_sql("schema")
        assert "vector(384)" in sql
        assert "PRIMARY KEY (inquiry_id, model)" in sql

    def test_schema_allows_many_to_many_supersedes_edges(self) -> None:
        sql = load_sql("schema")
        assert "idx_edges_one_supersedes_out" not in sql
        assert "idx_edges_one_supersedes_in" not in sql
        assert "edge_kind = 'supersedes'" in sql

    def test_produced_by_check_admits_any_inquiry_endpoints(self) -> None:
        """The ``produced_by`` CHECK admits any-Inquiry endpoints.

        Provenance is ``Inquiry -> Inquiry`` (stored produced -> producer), so
        any inquiry can be the produced child. The rendered CHECK arm must gate
        both endpoints with ``IN (<every inquiry kind>)``.
        """
        sql = substitute_schema_placeholders(load_sql("schema"))
        arm = sql.split("edge_kind = 'produced_by'", 1)[1].split("OR", 1)[0]
        assert "from_kind IN (" in arm
        for kind in ("'Issue'", "'Belief'", "'Experiment'"):
            assert kind in arm

    def test_schema_has_per_kind_sequences(self) -> None:
        sql = substitute_schema_placeholders(load_sql("schema"))
        for kind in (
            "issue",
            "artifact",
            "experiment",
            "paper",
            "belief",
            "codechange",
            "webresult",
            "websearch",
        ):
            assert f"CREATE SEQUENCE IF NOT EXISTS seq_{kind}" in sql

    def test_schema_emits_issue_kind_cardinality_check(self) -> None:
        """``min_items=1`` on ``Issue.issue_kind`` propagates from
        ``ColumnSpec`` metadata into both the per-kind ``inquiries``
        CHECK and the matching ``change_log`` mirror, so empty
        ``issue_kind`` is rejected by the DB rather than by a racy
        application-layer precheck. ``cardinality`` (not
        ``array_length(col, 1)``) because the latter returns NULL on
        empty arrays and CHECK treats NULL as pass.
        """
        sql = substitute_schema_placeholders(load_sql("schema"))
        assert "cardinality(issue_kind) >= 1" in sql
        assert "cardinality(old_issue_kind) >= 1" in sql
        assert "cardinality(new_issue_kind) >= 1" in sql

    def test_edge_metadata_columns_are_generated_from_edge_specs(self) -> None:
        columns = generate_edge_metadata_columns()
        assert "priority" in columns
        assert "INTEGER" in columns
        assert "note" in columns
        assert "note           TEXT" in columns
        assert "valence" in columns
        assert "DOUBLE PRECISION" in columns
        assert "labels" in columns
        assert "labels         TEXT[]" in columns
        assert "priority >= 0" in columns
        assert "edge_kind IN ('narrows', 'requires')" in columns
        assert "valence >= -1 AND valence <= 1" in columns

    def test_edge_metadata_mirrors_are_generated_from_edge_specs(self) -> None:
        old = generate_edge_metadata_mirror_old()
        new = generate_edge_metadata_mirror_new()
        assert "old_edge_priority INTEGER" in old
        assert "new_edge_priority INTEGER" in new
        assert "old_edge_priority >= 0" in old
        assert "new_edge_priority >= 0" in new
        assert "old_peer_edge_kind IN ('narrows', 'requires')" in old
        assert "new_peer_edge_kind IN ('narrows', 'requires')" in new
        sql = substitute_schema_placeholders(load_sql("schema"))
        assert "{edge_metadata_mirror" not in sql
        assert "old_edge_priority INTEGER" in sql
        assert "new_edge_priority INTEGER" in sql

    def test_canonical_schema_contains_folded_change_kinds(self) -> None:
        sql = substitute_schema_placeholders(load_sql("schema"))
        assert "'marginal_cost'" in sql
        assert "'edge_added'" in sql
        assert "'edge_removed'" in sql
        assert "'edge_annotation_changed'" in sql
        assert "'edge_changed'" not in sql

    def test_canonical_schema_contains_edge_priority_contract(self) -> None:
        sql = substitute_schema_placeholders(load_sql("schema"))
        assert "old_edge_priority INTEGER" in sql
        assert "new_edge_priority INTEGER" in sql
        assert "old_peer_edge_kind IN ('narrows', 'requires')" in sql
        assert "new_peer_edge_kind IN ('narrows', 'requires')" in sql
        assert "{edge_kinds}" not in sql
        assert "{inquiry_kinds}" not in sql

    def test_schema_migrations_enumerate_baseline_only(self) -> None:
        # The schema is squashed to a single clean baseline: ``schema.sql`` is
        # the only schema asset, with no numbered ``schema.NNN.sql`` migrations.
        migrations = list(dict(schema_migrations()))
        assert migrations == ["schema.sql"]

    def test_canonical_schema_contains_artifact_audit_columns(self) -> None:
        sql = substitute_schema_placeholders(load_sql("schema"))
        assert "old_codechange_sha TEXT" in sql
        assert "new_codechange_sha TEXT" in sql
        assert "old_webresult_url TEXT" in sql
        assert "new_webresult_url TEXT" in sql
        assert "kind = 'codechange_sha' OR old_codechange_sha IS NULL" in sql
        assert "kind = 'webresult_url' OR new_webresult_url IS NULL" in sql


class TestCLIHelpers:
    def testquote_literal_rejects_non_literal_target(self) -> None:
        """The codegen helper points at its caller, not at empty SQL."""
        with pytest.raises(AssertionError, match="no Literal members"):
            quote_literal(int)
        with pytest.raises(AssertionError, match="non-string members"):
            quote_literal(Literal[1, 2, 3])


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
