"""Tests for the relationships projection layer."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from trackinizer.conftest import make_conn, new_uuid
from trackinizer.lib.postgres import Conn
from trackinizer.server.projection import (
    fetch_edges,
    project_relationships,
)
from trackinizer.types import inquiries
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
    ArtifactEdge,
    Belief,
    Experiment,
    InquiryEdge,
    Issue,
    IssueEdge,
    Paper,
)


def _edge(
    edge_kind: Edge.Kind,
    *,
    from_id: UUID | None = None,
    from_kind: inquiries.Inquiry.InquiryKind | None = None,
    to_id: UUID | None = None,
    to_kind: inquiries.Inquiry.InquiryKind | None = None,
    priority: int | None = None,
    valence: float | None = None,
    note: str | None = None,
    labels: tuple[str, ...] | None = None,
) -> Any:
    """A full edge row (every selected column present), as the projection sees."""
    return {
        "edge_kind": edge_kind,
        "from_id": from_id,
        "from_kind": from_kind,
        "to_id": to_id,
        "to_kind": to_kind,
        "priority": priority,
        "valence": valence,
        "note": note,
        "labels": labels,
    }


class TestProjection:
    """Every edge is stored child -> parent; forward fields read OUTBOUND
    (to-side), inverse fields read INBOUND (from-side).
    """

    def test_provenance_both_directions(self) -> None:
        # produced_by stored produced(child) -> producer(parent).
        producer_id, child_id = new_uuid(), new_uuid()
        # This vertex is the produced child: its producer parent is OUTBOUND.
        produced = project_relationships(
            Belief(),
            cast(Any, [_edge("produced_by", to_id=producer_id, to_kind="Issue")]),
            cast(Any, []),
        )
        # This vertex is the producer: what it produced is INBOUND.
        producer = project_relationships(
            Issue(),
            cast(Any, []),
            cast(Any, [_edge("produced_by", from_id=child_id, from_kind="Belief")]),
        )
        assert produced.produced_by == (InquiryEdge(id=producer_id, kind="Issue"),)
        assert producer.produces == (InquiryEdge(id=child_id, kind="Belief"),)

    def test_supersedes_both_directions(self) -> None:
        pred_id, succ_id = new_uuid(), new_uuid()
        # Successor (child): its predecessor parent is OUTBOUND.
        succ = project_relationships(
            Belief(),
            cast(Any, [_edge("supersedes", to_id=pred_id, to_kind="Belief")]),
            cast(Any, []),
        )
        # Predecessor (parent): its successor is INBOUND.
        pred = project_relationships(
            Belief(),
            cast(Any, []),
            cast(Any, [_edge("supersedes", from_id=succ_id, from_kind="Belief")]),
        )
        assert succ.supersedes == (InquiryEdge(id=pred_id, kind="Belief"),)
        assert pred.superseded_by == (InquiryEdge(id=succ_id, kind="Belief"),)

    def test_issue_narrows_and_requires(self) -> None:
        broader_id, narrower_id = new_uuid(), new_uuid()
        prereq_id, requirer_id = new_uuid(), new_uuid()
        issue = project_relationships(
            Issue(),
            # OUTBOUND: this issue narrows a broader parent and requires a prereq.
            cast(
                Any,
                [
                    _edge("narrows", to_id=broader_id, to_kind="Issue", priority=10),
                    _edge("requires", to_id=prereq_id, to_kind="Issue"),
                ],
            ),
            # INBOUND: a narrower child decomposes it; a requirer waits on it.
            cast(
                Any,
                [
                    _edge(
                        "narrows", from_id=narrower_id, from_kind="Issue", priority=20
                    ),
                    _edge("requires", from_id=requirer_id, from_kind="Issue"),
                ],
            ),
        )
        assert isinstance(issue, Issue)
        assert issue.narrows == (IssueEdge(id=broader_id, kind="Issue", priority=10),)
        assert issue.narrowed_by == (
            IssueEdge(id=narrower_id, kind="Issue", priority=20),
        )
        assert issue.requires == (IssueEdge(id=prereq_id, kind="Issue"),)
        assert issue.required_by == (IssueEdge(id=requirer_id, kind="Issue"),)

    def test_artifact_cites_outbound(self) -> None:
        """proves/favors stored evidence(child Artifact) -> claim(parent).

        The citing artifact's forward view (the claims it cites) is OUTBOUND,
        carrying the signed valence.
        """
        proved_id, favored_id = new_uuid(), new_uuid()
        paper = project_relationships(
            inquiries.Paper(),
            cast(
                Any,
                [
                    _edge("proves", to_id=proved_id, to_kind="Belief", valence=0.8),
                    _edge(
                        "favors", to_id=favored_id, to_kind="Experiment", valence=-0.5
                    ),
                ],
            ),
            cast(Any, []),
        )
        assert isinstance(paper, inquiries.Paper)
        assert paper.proves == (ArtifactEdge(id=proved_id, kind="Belief", valence=0.8),)
        assert paper.favors == (
            ArtifactEdge(id=favored_id, kind="Experiment", valence=-0.5),
        )

    def test_claim_cited_by_inbound(self) -> None:
        """A Belief/Experiment reads its citing artifacts from INBOUND edges,
        carrying the signed valence; a NULL stored valence reads as 0.5.
        """
        prover_id, favorer_id = new_uuid(), new_uuid()
        belief = project_relationships(
            Belief(),
            cast(Any, []),
            cast(
                Any,
                [
                    _edge(
                        "proves", from_id=prover_id, from_kind="Experiment", valence=0.9
                    ),
                    _edge("favors", from_id=favorer_id, from_kind="Paper"),
                ],
            ),
        )
        assert isinstance(belief, Belief)
        assert belief.proved_by == (
            ArtifactEdge(id=prover_id, kind="Experiment", valence=0.9),
        )
        # NULL valence defaults to 0.5, matching Edge.from_row.
        assert belief.favored_by == (
            ArtifactEdge(id=favorer_id, kind="Paper", valence=0.5),
        )

    def test_experiment_is_a_citation_target(self) -> None:
        prover_id = new_uuid()
        exp = project_relationships(
            Experiment(),
            cast(Any, []),
            cast(
                Any,
                [_edge("proves", from_id=prover_id, from_kind="Paper", valence=0.7)],
            ),
        )
        assert isinstance(exp, Experiment)
        assert exp.proved_by == (ArtifactEdge(id=prover_id, kind="Paper", valence=0.7),)

    def test_edge_note_and_labels_carry(self) -> None:
        peer_id = new_uuid()
        issue = project_relationships(
            Issue(),
            cast(
                Any,
                [
                    _edge(
                        "narrows",
                        to_id=peer_id,
                        to_kind="Issue",
                        priority=5,
                        note="decomposes the auth epic",
                        labels=("auth",),
                    )
                ],
            ),
            cast(Any, []),
        )
        assert isinstance(issue, Issue)
        (ref,) = issue.narrows
        assert ref == IssueEdge(
            id=peer_id,
            kind="Issue",
            priority=5,
            note="decomposes the auth epic",
            labels=("auth",),
        )

    def test_paper_cites_both_directions(self) -> None:
        """cites_paper stored citing(child) -> cited(parent), Paper -> Paper.

        A citing paper's bibliography (the papers it cites) is its OUTBOUND
        to-side ``cites``; the inverse (papers citing it) is its INBOUND
        from-side ``cited_by``. Historical citation carries note/labels but NO
        valence, so the projection is the base InquiryEdge.
        """
        cited_id, citing_id = new_uuid(), new_uuid()
        # The citing paper (child): its cited parent is OUTBOUND.
        citing = project_relationships(
            Paper(),
            cast(
                Any,
                [
                    _edge(
                        "cites_paper",
                        to_id=cited_id,
                        to_kind="Paper",
                        note="see \u00a73",
                        labels=("prior-art",),
                    )
                ],
            ),
            cast(Any, []),
        )
        # The cited paper (parent): its citing child is INBOUND.
        cited = project_relationships(
            Paper(),
            cast(Any, []),
            cast(Any, [_edge("cites_paper", from_id=citing_id, from_kind="Paper")]),
        )
        assert isinstance(citing, Paper)
        assert isinstance(cited, Paper)
        assert citing.cites == (
            InquiryEdge(
                id=cited_id, kind="Paper", note="see \u00a73", labels=("prior-art",)
            ),
        )
        assert cited.cited_by == (InquiryEdge(id=citing_id, kind="Paper"),)

    def test_kind_to_class_resolves_canonical_exported_classes(self) -> None:
        """``KIND_TO_CLASS`` maps every kind to the module's exported class."""
        for kind, cls in KIND_TO_CLASS.items():
            assert cls is getattr(inquiries, kind), (
                f"KIND_TO_CLASS[{kind!r}] is a non-canonical (ghost) class"
            )

    @pytest.mark.asyncio
    async def testfetch_edges_includes_inbound_priority(self) -> None:
        conn = make_conn()
        subject_id = new_uuid()
        await fetch_edges(cast(Conn, conn), subject_id)
        inbound_sql = cast(tuple[str, ...], conn.fetch.call_args_list[1].args)[0]
        assert "priority" in inbound_sql
        assert "valence" in inbound_sql


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
