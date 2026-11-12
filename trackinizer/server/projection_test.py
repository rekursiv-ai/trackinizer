"""Tests for the relationships projection layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast, override
from unittest.mock import Mock

import asyncpg
import pytest

from trackinizer.conftest import RecordingPolicy, make_conn, new_uuid
from trackinizer.lib.postgres import Conn
from trackinizer.server.access_policy import Principal
from trackinizer.server.projection import (
    fetch_edges,
    fetch_edges_bulk,
    materialize,
    project_relationships,
)
from trackinizer.types import inquiries
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


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.access_policy import ResourceKind
    from trackinizer.types.edges import Edge


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
) -> asyncpg.Record:
    """Return a full edge row, every selected column present, as the projection sees."""
    row = {
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
    return cast(asyncpg.Record, row)


class TestProjection:
    """Every edge is stored child -> parent.

    Forward fields read OUTBOUND (to-side), inverse fields read INBOUND (from-side).
    """

    def test_provenance_both_directions(self) -> None:
        # produced_by stored produced(child) -> producer(parent).
        producer_id, child_id = new_uuid(), new_uuid()
        # This vertex is the produced child: its producer parent is OUTBOUND.
        produced = project_relationships(
            Belief(),
            [_edge("produced_by", to_id=producer_id, to_kind="Issue")],
            [],
        )
        # This vertex is the producer: what it produced is INBOUND.
        producer = project_relationships(
            Issue(),
            [],
            [_edge("produced_by", from_id=child_id, from_kind="Belief")],
        )
        assert produced.produced_by == (InquiryEdge(id=producer_id, kind="Issue"),)
        assert producer.produces == (InquiryEdge(id=child_id, kind="Belief"),)

    def test_supersedes_both_directions(self) -> None:
        pred_id, succ_id = new_uuid(), new_uuid()
        # Successor (child): its predecessor parent is OUTBOUND.
        succ = project_relationships(
            Belief(),
            [_edge("supersedes", to_id=pred_id, to_kind="Belief")],
            [],
        )
        # Predecessor (parent): its successor is INBOUND.
        pred = project_relationships(
            Belief(),
            [],
            [_edge("supersedes", from_id=succ_id, from_kind="Belief")],
        )
        assert succ.supersedes == (InquiryEdge(id=pred_id, kind="Belief"),)
        assert pred.superseded_by == (InquiryEdge(id=succ_id, kind="Belief"),)

    def test_issue_narrows_and_requires(self) -> None:
        broader_id, narrower_id = new_uuid(), new_uuid()
        prereq_id, requirer_id = new_uuid(), new_uuid()
        issue = project_relationships(
            Issue(),
            # OUTBOUND: this issue narrows a broader parent and requires a prereq.
            [
                _edge("narrows", to_id=broader_id, to_kind="Issue", priority=10),
                _edge("requires", to_id=prereq_id, to_kind="Issue"),
            ],
            # INBOUND: a narrower child decomposes it; a requirer waits on it.
            [
                _edge(
                    "narrows",
                    from_id=narrower_id,
                    from_kind="Issue",
                    priority=20,
                ),
                _edge("requires", from_id=requirer_id, from_kind="Issue"),
            ],
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
            [
                _edge("proves", to_id=proved_id, to_kind="Belief", valence=0.8),
                _edge(
                    "favors",
                    to_id=favored_id,
                    to_kind="Experiment",
                    valence=-0.5,
                ),
            ],
            [],
        )
        assert isinstance(paper, inquiries.Paper)
        assert paper.proves == (ArtifactEdge(id=proved_id, kind="Belief", valence=0.8),)
        assert paper.favors == (
            ArtifactEdge(id=favored_id, kind="Experiment", valence=-0.5),
        )

    def test_claim_cited_by_inbound(self) -> None:
        """A Belief/Experiment reads its citing artifacts from INBOUND edges.

        Carrying the signed valence; a NULL stored valence reads as 0.5.
        """
        prover_id, favorer_id = new_uuid(), new_uuid()
        belief = project_relationships(
            Belief(),
            [],
            [
                _edge(
                    "proves",
                    from_id=prover_id,
                    from_kind="Experiment",
                    valence=0.9,
                ),
                _edge("favors", from_id=favorer_id, from_kind="Paper"),
            ],
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
            [],
            [_edge("proves", from_id=prover_id, from_kind="Paper", valence=0.7)],
        )
        assert isinstance(exp, Experiment)
        assert exp.proved_by == (ArtifactEdge(id=prover_id, kind="Paper", valence=0.7),)

    def test_edge_note_and_labels_carry(self) -> None:
        peer_id = new_uuid()
        issue = project_relationships(
            Issue(),
            [
                _edge(
                    "narrows",
                    to_id=peer_id,
                    to_kind="Issue",
                    priority=5,
                    note="decomposes the auth epic",
                    labels=("auth",),
                ),
            ],
            [],
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
            [
                _edge(
                    "cites_paper",
                    to_id=cited_id,
                    to_kind="Paper",
                    note="see \u00a73",
                    labels=("prior-art",),
                ),
            ],
            [],
        )
        # The cited paper (parent): its citing child is INBOUND.
        cited = project_relationships(
            Paper(),
            [],
            [_edge("cites_paper", from_id=citing_id, from_kind="Paper")],
        )
        assert isinstance(citing, Paper)
        assert isinstance(cited, Paper)
        assert citing.cites == (
            InquiryEdge(
                id=cited_id,
                kind="Paper",
                note="see \u00a73",
                labels=("prior-art",),
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
        inbound_sql = conn.fetch.call_args_list[1].args[0]
        assert isinstance(inbound_sql, str)
        assert "priority" in inbound_sql
        assert "valence" in inbound_sql


def _issue_row(row_id: UUID) -> asyncpg.Record:
    """Return the base columns of one Issue row, as a list read selects them."""
    at = datetime(2026, 8, 19, tzinfo=UTC)
    row = {
        "kind": "Issue",
        "id": row_id,
        "seq": 3,
        "owner": "ada@example.com",
        "account": "ada@example.com",
        "status": "active",
        "title": "a title",
        "description": "",
        "labels": None,
        "subscribers": None,
        "created": at,
        "modified": at,
    }
    return cast(asyncpg.Record, row)


class TestFetchEdgesBulk:
    @pytest.mark.asyncio
    async def test_no_subjects_asks_nothing(self) -> None:
        conn = make_conn()

        assert await fetch_edges_bulk(cast(Conn, conn), []) == ({}, {})
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_each_edge_lands_in_its_subjects_bucket_and_every_subject_has_one(
        self,
    ) -> None:
        one, two, quiet, peer = new_uuid(), new_uuid(), new_uuid(), new_uuid()
        out_one = _edge("narrows", from_id=one, to_id=peer, to_kind="Issue")
        out_two = _edge("requires", from_id=two, to_id=peer, to_kind="Issue")
        in_one = _edge("narrows", from_id=peer, from_kind="Issue", to_id=one)
        conn = make_conn()
        conn.fetch.side_effect = [[out_one, out_two], [in_one]]

        outbound, inbound = await fetch_edges_bulk(
            cast(Conn, conn),
            [one, two, quiet],
        )

        assert outbound == {one: [out_one], two: [out_two], quiet: []}
        assert inbound == {one: [in_one], two: [], quiet: []}
        out_call, in_call = conn.fetch.await_args_list
        assert out_call.args[1:] == ([one, two, quiet],)
        assert in_call.args[1:] == ([one, two, quiet],)
        assert "from_id = ANY($1::uuid[])" in str(out_call.args[0])
        assert "to_id = ANY($1::uuid[])" in str(in_call.args[0])
        assert "AND (" not in str(out_call.args[0])

    @pytest.mark.asyncio
    async def test_a_reader_sees_only_peers_the_policy_lets_them_read(self) -> None:
        conn = make_conn()
        principal = Principal(email="ada@example.com", role="writer")

        await fetch_edges_bulk(
            cast(Conn, conn),
            [new_uuid()],
            policy=_Restricted(),
            principal=principal,
        )

        out_sql, in_sql = (str(c.args[0]) for c in conn.fetch.await_args_list)
        assert "readable_inquiry.id = edges.to_id AND ((FALSE) AND" in out_sql
        assert "readable_inquiry.id = edges.from_id AND ((FALSE) AND" in in_sql


def test_materialize_projects_the_rows_own_edges_onto_its_kind() -> None:
    row_id, broader, other = new_uuid(), new_uuid(), new_uuid()
    outbound = {
        row_id: [_edge("narrows", to_id=broader, to_kind="Issue", priority=2)],
        other: [_edge("requires", to_id=new_uuid(), to_kind="Issue")],
    }

    issue = materialize(_issue_row(row_id), outbound, {})

    assert isinstance(issue, Issue)
    assert (issue.id, issue.title) == (row_id, "a title")
    assert issue.narrows == (IssueEdge(id=broader, kind="Issue", priority=2),)
    assert issue.requires == ()
    assert issue.narrowed_by == ()


class _Restricted(RecordingPolicy):
    """A policy whose read filter hides every row."""

    @override
    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        del kind, alias, params, principal
        return "FALSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_bulk_edges_keep_empty_subjects_and_bucket_both_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    restricted: bool,
) -> None:
    conn = make_conn()
    first, second, peer = new_uuid(), new_uuid(), new_uuid()
    outgoing = _edge("requires", from_id=second, to_id=peer, to_kind="Issue")
    incoming = _edge("requires", from_id=peer, from_kind="Issue", to_id=first)
    assert await fetch_edges_bulk(cast(Conn, conn), []) == ({}, {})
    conn.fetch.assert_not_awaited()
    conn.fetch.side_effect = [[outgoing], [incoming]]
    policy = RecordingPolicy()
    monkeypatch.setattr(
        RecordingPolicy,
        "read_filter",
        Mock(return_value="FALSE" if restricted else "TRUE"),
    )
    outbound, inbound = await fetch_edges_bulk(
        cast(Conn, conn),
        [first, second],
        policy=policy,
        principal=Principal(email="reader@example.com", role="viewer"),
    )
    assert outbound == {first: [], second: [outgoing]}
    assert inbound == {first: [incoming], second: []}
    # Each read binds the ids, then the chat filing test's reader: the email first.
    assert [entry.args[1:3] for entry in conn.fetch.await_args_list] == [
        ([first, second], "reader@example.com"),
        ([first, second], "reader@example.com"),
    ]
    # The chat a peer was filed from gates it under every policy.
    for sent in conn.fetch.await_args_list:
        assert "AND (EXISTS" in str(sent.args[0])
        assert ("((FALSE) AND (readable_inquiry." in str(sent.args[0])) is restricted


@pytest.mark.parametrize("has_edges", [False, True])
def test_materialize_dispatches_the_row_kind_and_uses_its_own_buckets(
    monkeypatch: pytest.MonkeyPatch,
    has_edges: bool,
) -> None:
    subject, peer, other = new_uuid(), new_uuid(), new_uuid()
    row = cast(asyncpg.Record, {"id": subject, "kind": "Issue"})
    read = Mock(return_value=Issue(id=subject, title="Persisted title"))
    monkeypatch.setattr(Issue, "from_row", read)
    edges = [_edge("requires", to_id=peer, to_kind="Issue", priority=10)]
    projected = materialize(row, {subject if has_edges else other: edges}, {})
    assert isinstance(projected, Issue)
    assert (projected.id, projected.title) == (subject, "Persisted title")
    assert projected.requires == (
        (IssueEdge(id=peer, kind="Issue", priority=10),) if has_edges else ()
    )
    assert projected.required_by == ()
    read.assert_called_once_with(row)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
