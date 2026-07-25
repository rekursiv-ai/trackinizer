"""The edge topology has ONE source of truth: ``types.edges.edge_topology``.

A citation-direction change can break the SPA when the edge direction is encoded
twice -- in the schema CHECK and in hand-typed SPA JS -- with no parity check, so
changing one leaves the other stale. This test pins the Python topology against
the generated schema edge-validity CHECK, and ``/api/meta/edges`` serves the same
topology to the SPA. A future direction change that updates one but not the other
fails here, at unit speed, instead of in the browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import re

from trackinizer.server.schema_gen import (
    substitute_schema_placeholders,
)
from trackinizer.server.sql import load_sql
from trackinizer.types import inquiries
from trackinizer.types.edges import (
    EDGE_POLICIES,
    PRODUCED_INFERENCE_NEUTRAL,
    PRODUCED_INFERENCE_PRECEDENCE,
    PRODUCED_INFERENCE_SUPPRESSED,
    Edge,
    edge_topology,
)


def _schema_edge_arms() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Parse ``edge_kind -> (from_kinds, to_kinds)`` from the schema edge CHECK.

    Each arm is ``(edge_kind = 'X' OR edge_kind IN (...)) AND from_kind ...
    AND to_kind ...``; this recovers the admitted kind sets per edge_kind so the
    test can set-compare them against :func:`edge_topology`.
    """
    sql = substitute_schema_placeholders(load_sql("schema"))
    # The edges table body, up to the next CREATE. The edge-validity CHECK (the
    # from/to-kind arms) sits AFTER the PRIMARY KEY line, so the block must span
    # the whole table definition, not stop at the key.
    start = sql.index("CREATE TABLE IF NOT EXISTS edges")
    block = sql[start : sql.index("CREATE", start + len("CREATE TABLE"))]

    arm = re.compile(
        r"edge_kind\s*(?:=\s*'(?P<one>\w+)'|IN\s*\((?P<many>[^)]*)\))\s+"
        r"AND\s+from_kind\s*(?:=\s*'(?P<f1>\w+)'|IN\s*\((?P<fmany>[^)]*)\))\s+"
        r"AND\s+to_kind\s*(?:=\s*'(?P<t1>\w+)'|IN\s*\((?P<tmany>[^)]*)\))",
        re.IGNORECASE | re.DOTALL,
    )

    def members(one: str | None, many: str | None) -> frozenset[str]:
        if one is not None:
            return frozenset({one})
        assert many is not None
        return frozenset(re.findall(r"'(\w+)'", many))

    out: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for m in arm.finditer(block):
        kinds = members(m["one"], m["many"])
        from_kinds = members(m["f1"], m["fmany"])
        to_kinds = members(m["t1"], m["tmany"])
        for kind in kinds:
            out[kind] = (from_kinds, to_kinds)
    return out


def test_edge_topology_matches_schema_check() -> None:
    """``edge_topology`` and the schema edge-validity CHECK agree on every kind.

    Set comparison (not order) per edge_kind. This is the drift gate: a citation
    direction must show the SAME from/to kinds in both the Python registry and
    the SQL, so the SPA (which derives from the registry via ``/api/meta/edges``)
    can never lag the schema again.
    """
    schema_arms = _schema_edge_arms()
    topo = edge_topology()
    assert set(schema_arms) == set(topo), (
        "edge kinds differ between schema CHECK and edge_topology:\n"
        f"  schema-only: {sorted(set(schema_arms) - set(topo))}\n"
        f"  topology-only: {sorted(set(topo) - set(schema_arms))}"
    )
    for kind in topo:
        sf, st = schema_arms[kind]
        assert frozenset(topo[kind]["from_kinds"]) == sf, f"{kind} from_kinds drift"
        assert frozenset(topo[kind]["to_kinds"]) == st, f"{kind} to_kinds drift"


def test_edge_topology_covers_every_edge_kind() -> None:
    """Every ``Edge.Kind`` literal has a topology entry (no kind unmapped)."""
    assert set(edge_topology()) == set(get_args(Edge.Kind.__value__))


def test_citations_store_artifact_to_claimable() -> None:
    """Citations are pinned: ``proves``/``favors`` store Artifact -> claim.

    The citing evidence (child) is the from-side (any Artifact kind); the cited
    claim (parent) is the to-side, exactly ``{Belief, Experiment}``. For-vs-
    against is the valence sign, so there is no separate against-edge kind.
    """
    topo = edge_topology()
    for kind in ("proves", "favors"):
        assert topo[kind]["to_kinds"] == ["Belief", "Experiment"]
        assert "Paper" in topo[kind]["from_kinds"]
        assert "Belief" in topo[kind]["from_kinds"]


def test_cites_paper_stores_paper_to_paper() -> None:
    """``cites_paper`` (historical citation) is pinned Paper -> Paper both sides.

    Distinct from the epistemic proves/favors: both endpoints are Paper-only, so
    an in-house artifact (Belief/Experiment/CodeChange) can never be a historical
    citation endpoint.
    """
    topo = edge_topology()
    assert topo["cites_paper"]["from_kinds"] == ["Paper"]
    assert topo["cites_paper"]["to_kinds"] == ["Paper"]


