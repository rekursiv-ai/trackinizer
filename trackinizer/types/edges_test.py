from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast, get_args

import re
import runpy
import types
import uuid

import pytest

from trackinizer.types import edges
from trackinizer.types.edges import (
    EDGE_POLICIES,
    PRODUCED_INFERENCE_NEUTRAL,
    PRODUCED_INFERENCE_PRECEDENCE,
    PRODUCED_INFERENCE_SUPPRESSED,
    Edge,
    edge_annotations,
    edge_labels,
    edge_topology,
    kind_group_members,
)
from trackinizer.types.inquiries import Inquiry


if TYPE_CHECKING:
    from collections.abc import Mapping


def _row(**over: object) -> dict[str, object]:
    return {
        "from_id": uuid.uuid4(),
        "from_kind": "Issue",
        "to_id": uuid.uuid4(),
        "to_kind": "Issue",
        "edge_kind": "requires",
    } | over


def test_from_row_reads_missing_annotations_as_null() -> None:
    row = _row()
    edge = Edge.from_row(row)
    assert (edge.from_id, edge.to_id) == (row["from_id"], row["to_id"])
    assert (edge.from_kind, edge.to_kind, edge.edge_kind) == (
        "Issue",
        "Issue",
        "requires",
    )
    assert (edge.priority, edge.note, edge.valence, edge.labels) == (
        None,
        None,
        None,
        None,
    )


def test_from_row_reads_annotations() -> None:
    edge = Edge.from_row(
        _row(priority=2, note="why", valence=0.5, labels=["a", "b"]),
    )
    assert (edge.priority, edge.note, edge.valence, edge.labels) == (
        2,
        "why",
        0.5,
        ("a", "b"),
    )


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"priority": "1"}, "priority"),
        ({"note": 3}, "note"),
        ({"valence": 1}, "valence"),
    ],
)
def test_from_row_refuses_mistyped_annotations(
    over: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"Expected {message} is None"):
        Edge.from_row(_row(**over))


def test_kind_groups_resolve_to_concrete_kinds() -> None:
    assert kind_group_members("issue") == ("Issue",)
    assert kind_group_members("inquiry") == get_args(
        cast(object, Inquiry.InquiryKind.__value__),
    )
    assert "Paper" in kind_group_members("evidence")
    assert "Issue" not in kind_group_members("evidence")
    assert kind_group_members("belief") == ("Belief",)
    assert kind_group_members("experiment") == ("Experiment",)
    assert kind_group_members("claimable") == ("Belief", "Experiment")
    assert kind_group_members("paper") == ("Paper",)


def test_every_kind_has_a_policy() -> None:
    assert set(EDGE_POLICIES) == set(get_args(cast(object, Edge.Kind.__value__)))


def test_labels_come_from_the_registry() -> None:
    labels = edge_labels()
    assert labels["narrows"] == {"forward": "narrows", "inverse": "narrowed_by"}
    assert labels["cites_paper"] == {"forward": "cites", "inverse": "cited_by"}


def test_annotations_follow_column_applicability() -> None:
    annotations = edge_annotations()
    assert annotations["narrows"] == ["priority", "note", "labels"]
    assert annotations["proves"] == ["note", "valence", "labels"]
    assert annotations["supersedes"] == ["note", "labels"]


def test_import_refuses_a_kind_without_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kind missing from ``EDGE_POLICIES`` fails the module import itself."""
    proxy = MappingProxyType

    def without_cites_paper(
        mapping: Mapping[str, object],
    ) -> MappingProxyType[str, object]:
        if "cites_paper" in mapping:
            mapping = {k: v for k, v in mapping.items() if k != "cites_paper"}
        return proxy(mapping)

    monkeypatch.setattr(types, "MappingProxyType", without_cites_paper)
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "EDGE_POLICIES missing an entry for ['cites_paper']; every Edge.Kind "
            "needs a policy (labels + endpoints + behavior).",
        ),
    ):
        runpy.run_path(str(Path(edges.__file__)))


def test_edge_topology_covers_every_edge_kind() -> None:
    """Every ``Edge.Kind`` literal has a topology entry (no kind unmapped)."""
    assert set(edge_topology()) == set(EDGE_POLICIES)


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
    all_kinds = set(EDGE_POLICIES)
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
    assert set(policies) == set(EDGE_POLICIES)
    assert policies["cites_paper"].enforces_acyclicity is False
    for kind, policy in policies.items():
        if kind != "cites_paper":
            assert policy.enforces_acyclicity is True, (
                f"{kind} unexpectedly acyclicity-exempt"
            )


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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
