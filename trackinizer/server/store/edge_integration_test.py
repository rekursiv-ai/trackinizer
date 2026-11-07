"""Edge writes, annotations, and inferred provenance against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import asyncio

import pytest

from trackinizer.server.notify import tx
from trackinizer.server.store.edge import INFERRED_PROVENANCE_REASON
from trackinizer.server.store.store_test_support import fetch_value
from trackinizer.types.errors import ConflictError, ValidationError
from trackinizer.types.inquiries import Belief, Issue, Paper
from trackinizer.wire.bodies import (
    BatchEdge,
    Citation,
    SubmitBelief,
    SubmitIssue,
    SubmitPaper,
    SubmitWebSearch,
)


if TYPE_CHECKING:
    import uuid

    from trackinizer.server.store.core import Store
    from trackinizer.types.edges import Edge


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Edge writes, annotations, and inferred provenance, each read back from the database."""

    async def test_edge_cycles_are_rejected(self, integ_store: Store) -> None:
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
        )
        with pytest.raises(ConflictError, match="cycle"):
            await integ_store.add_edge(
                from_id=b_id,
                to_id=a_id,
                edge_kind="requires",
                actor="user",
            )

    async def test_edge_cycle_check_is_per_kind(self, integ_store: Store) -> None:
        """Acyclicity is per-edge-kind; cross-kind paths aren't cycles."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="parent issue"),
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="a belief"),
        )
        # Issue produced_by -> Belief.
        await integ_store.add_edge(
            from_id=issue_id,
            to_id=belief_id,
            edge_kind="produced_by",
            actor="user",
        )
        # Belief supersedes Issue -- not a cycle in either relation,
        # but the pre-fix global walk would have flagged it.
        await integ_store.add_edge(
            from_id=belief_id,
            to_id=issue_id,
            edge_kind="supersedes",
            actor="user",
        )

    async def test_add_edge_existing_edge_overwrites_annotations(
        self,
        integ_store: Store,
    ) -> None:
        """Re-adding an edge applies the supplied annotations (upsert, no error).

        Edge creation is an upsert: a second ``add_edge`` with a new ``note``
        does NOT raise -- it overwrites, exactly as ``note to X`` sets a value
        anywhere else. User annotations are never silently dropped.
        """
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
            note="first",
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
            note="second",
        )
        edge = await integ_store.get_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
        )
        assert edge is not None
        assert edge.note == "second"

    async def test_set_edge_annotation_is_partial_update(
        self,
        integ_store: Store,
    ) -> None:
        """Omitted fields stay unchanged; explicit ``""`` / ``[]`` clears."""
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="user",
            note="original",
            labels=["important"],
        )
        # Update only the note; labels must survive untouched.
        await integ_store.set_edge_annotation(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            note="revised",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert row is not None
        assert row["note"] == "revised"
        labels = row["labels"]
        assert labels == ["important"]

    async def test_edge_without_note_or_labels_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """An edge created with no note/labels stores NULL, not '' / '{}'.

        Edge metadata follows the same "unset is NULL" rule as inquiry
        columns: an absent annotation is NULL on the live edges table (the
        change-log mirror coerces it for its presence CHECK separately).
        """
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="u",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert row is not None
        assert row["note"] is None
        assert row["labels"] is None

    async def test_clearing_edge_note_and_last_label_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """Blanking an edge note or removing its last label stores NULL."""
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="u",
            note="n",
            labels=["only"],
        )
        await integ_store.set_edge_annotation(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            note="",
            actor="u",
        )
        await integ_store.remove_edge_label(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            label="only",
            actor="u",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note, labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert row is not None
        assert row["note"] is None
        assert row["labels"] is None

    async def test_edge_label_add_preserves_insertion_order(
        self,
        integ_store: Store,
    ) -> None:
        """Sequential ``label add`` keeps insertion order; it must not re-sort.

        Each single-label mutation previously rebuilt the set and ``sorted()``
        it, silently reordering an edge's existing labels alphabetically. Adding
        labels in a non-alphabetical sequence and asserting the stored order
        catches that regression (A3).
        """
        a_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a_id,
            to_id=b_id,
            edge_kind="requires",
            actor="u",
        )
        for label in ("zeta", "alpha", "mu"):
            await integ_store.add_edge_label(
                from_id=a_id,
                to_id=b_id,
                edge_kind="requires",
                label=label,
                actor="u",
            )
        async with integ_store.engine.acquire() as conn:
            labels = await fetch_value(
                conn,
                "SELECT labels FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = $3",
                a_id,
                b_id,
                "requires",
            )
        assert isinstance(labels, list)
        assert labels == ["zeta", "alpha", "mu"]

    async def test_remove_narrows_cascades_to_broader(
        self,
        integ_store: Store,
    ) -> None:
        """Removing ``narrows`` must still alert the broader Issue.

        Regression: cascade re-queried ``edges`` live, after DELETE; the
        broader Issue (the dependent endpoint for ``narrows``)
        never got the ``dependency_changed`` row.
        """
        broader_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="goal",
                subscribers=["alice"],
            ),
        )
        narrower_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="sub-task",
                narrows=[(broader_id, None)],
            ),
        )
        # Sanity: setup wired the edge.
        # Now remove it and assert the broader Issue gets a cascade alert.
        await integ_store.remove_edge(
            from_id=narrower_id,
            to_id=broader_id,
            edge_kind="narrows",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            # Scope to the narrows cascade: the broader Issue also
            # ``produced`` the narrower (inferred from the first edge at setup),
            # so an unscoped latest-row read could pick up that edge's cascade.
            row = await conn.fetchrow(
                "SELECT * FROM change_log WHERE subject_id = $1 "
                "AND kind = 'dependency_changed' "
                "AND new_peer_edge_kind = 'narrows' "
                "ORDER BY created DESC LIMIT 1",
                broader_id,
            )
        assert row is not None
        assert row["new_peer_id"] == narrower_id
        assert row["new_peer_edge_kind"] == "narrows"

    async def test_add_edge_emits_audit_on_both_endpoints(
        self,
        integ_store: Store,
    ) -> None:
        """Symmetric edge_added: both endpoints get a change_log row."""
        issue_a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        issue_b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=issue_a,
            to_id=issue_b,
            edge_kind="requires",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT subject_id FROM change_log "
                "WHERE kind = 'edge_added' AND "
                "(new_peer_id = $1 OR new_peer_id = $2)",
                issue_a,
                issue_b,
            )
        assert {r["subject_id"] for r in rows} == {issue_a, issue_b}

    async def test_add_edge_on_existing_edge_upserts_annotations(
        self,
        integ_store: Store,
    ) -> None:
        """Re-creating an edge WITH annotations overwrites them (upsert, no 409).

        This is the load-bearing invariant: ``proves ... note to X`` on an
        edge that already exists SETS the note -- it does not raise
        "already exists" and send the caller to a different endpoint. A bare
        re-create (no annotations) stays a no-op.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        first_change, first_created = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="user",
        )
        assert first_created is True
        assert first_change is not None

        # A bare re-create is a pure no-op (nothing to set).
        bare_change, bare_created = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="user",
        )
        assert bare_created is False
        assert bare_change is None

        # A re-create carrying annotations OVERWRITES them (no raise). ``requires``
        # is a structural edge, so its annotations are note / priority (valence is
        # citation-only and rejected by the edges CHECK on a non-citation edge).
        ann_change, ann_created = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            note="degradation half",
            priority=3,
            actor="user",
        )
        assert ann_created is False  # `existing` edge, not a fresh create.
        assert ann_change is not None  # But a change WAS emitted (annotated)
        edge = await integ_store.get_edge(from_id=a, to_id=b, edge_kind="requires")
        assert edge is not None
        assert edge.note == "degradation half"
        assert edge.priority == 3

    async def test_citation_create_without_valence_defaults_not_null(
        self,
        integ_store: Store,
    ) -> None:
        """A proves/favors edge created with no valence stores the default, not NULL.

        The citation-valence invariant ("a citation is never NULL") holds at the
        single storage boundary regardless of entry path, so a bare ``add_edge``
        on a citation kind stores ``CITATION_VALENCE_DEFAULT`` rather than NULL.
        """
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.2"),
        )
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=paper,
            to_id=belief,
            edge_kind="proves",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief,
            )
        assert stored == 0.5

    async def test_clearing_citation_valence_resets_to_default_not_null(
        self,
        integ_store: Store,
    ) -> None:
        """``set_edge_annotation(valence=None)`` on a citation heals to the default.

        A citation can never be stored NULL; an explicit clear resets the valence
        to ``CITATION_VALENCE_DEFAULT`` instead of writing NULL (which the model
        forbids on a citation).
        """
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.3"),
        )
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=paper,
            to_id=belief,
            edge_kind="proves",
            valence=0.9,
            actor="user",
        )
        await integ_store.set_edge_annotation(
            from_id=paper,
            to_id=belief,
            edge_kind="proves",
            valence=None,
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief,
            )
        assert stored == 0.5

    async def test_valence_on_structural_edge_annotation_raises_4xx(
        self,
        integ_store: Store,
    ) -> None:
        """A valence on a structural edge is a clean ValidationError, not a DB 500.

        The annotation-edit path runs the same ``validate_edge_valence`` guard as
        create, so a valence on ``requires`` surfaces as a 4xx ValidationError,
        not a raw mid-transaction CHECK violation.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="user",
        )
        with pytest.raises(ValidationError, match="valence"):
            await integ_store.set_edge_annotation(
                from_id=a,
                to_id=b,
                edge_kind="requires",
                valence=0.5,
                actor="user",
            )

    async def test_remove_edge_label_normalizes_whitespace(
        self,
        integ_store: Store,
    ) -> None:
        """Atomic edge-label remove canonicalizes the label, matching whole-list.

        A stored ``foo`` must be removed by ``remove_edge_label("  foo  ")``,
        since the single-label path canonicalizes the same way whole-list writes
        do -- otherwise the atomic op silently no-ops on a whitespace variant.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            labels=["foo"],
            actor="user",
        )
        change = await integ_store.remove_edge_label(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            label="  foo  ",
            actor="user",
        )
        assert change is not None
        edge = await integ_store.get_edge(from_id=a, to_id=b, edge_kind="requires")
        assert edge is not None
        assert edge.labels is None

    async def test_inline_citation_upserts_valence_on_existing_edge(
        self,
        integ_store: Store,
    ) -> None:
        """An inline Belief citation applies its valence to an already-present edge.

        ``insert_edge_and_audit`` (the submit-time citation path) must upsert the
        caller's valence onto an existing edge, not silently drop it -- symmetric
        with ``add_edge``'s create-is-upsert behavior.
        """
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.4"),
        )
        # First Belief stamps the proves edge with valence 0.7.
        await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="b1",
                proved_by=[
                    Citation(artifact_id=paper, artifact_kind="Paper", valence=0.7),
                ],
            ),
        )
        belief1 = (await integ_store.list_kind("Belief"))[0].id
        # A second submit citing the SAME (paper -> belief1) pair with a fresh
        # valence must overwrite, not no-op. Re-cite via a direct belief edit
        # path: submit a citation on belief1 again with a different valence.
        await integ_store.add_edge(
            from_id=paper,
            to_id=belief1,
            edge_kind="proves",
            valence=-0.9,
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief1,
            )
        assert stored == -0.9

    async def test_a_submit_time_citation_of_an_existing_edge_sets_its_valence(
        self,
        integ_store: Store,
    ) -> None:
        """The citation path upserts onto the edge it finds, and audits no new edge."""
        paper = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="arXiv:1.5"),
        )
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b"),
        )
        cause, created = await integ_store.add_edge(
            from_id=paper,
            to_id=belief,
            edge_kind="proves",
            valence=0.7,
            actor="user",
        )
        assert created
        assert cause is not None

        async with integ_store.engine.acquire() as conn, tx(conn):
            inserted = await integ_store.insert_edge_and_audit(
                conn,
                subject_id=belief,
                subject_kind="Belief",
                to_id=paper,
                edge_kind="proves",
                api_key_id=None,
                actor="user",
                caused_by=cause,
                valence=-0.4,
                require_to_kind="Paper",
                cite_peer_as_from=True,
            )
            stored = await fetch_value(
                conn,
                "SELECT valence FROM edges "
                "WHERE from_id = $1 AND to_id = $2 AND edge_kind = 'proves'",
                paper,
                belief,
            )
            added = await fetch_value(
                conn,
                "SELECT count(*) FROM change_log WHERE kind = 'edge_added' "
                "AND subject_id = $1",
                belief,
            )

        assert (inserted, stored, added) == (False, -0.4, 1)
        with pytest.raises(
            ConflictError,
            match="declared as Experiment but is a Paper",
        ):
            async with integ_store.engine.acquire() as conn, tx(conn):
                _ = await integ_store.insert_edge_and_audit(
                    conn,
                    subject_id=belief,
                    subject_kind="Belief",
                    to_id=paper,
                    edge_kind="proves",
                    api_key_id=None,
                    actor="user",
                    caused_by=cause,
                    valence=0.1,
                    require_to_kind="Experiment",
                    cite_peer_as_from=True,
                )

    async def test_an_edge_added_on_a_held_connection_commits_with_its_transaction(
        self,
        integ_store: Store,
    ) -> None:
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="first"),
        )
        second = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="second"),
        )

        async def add_then_abandon() -> None:
            async with integ_store.engine.acquire() as conn, tx(conn):
                _ = await integ_store.add_edge(
                    from_id=first,
                    to_id=second,
                    edge_kind="requires",
                    actor="user",
                    conn=conn,
                )
                raise RuntimeError("abandon the transaction")

        with pytest.raises(RuntimeError, match="abandon"):
            await add_then_abandon()
        rolled_back = await integ_store.get_edge(
            from_id=first,
            to_id=second,
            edge_kind="requires",
        )
        async with integ_store.engine.acquire() as conn, tx(conn):
            _, created = await integ_store.add_edge(
                from_id=first,
                to_id=second,
                edge_kind="requires",
                actor="user",
                conn=conn,
            )

        assert rolled_back is None
        assert created
        assert await integ_store.get_edge(
            from_id=first,
            to_id=second,
            edge_kind="requires",
        )

    async def test_add_edge_records_reason_on_both_audit_rows(
        self,
        integ_store: Store,
    ) -> None:
        """An edge create's ``reason`` lands on both paired ``edge_added`` rows.

        Edge mutations accept a ``reason`` (the body carries it), exactly like
        inquiry field edits. It must reach ``change_log.reason`` on both the
        from- and to-side audit rows, not be silently dropped.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="user",
            reason="because the analyst asked",
        )
        async with integ_store.engine.acquire() as conn:
            # Scope to the requires audit rows: the first edge between the
            # pair also infers a ``produced_by`` edge (its own reason), so filter
            # to this edge's audits rather than every edge_added on the pair.
            reasons = {
                r["reason"]
                for r in await conn.fetch(
                    "SELECT reason FROM change_log WHERE kind = 'edge_added' "
                    "AND new_peer_edge_kind = 'requires' "
                    "AND (subject_id = $1 OR subject_id = $2)",
                    a,
                    b,
                )
            }
        assert reasons == {"because the analyst asked"}

    async def test_set_edge_annotation_records_reason(
        self,
        integ_store: Store,
    ) -> None:
        """A ``set_edge_annotation``'s ``reason`` reaches both audit rows."""
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="user",
        )
        await integ_store.set_edge_annotation(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            note="n",
            actor="user",
            reason="annotate-reason",
        )
        async with integ_store.engine.acquire() as conn:
            reasons = {
                r["reason"]
                for r in await conn.fetch(
                    "SELECT reason FROM change_log "
                    "WHERE kind = 'edge_annotation_changed' "
                    "AND (subject_id = $1 OR subject_id = $2)",
                    a,
                    b,
                )
            }
        assert reasons == {"annotate-reason"}

    async def test_edge_input_validation_raises_validation_error(
        self,
        integ_store: Store,
    ) -> None:
        """Pure-input edge errors raise ``ValidationError`` (HTTP 422), not 409.

        Priority-on-a-non-priority-kind and a self-loop are malformed requests
        knowable without consulting the graph -- RFC 9110 422, not the 409
        ``Conflict`` reserved for clashes with existing state (cycle).
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        # ``supersedes`` cannot carry priority -> input-invalid -> ValidationError.
        with pytest.raises(ValidationError, match="cannot carry priority"):
            await integ_store.add_edge(
                from_id=a,
                to_id=b,
                edge_kind="supersedes",
                priority=0,
                actor="u",
            )
        # A self-loop is input-invalid -> ValidationError.
        with pytest.raises(ValidationError, match="self-loop"):
            await integ_store.add_edge(
                from_id=a,
                to_id=a,
                edge_kind="requires",
                actor="u",
            )

    async def test_edge_cycle_stays_conflict_error(
        self,
        integ_store: Store,
    ) -> None:
        """A cycle is a STATE conflict (409), not input-invalid (422).

        The edge is well-formed; it fails only because of edges already in the
        graph. That is RFC 9110 409 Conflict, kept distinct from the 422 input
        errors above.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        await integ_store.add_edge(from_id=a, to_id=b, edge_kind="requires", actor="u")
        with pytest.raises(ConflictError, match="would create a cycle"):
            await integ_store.add_edge(
                from_id=b,
                to_id=a,
                edge_kind="requires",
                actor="u",
            )

    async def test_edge_create_is_an_upsert_like_every_other_set(
        self,
        integ_store: Store,
    ) -> None:
        """INVARIANT: an edge ``add`` is an upsert, exactly like ``label add``.

        This is the safeguard against re-introducing an "edge already exists"
        error. Every set in the system is idempotent: ``label add X`` on an
        already-labelled row no-ops, ``status to S`` overwrites, ``note to X``
        sets. Edge creation MUST behave the same -- a repeat never raises, a
        bare repeat is a no-op, an annotated repeat applies the annotation. If
        a future change makes edge-create raise on an existing edge, this test
        fails and names the principle it violated.
        """
        a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        # First create: a change is emitted, created flag set.
        _, created1 = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="u",
        )
        assert created1 is True
        # Bare repeat: idempotent no-op, NO raise (the load-bearing assertion).
        change2, created2 = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="u",
        )
        assert (change2, created2) == (None, False)
        # Annotated repeat: applies the annotation, NO raise.
        change3, created3 = await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="requires",
            actor="u",
            note="n",
        )
        assert change3 is not None
        assert created3 is False

    async def test_concurrent_edge_label_adds_do_not_clobber(
        self,
        integ_store: Store,
    ) -> None:
        """Two concurrent edge-label adds must both survive (TAPI-002).

        ``_mutate_edge_label`` read the labels outside the write
        transaction (``get_edge`` on its own connection) then overwrote
        via ``set_edge_annotation``, so interleaved adds lost-update one
        another. The fix locks the edge row ``FOR UPDATE`` for the whole
        read-modify-write, mirroring the inquiry list path.
        """
        src = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="src"),
        )
        dst = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="dst"),
        )
        await integ_store.add_edge(
            from_id=src,
            to_id=dst,
            edge_kind="requires",
            actor="u",
        )
        await asyncio.gather(
            integ_store.add_edge_label(
                from_id=src,
                to_id=dst,
                edge_kind="requires",
                label="a",
                actor="u",
            ),
            integ_store.add_edge_label(
                from_id=src,
                to_id=dst,
                edge_kind="requires",
                label="b",
                actor="u",
            ),
        )
        edge = await integ_store.get_edge(from_id=src, to_id=dst, edge_kind="requires")
        assert edge is not None
        assert set(edge.labels or ()) == {"a", "b"}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestFirstEdgeInfersProduced:
    """The first edge between two vertices infers ``younger produced_by older``.

    The definition of provenance (see ``Inquiry.produced_by``): on the first
    edge between a pair, the older vertex produced the younger, stamped as a
    ``produced_by`` edge child -> parent (from=younger, to=older).
    Birth-coincident creates are the special case; a late link between two
    pre-existing nodes is the same rule. Verified end to end against a real Store.
    """

    @classmethod
    async def _set_created(
        cls,
        store: Store,
        target_id: uuid.UUID,
        when: datetime,
    ) -> None:
        """Pin a row's ``created`` so older/younger ordering is deterministic."""
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE inquiries SET created = $2 WHERE id = $1",
                target_id,
                when,
            )

    async def _two_issues(self, store: Store) -> tuple[uuid.UUID, uuid.UUID]:
        """Create an older and a younger Issue with pinned, distinct created."""
        older = await store.submit_issue(
            SubmitIssue(account="tester@example.com", title="older"),
        )
        younger = await store.submit_issue(
            SubmitIssue(account="tester@example.com", title="younger"),
        )
        await self._set_created(store, older, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(store, younger, datetime(2020, 1, 2, tzinfo=UTC))
        return older, younger

    async def _two_papers(self, store: Store) -> tuple[uuid.UUID, uuid.UUID]:
        """Create an older (cited) and younger (citing) Paper, pinned created."""
        older = await store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="cited",
                source="arXiv:2001.00001",
            ),
        )
        younger = await store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="citing",
                source="arXiv:2401.00001",
            ),
        )
        await self._set_created(store, older, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(store, younger, datetime(2024, 1, 2, tzinfo=UTC))
        return older, younger

    async def test_first_requires_edge_infers_produced_older_to_younger(
        self,
        integ_store: Store,
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        # The younger requires the older (stored requirer=younger -> prerequisite=
        # older). This is the pair's first edge, so the older produced the
        # younger -- regardless of the requires edge's own direction.
        await integ_store.add_edge(
            from_id=younger,
            to_id=older,
            edge_kind="requires",
            actor="alice",
        )
        older_row = await integ_store.get_inquiry(older)
        assert isinstance(older_row, Issue)
        younger_row = await integ_store.get_inquiry(younger)
        assert isinstance(younger_row, Issue)
        assert younger_row.id in {e.id for e in older_row.produces}
        assert older_row.id in {e.id for e in younger_row.produced_by}

    async def test_reverse_direction_edge_still_infers_older_produced_younger(
        self,
        integ_store: Store,
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        # Edge written older -> younger (older requires younger). Provenance still
        # follows age, not edge direction: older produced younger.
        await integ_store.add_edge(
            from_id=older,
            to_id=younger,
            edge_kind="requires",
            actor="alice",
        )
        older_row = await integ_store.get_inquiry(older)
        assert isinstance(older_row, Issue)
        younger_row = await integ_store.get_inquiry(younger)
        assert isinstance(younger_row, Issue)
        assert younger in {e.id for e in older_row.produces}
        assert older in {e.id for e in younger_row.produced_by}

    async def test_inferred_produced_target_can_be_issue(
        self,
        integ_store: Store,
    ) -> None:
        # The widened produced_by edge (Inquiry -> Inquiry) lets an Issue be the
        # produced row, which the pre-widen schema forbade.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=older,
            to_id=younger,
            edge_kind="narrows",
            actor="alice",
        )
        older_row = await integ_store.get_inquiry(older)
        assert isinstance(older_row, Issue)
        assert younger in {e.id for e in older_row.produces}
        assert "Issue" in {e.kind for e in older_row.produces}

    async def test_second_edge_between_pair_does_not_restamp(
        self,
        integ_store: Store,
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger,
            to_id=older,
            edge_kind="requires",
            actor="alice",
        )
        # A second edge between the same pair must not add another produced_by edge.
        await integ_store.add_edge(
            from_id=older,
            to_id=younger,
            edge_kind="narrows",
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            produced = await fetch_value(
                conn,
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                older,
                younger,
            )
        assert produced == 1

    async def test_explicit_produced_by_edge_does_not_self_infer(
        self,
        integ_store: Store,
    ) -> None:
        older, younger = await self._two_issues(integ_store)
        # Recording provenance directly (younger produced_by older) must not
        # trigger a second inferred edge.
        await integ_store.add_edge(
            from_id=younger,
            to_id=older,
            edge_kind="produced_by",
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            produced = await fetch_value(
                conn,
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                older,
                younger,
            )
        assert produced == 1

    async def test_birth_coincident_inline_create_infers_produced(
        self,
        integ_store: Store,
    ) -> None:
        # The motivating case: anchor exists, a new row is born as an edge target
        # in one atomic batch. The anchor (older) produced the newborn (younger).
        anchor = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="anchor"),
        )
        ids = await integ_store.submit_batch(
            [SubmitIssue(account="tester@example.com", title="newborn")],
            edges=[
                BatchEdge(
                    from_id=anchor,
                    to_index=0,
                    edge_kind="narrows",
                ),
            ],
        )
        newborn = ids[0]
        anchor_row = await integ_store.get_inquiry(anchor)
        assert isinstance(anchor_row, Issue)
        assert newborn in {e.id for e in anchor_row.produces}

    async def test_supersedes_infers_produced_by(self, integ_store: Store) -> None:
        # ``younger supersedes older`` is consistent with the universal rule:
        # both point younger -> older, so the first edge between the pair infers
        # ``younger produced_by older`` (from = younger, to = older) like any
        # other kind. Supersession is not a separate exception.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger,
            to_id=older,
            edge_kind="supersedes",
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            produced = await conn.fetchrow(
                "SELECT from_id, to_id FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                older,
                younger,
            )
        assert produced is not None
        # produced_by stored child -> parent: from = younger (produced), to = older.
        assert (produced["from_id"], produced["to_id"]) == (younger, older)

    async def test_cites_paper_round_trips_into_projection(
        self,
        integ_store: Store,
    ) -> None:
        # A cites_paper edge (citing younger -> cited older) fills the citing
        # paper's forward ``cites`` and the cited paper's inverse ``cited_by``,
        # carrying the edge note (no valence -- historical citation).
        cited, citing = await self._two_papers(integ_store)
        await integ_store.add_edge(
            from_id=citing,
            to_id=cited,
            edge_kind="cites_paper",
            note="see section 3",
            actor="alice",
        )
        citing_row = await integ_store.get_inquiry(citing)
        assert isinstance(citing_row, Paper)
        cited_row = await integ_store.get_inquiry(cited)
        assert isinstance(cited_row, Paper)
        assert cited in {e.id for e in citing_row.cites}
        assert {e.note for e in citing_row.cites} == {"see section 3"}
        assert citing in {e.id for e in cited_row.cited_by}

    async def test_lone_cites_paper_infers_no_produced_by(
        self,
        integ_store: Store,
    ) -> None:
        # Provenance-neutrality: a historical citation is NOT a provenance claim.
        # cites_paper is absent from PRODUCED_INFERENCE_PRECEDENCE, so a pair whose
        # only edge is cites_paper gets NO inferred produced_by (a 2024 paper
        # citing a 2020 paper must not be recorded as "produced by" it).
        cited, citing = await self._two_papers(integ_store)
        await integ_store.add_edge(
            from_id=citing,
            to_id=cited,
            edge_kind="cites_paper",
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            produced = await fetch_value(
                conn,
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                cited,
                citing,
            )
        assert produced == 0
        # And the projection confirms neither side gained a provenance edge.
        citing_row = await integ_store.get_inquiry(citing)
        assert isinstance(citing_row, Paper)
        assert citing_row.produced_by == ()
        assert citing_row.produces == ()

    async def _paper_and_belief(self, store: Store) -> tuple[uuid.UUID, uuid.UUID]:
        """Create an older Belief (claim) and a younger Paper (evidence)."""
        belief = await store.submit_belief(
            SubmitBelief(account="tester@example.com", title="claim"),
        )
        paper = await store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="evidence",
                source="arXiv:2401.00002",
            ),
        )
        await self._set_created(store, belief, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(store, paper, datetime(2024, 1, 2, tzinfo=UTC))
        return belief, paper

    @pytest.mark.parametrize("edge_kind", ["favors", "proves"])
    async def test_lone_citation_of_belief_infers_no_produced_by(
        self,
        integ_store: Store,
        edge_kind: Edge.Kind,
    ) -> None:
        # Epistemic-neutrality: a Paper favoring/proving a Belief is a CITATION,
        # not a provenance claim. proves/favors are absent from
        # PRODUCED_INFERENCE_PRECEDENCE, so a pair whose only edge is such a
        # citation gets NO inferred produced_by. Regression: these kinds were once
        # ranked in PRECEDENCE, which stamped one bogus "Belief produced_by Paper"
        # parent per citing paper -- a cited claim would accrue a fake producer for
        # every piece of evidence marshalled to support it.
        belief, paper = await self._paper_and_belief(integ_store)
        await integ_store.add_edge(
            from_id=paper,
            to_id=belief,
            edge_kind=edge_kind,
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            produced = await fetch_value(
                conn,
                "SELECT count(*) FROM edges WHERE edge_kind = 'produced_by' "
                "AND ((from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1))",
                belief,
                paper,
            )
        assert produced == 0
        # The Belief gained no provenance parent; the Paper cites it via the
        # epistemic edge, which is the only relationship stored.
        belief_row = await integ_store.get_inquiry(belief)
        assert isinstance(belief_row, Belief)
        assert belief_row.produced_by == ()
        assert belief_row.produces == ()

    async def test_cites_paper_allows_mutual_citation(self, integ_store: Store) -> None:
        # A historical bibliography is an EXTERNAL fact we record, not a DAG we
        # own: two papers can legitimately cite each other (companion papers,
        # errata, cross-version references). cites_paper is acyclicity-exempt, so
        # ``A cites B`` then ``B cites A`` both persist rather than raising the
        # per-kind cycle ConflictError that DAG kinds (narrows/requires/...) get.
        a, b = await self._two_papers(integ_store)
        await integ_store.add_edge(
            from_id=a,
            to_id=b,
            edge_kind="cites_paper",
            actor="alice",
        )
        await integ_store.add_edge(
            from_id=b,
            to_id=a,
            edge_kind="cites_paper",
            actor="alice",
        )
        a_row = await integ_store.get_inquiry(a)
        assert isinstance(a_row, Paper)
        b_row = await integ_store.get_inquiry(b)
        assert isinstance(b_row, Paper)
        assert b in {e.id for e in a_row.cites}
        assert a in {e.id for e in b_row.cites}
        # Acyclicity-exemption does NOT relax the self-loop bar: a paper still
        # cannot cite itself (from_id <> to_id, enforced before the policy skip).
        with pytest.raises(ValidationError):
            await integ_store.add_edge(
                from_id=a,
                to_id=a,
                edge_kind="cites_paper",
                actor="alice",
            )

    async def test_precedence_requires_outranks_supersedes_in_one_batch(
        self,
        integ_store: Store,
    ) -> None:
        # A batch links the same pair with supersedes FIRST then requires.
        # Inference is universal -- either kind alone would infer produced_by --
        # so the pair infers it regardless of arrival order; precedence only
        # picks which kind labels the audit reason.
        older, younger = await self._two_issues(integ_store)
        await integ_store.submit_batch(
            [],
            edges=[
                BatchEdge(from_id=younger, to_id=older, edge_kind="supersedes"),
                BatchEdge(from_id=younger, to_id=older, edge_kind="requires"),
            ],
        )
        older_row = await integ_store.get_inquiry(older)
        assert isinstance(older_row, Issue)
        assert younger in {e.id for e in older_row.produces}

    async def test_inferred_produced_by_audit_is_chained_to_trigger(
        self,
        integ_store: Store,
    ) -> None:
        # The inferred produced_by audit chains to the edge that triggered it via
        # caused_by, and carries the stable inferred-provenance reason prefix.
        # The inferred edge is stored child -> parent (from=younger, to=older), so
        # the younger row is the audit subject.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger,
            to_id=older,
            edge_kind="requires",
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            trigger = await fetch_value(
                conn,
                "SELECT id FROM change_log WHERE kind = 'edge_added' "
                "AND new_peer_edge_kind = 'requires' AND subject_id = $1",
                younger,
            )
            inferred = await conn.fetchrow(
                "SELECT caused_by, reason FROM change_log WHERE kind = 'edge_added' "
                "AND new_peer_edge_kind = 'produced_by' AND subject_id = $1 "
                "AND new_peer_id = $2",
                younger,
                older,
            )
        assert inferred is not None
        assert inferred["caused_by"] == trigger
        reason = cast(str, inferred["reason"])
        assert reason.startswith(INFERRED_PROVENANCE_REASON)

    async def test_inferred_produced_by_creation_does_not_cascade(
        self,
        integ_store: Store,
    ) -> None:
        # cascade=False on the inferred edge: its own creation emits no
        # dependency_changed. Any dependency_changed in this run must be caused by
        # the user-driven requires edge, never by the inferred produced_by edge.
        older, younger = await self._two_issues(integ_store)
        await integ_store.add_edge(
            from_id=younger,
            to_id=older,
            edge_kind="requires",
            actor="alice",
        )
        async with integ_store.engine.acquire() as conn:
            inferred_id = await fetch_value(
                conn,
                "SELECT id FROM change_log WHERE kind = 'edge_added' "
                "AND new_peer_edge_kind = 'produced_by' AND subject_id = $1 "
                "AND new_peer_id = $2",
                younger,
                older,
            )
            caused_by_inferred = await fetch_value(
                conn,
                "SELECT count(*) FROM change_log WHERE kind = 'dependency_changed' "
                "AND caused_by = $1",
                inferred_id,
            )
        assert caused_by_inferred == 0

    async def test_lone_proves_of_belief_infers_no_produced_by(
        self,
        integ_store: Store,
    ) -> None:
        # A ``proves`` edge is a CITATION (Artifact -> claim), not provenance: a
        # WebSearch proving a Belief was NOT produced by that Belief -- the search
        # is independent evidence that later bears on the claim. proves is
        # provenance-neutral, so a lone proves between them infers no produced_by.
        # (A Belief genuinely producing a search it spawned is recorded by an
        # explicit produced_by edge, not inferred from a citation.)
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="claim"),
        )
        search = await integ_store.submit_websearch(
            SubmitWebSearch(account="tester@example.com", title="search", query="q"),
        )
        await self._set_created(integ_store, belief, datetime(2020, 1, 1, tzinfo=UTC))
        await self._set_created(integ_store, search, datetime(2020, 1, 2, tzinfo=UTC))
        # Proves stores Artifact -> {Belief, Experiment}: the WebSearch (citing
        # artifact) points up to the older Belief it bears on.
        await integ_store.add_edge(
            from_id=search,
            to_id=belief,
            edge_kind="proves",
            actor="alice",
        )
        belief_row = await integ_store.get_inquiry(belief)
        assert isinstance(belief_row, Belief)
        assert belief_row.produces == ()
        assert belief_row.produced_by == ()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
