from __future__ import annotations

from trackinizer.types.columns import (
    column_specs,
    flat_column_specs,
    storage_name,
)
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
    AgentSession,
    Artifact,
    Belief,
    CodeChange,
    Experiment,
    Inquiry,
    Issue,
    Paper,
    WebResult,
    WebSearch,
)
from trackinizer.wire.filters import (
    NON_NULLABLE_COLUMNS,
    canonical_filter_field,
    validate_presence_op,
)


def test_every_kind_specific_bare_field_canonicalizes_to_its_storage_column() -> None:
    """Every kind-specific bare field name aliases to its ``<kind>_`` column.

    GSI-02 class: a kind-specific column stores under a ``<kind>_`` prefix
    (``paper_source``), but the CLI filters by the bare field (``source``). The
    alias map DERIVES these (``_kind_specific_aliases``); this recomputes the
    expected set the same way so a new kind-specific field is filterable with no
    edit -- a bare filter the CLI accepts can never 400 for a missing alias
    (the original ``google_scholar_id`` bug, now covering the cluster/cites
    split too).
    """
    for cls in KIND_TO_CLASS.values():
        for name, spec in column_specs(cls).items():
            storage = storage_name(name, spec)
            if storage != name:
                assert canonical_filter_field(name) == storage, (
                    f"bare filter field {name!r} does not resolve to its storage "
                    f"column {storage!r}"
                )
    # The two Google Scholar handles (cluster identity + cited-by pivot) each
    # resolve -- the split that replaced the single google_scholar_id.
    assert (
        canonical_filter_field("google_scholar_cluster_id")
        == "paper_google_scholar_cluster_id"
    )
    assert (
        canonical_filter_field("google_scholar_cites_id")
        == "paper_google_scholar_cites_id"
    )


def test_agentsession_filter_aliases_resolve_to_storage_columns() -> None:
    """AgentSession's bare CLI field names canonicalize to their storage names.

    The CLI filters an AgentSession by the bare field (``cli``, ``started``),
    the kind already in scope; the canonical SQL column is the
    ``agentsession_``-prefixed storage name. Omitting these aliases 400s a
    ``filter cli`` query, so each must resolve here.
    """
    assert canonical_filter_field("cli") == "agentsession_cli"
    assert canonical_filter_field("cli_session_id") == "agentsession_cli_session_id"
    assert canonical_filter_field("started") == "agentsession_started"
    assert canonical_filter_field("ended") == "agentsession_ended"
    assert canonical_filter_field("rooms") == "agentsession_rooms"


def test_non_nullable_columns_match_schema_metadata() -> None:
    """The NOT-NULL set tracks the column specs, not a hand-kept list.

    Drift guard: a future ``required=True`` field (or a new flattened cost
    axis) must appear here automatically, else ``isnull`` / ``notnull``
    validation silently breaks on it. The expected set is recomputed the
    same way the source derives it, so a hardcoded edit that desyncs from
    the specs fails loudly.
    """
    identity = {"id", "kind", "seq", "created", "modified"}
    expected = set(identity)
    for src in (
        Inquiry,
        Issue,
        Artifact,
        Experiment,
        Paper,
        Belief,
        CodeChange,
        WebResult,
        WebSearch,
        AgentSession,
    ):
        for name, flat in flat_column_specs(src).items():
            if flat.spec.required or flat.spec.flatten is not None:
                expected.add(storage_name(name, flat.spec))
    assert set(NON_NULLABLE_COLUMNS) == expected


def test_validate_presence_op_rejects_not_null_allows_nullable() -> None:
    """``isnull`` / ``notnull`` reject NOT-NULL columns and pass nullable ones."""
    assert validate_presence_op("id", "isnull") is not None
    assert validate_presence_op("status", "notnull") is not None
    assert validate_presence_op("marginal_cost_agent_usd", "isnull") is not None
    assert validate_presence_op("owner", "isnull") is None
    assert validate_presence_op("issue_kind", "notnull") is None
    # A value-bearing op on a NOT-NULL column is fine; the gate is presence-only.
    assert validate_presence_op("status", "is") is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