def test_produced_inference_precedence_and_neutral_partition_every_edge_kind() -> None:
    """PRECEDENCE and NEUTRAL partition ``Edge.Kind`` -- each kind is placed once.

    A new edge kind must be classified deliberately: either it ranks in the
    first-edge precedence list (participates in provenance inference) or it is
    explicitly provenance-NEUTRAL. This fails if a kind is added to neither
    (silently undefined inference behavior) or to both (contradiction).
    """
    all_kinds = set(get_args(Edge.Kind.__value__))
    assert set(PRODUCED_INFERENCE_PRECEDENCE) | PRODUCED_INFERENCE_NEUTRAL == all_kinds
    assert set(PRODUCED_INFERENCE_PRECEDENCE) & PRODUCED_INFERENCE_NEUTRAL == set()
    assert len(PRODUCED_INFERENCE_PRECEDENCE) == len(set(PRODUCED_INFERENCE_PRECEDENCE))


def test_citation_kinds_are_provenance_neutral() -> None:
    """Every citation kind is neutral: absent from PRECEDENCE and SUPPRESSED both.

    A citation records that the citer points AT a target, never that the target
    produced the citer -- true for ``cites_paper`` (Paper->Paper) and equally for
    the epistemic ``proves``/``favors`` (Artifact->Belief/Experiment): the
    evidence predates and is independent of the claim it later supports. Absent
    from PRECEDENCE, a lone citation never wins inference (winner is None), so no
    ``produced_by`` is stamped. Absent from SUPPRESSED, it never vetoes inference
    from a coexisting structural edge.

    Regression: ``proves``/``favors`` were mistakenly ranked in PRECEDENCE, which
    stamped one bogus ``produced_by`` parent on a cited Belief per citing Paper.
    """
    for kind in ("cites_paper", "proves", "favors"):
        assert kind in PRODUCED_INFERENCE_NEUTRAL
        assert kind not in PRODUCED_INFERENCE_PRECEDENCE
        assert kind not in PRODUCED_INFERENCE_SUPPRESSED


def test_every_edge_kind_has_an_acyclicity_policy() -> None:
    """Acyclicity is a DECLARED per-kind property, not blanket behavior.

    ``EdgeKindPolicy.enforces_acyclicity`` decides whether a kind's subgraph is
    kept a DAG. Every ``Edge.Kind`` must have a policy (so a new kind cannot
    silently inherit blanket cycle-rejection), and ``cites_paper`` -- the lone
    external-fact kind -- must be exempt so mutual citation is storable.
    """
    policies = EDGE_POLICIES
    assert set(policies) == set(get_args(Edge.Kind.__value__))
    assert policies["cites_paper"].enforces_acyclicity is False
    for kind, policy in policies.items():
        if kind != "cites_paper":
            assert policy.enforces_acyclicity is True, (
                f"{kind} unexpectedly acyclicity-exempt"
            )


def test_produces_docstring_does_not_claim_universal_inference() -> None:
    """The authoritative provenance docstring stays truthful about neutrality.

    ``Inquiry.produces`` (types/inquiries.py) is the source-of-record for the
    provenance rule; ``edges.py`` cross-references it. Once a provenance-NEUTRAL
    kind exists, that prose can no longer claim inference fires on "ANY" kind or
    that there is "no per-kind exception set" -- both are false for a neutral
    kind. Pin the doc to the code so the two cannot drift again (the exact drift
    the cites_paper review caught).
    """
    # Field docstrings are bare string expressions Python discards, so read the
    # module source and scope to the ``produces`` field's docstring block.
    src = Path(inquiries.__file__).read_text(encoding="utf-8")
    start = src.index("    produces: tuple[InquiryEdge, ...] = ()")
    end = src.index("    produced_by: tuple[InquiryEdge, ...] = ()", start)
    produces_doc = src[start:end]
    assert "no per-kind exception set" not in produces_doc
    assert "PRODUCED_INFERENCE_NEUTRAL" in produces_doc


def test_only_produced_by_is_suppressed_for_idempotency() -> None:
    """Inference fires on structural kinds; the lone suppressed kind is ``produced_by``.

    Each STRUCTURAL kind (PRECEDENCE) stores younger -> older AND implies the
    older produced the younger, so a first such edge infers the same ``younger
    produced_by older`` -- none is age-inverted, so none contradicts the
    inference. The only skip is idempotency: a pair already carrying a
    ``produced_by`` is never re-stamped. The set stays a subset of the precedence
    list. (Citation kinds are neutral, not suppressed -- see
    ``test_citation_kinds_are_provenance_neutral``.)
    """
    assert set(PRODUCED_INFERENCE_PRECEDENCE) >= PRODUCED_INFERENCE_SUPPRESSED
    assert {"produced_by"} == PRODUCED_INFERENCE_SUPPRESSED


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
