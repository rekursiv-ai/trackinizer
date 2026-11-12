"""Submits, batches, and idempotent replays against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import asyncio

import asyncpg
import pytest

from trackinizer.conftest import new_uuid
from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.server.store.store_test_support import fetch_value
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import (
    ArtifactEdge,
    Belief,
    Experiment,
    Issue,
    Paper,
)
from trackinizer.wire.bodies import (
    BatchEdge,
    Citation,
    SubmitArtifact,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
    SubmitWebSearch,
)


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Submits, batches, and idempotent replays, each read back from the database."""

    async def test_per_kind_seq_is_monotonic(self, integ_store: Store) -> None:
        i1 = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i1"),
        )
        i2 = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i2"),
        )
        c1 = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="c1"),
        )
        assert cast(Issue, await integ_store.get_inquiry(i1)).seq == 1
        assert cast(Issue, await integ_store.get_inquiry(i2)).seq == 2
        assert cast(Belief, await integ_store.get_inquiry(c1)).seq == 1

    async def test_experiment_config_empty_dict_round_trips(
        self,
        integ_store: Store,
    ) -> None:
        """An empty ``config={}`` is a real value, stored/read as ``{}`` not NULL.

        ``primitives.insert_inquiry`` binds ``config`` raw (a dict is neither str
        nor sequence, so ``empty_optional_to_none`` never collapses ``{}`` to
        NULL) -- this pins that the empty-dict is preserved, not silently
        dropped.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run", config={}),
        )
        row = await integ_store.get_inquiry(eid)
        assert isinstance(row, Experiment)
        assert row.config == {}

    async def test_submit_belief_keeps_author_judgement(
        self,
        integ_store: Store,
    ) -> None:
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="bare proven",
                judgement="proven",
            ),
        )
        assert (
            cast(Belief, await integ_store.get_inquiry(belief_id)).judgement == "proven"
        )

    async def test_submit_canonicalizes_labels_and_subscribers(
        self,
        integ_store: Store,
    ) -> None:
        """Strip, drop blanks, dedup at write."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="i",
                labels=["x", "x", "  y  ", "", "z"],
                subscribers=["alice", "alice", "bob"],
            ),
        )
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.labels == ("x", "y", "z")
        assert issue.subscribers == ("alice", "bob")

    async def test_invalid_publication_type_rejected_by_check(
        self,
        integ_store: Store,
    ) -> None:
        """An out-of-set publication_type written directly hits the DB CHECK."""
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p"),
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with integ_store.engine.acquire() as conn:
                await conn.execute(
                    "UPDATE inquiries SET paper_publication_type = 'PUNCHCARD' "
                    "WHERE id = $1",
                    paper_id,
                )

    async def test_free_text_venue_accepted(
        self,
        integ_store: Store,
    ) -> None:
        """``venue`` is free text now -- an arbitrary series name is stored."""
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="k", venue="KDD"),
        )
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.venue == "KDD"

    async def test_submit_paper_blank_venue_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """A whitespace-only ``venue`` collapses to NULL at the insert boundary.

        ``venue`` is free text now, so the closed-set Literal no longer rules
        out blanks; the insert must honor the same "unset is NULL" contract the
        edit path (``set_venue``) and the neighboring nullable text columns do.
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", venue="   "),
        )
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.venue is None

    async def test_submit_with_idempotency_key_is_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        """A repeated submit with the same ``idempotency_key`` returns the same row."""
        key = new_uuid()
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i1", idempotency_key=key),
        )
        second = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i1", idempotency_key=key),
        )
        # Server-minted: first == second, but NOT equal to the key.
        assert first == second
        assert first != key
        async with integ_store.engine.acquire() as conn:
            inquiry_count = await fetch_value(
                conn,
                "SELECT COUNT(*) FROM inquiries WHERE id = $1",
                first,
            )
            # The idempotency_key lives on change_log.id of the
            # ``created`` event for the resulting inquiry.
            created_change_id = await fetch_value(
                conn,
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
                first,
            )
        assert inquiry_count == 1
        assert created_change_id == key

    async def test_concurrent_submit_with_idempotency_key_is_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        key = new_uuid()
        first, second = await asyncio.gather(
            integ_store.submit_issue(
                SubmitIssue(
                    account="tester@example.com",
                    title="i1",
                    idempotency_key=key,
                ),
            ),
            integ_store.submit_issue(
                SubmitIssue(
                    account="tester@example.com",
                    title="i1",
                    idempotency_key=key,
                ),
            ),
        )
        assert first == second
        assert first != key  # server-minted.
        async with integ_store.engine.acquire() as conn:
            inquiry_count = await fetch_value(
                conn,
                "SELECT COUNT(*) FROM inquiries WHERE id = $1",
                first,
            )
            change_count = await fetch_value(
                conn,
                "SELECT COUNT(*) FROM change_log WHERE subject_id = $1",
                first,
            )
        assert inquiry_count == 1
        assert change_count == 1

    async def test_submit_codechange_with_sha_writes_created_change(
        self,
        integ_store: Store,
    ) -> None:
        codechange_id = await integ_store.submit_codechange(
            SubmitCodeChange(
                account="tester@example.com",
                title="commit",
                sha="e99c4980",
            ),
            actor="Agent",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kind, new_codechange_sha FROM change_log WHERE subject_id = $1",
                codechange_id,
            )
        assert row is not None
        assert row["kind"] == "created"
        assert row["new_codechange_sha"] is None

    async def test_submit_with_idempotency_key_kind_mismatch_raises(
        self,
        integ_store: Store,
    ) -> None:
        """A second submit at a different kind with the same key is a conflict."""
        key = new_uuid()
        await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i", idempotency_key=key),
        )
        with pytest.raises(ConflictError, match="not Belief"):
            await integ_store.submit_belief(
                SubmitBelief(
                    account="tester@example.com",
                    title="c",
                    idempotency_key=key,
                ),
            )

    async def test_favored_by_stores_artifact_to_belief(
        self,
        integ_store: Store,
    ) -> None:
        """``favors`` stores Artifact -> Belief and projects favored_by.

        A belief is favored *by* its evidence, so the stored edge has the
        Artifact on the from-side and the Belief on the to-side (the citing
        evidence is the younger child pointing up to the older claim). The
        Belief reads the citation back through its ``favored_by`` projection,
        which carries the signed valence.
        """
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="weak support",
                codechanges=[],
                outcome="ok",
            ),
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="plausibly works",
                favored_by=[
                    Citation(
                        artifact_id=exp_id,
                        artifact_kind="Experiment",
                        valence=0.5,
                    ),
                ],
            ),
        )
        # The Belief projects the experiment in favored_by, carrying valence.
        belief = await integ_store.get_inquiry(belief_id)
        assert isinstance(belief, Belief)
        assert belief.favored_by == (
            ArtifactEdge(id=exp_id, kind="Experiment", valence=0.5),
        )
        # The stored edge is from=Experiment (evidence), to=Belief.
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT from_id, from_kind, to_id, to_kind FROM edges "
                "WHERE edge_kind = 'favors'",
            )
        assert row is not None
        assert row["from_id"] == exp_id
        assert row["from_kind"] == "Experiment"
        assert row["to_id"] == belief_id
        assert row["to_kind"] == "Belief"

    async def test_negative_valence_favors_stores_artifact_to_belief(
        self,
        integ_store: Store,
    ) -> None:
        """A negative-valence ``favors`` is the old ``disfavors``: same edge kind.

        For-vs-against folded into the sign of valence, so counter-evidence is a
        ``favors`` edge (stored Artifact -> Belief) carrying a negative valence,
        not a separate ``disfavors`` kind.
        """
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="counter-evidence",
                codechanges=[],
                outcome="ok",
            ),
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="probably wrong",
                favored_by=[
                    Citation(
                        artifact_id=exp_id,
                        artifact_kind="Experiment",
                        valence=-0.5,
                    ),
                ],
            ),
        )
        belief = await integ_store.get_inquiry(belief_id)
        assert isinstance(belief, Belief)
        assert belief.favored_by == (
            ArtifactEdge(id=exp_id, kind="Experiment", valence=-0.5),
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT from_id, from_kind, to_id, to_kind, valence FROM edges "
                "WHERE edge_kind = 'favors'",
            )
        assert row is not None
        assert row["from_id"] == exp_id
        assert row["from_kind"] == "Experiment"
        assert row["to_id"] == belief_id
        assert row["to_kind"] == "Belief"
        assert row["valence"] == -0.5

    async def test_submit_experiment_rejects_non_codechange(
        self,
        integ_store: Store,
    ) -> None:
        """``Experiment.codechanges`` UUIDs are FK-free; validator catches drift."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="not a sha"),
        )
        with pytest.raises(ConflictError, match="CodeChange"):
            await integ_store.submit_experiment(
                SubmitExperiment(
                    account="tester@example.com",
                    title="x",
                    codechanges=[issue_id],
                    outcome="",
                ),
            )

    async def test_submit_belief_rejects_declared_kind_mismatch(
        self,
        integ_store: Store,
    ) -> None:
        """Wire-declared citation kind must match the actual stored kind."""
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="exp",
                codechanges=[],
                outcome="ok",
            ),
        )
        with pytest.raises(ConflictError, match="declared as"):
            await integ_store.submit_belief(
                SubmitBelief(
                    account="tester@example.com",
                    title="bad citation",
                    # Declared as Paper but actually an Experiment.
                    proved_by=[
                        Citation(
                            artifact_id=exp_id,
                            artifact_kind="Paper",
                            valence=1.0,
                        ),
                    ],
                ),
            )

    async def test_submit_batch_returns_ids_in_order(
        self,
        integ_store: Store,
    ) -> None:
        """``submit_batch`` collapses N round-trips into one and returns ordered ids."""
        items: list[SubmitIssue | SubmitArtifact | SubmitPaper] = [
            SubmitIssue(
                account="tester@example.com",
                title="issue",
                idempotency_key=new_uuid(),
            ),
            SubmitArtifact(
                account="tester@example.com",
                title="art",
                idempotency_key=new_uuid(),
            ),
            SubmitPaper(
                account="tester@example.com",
                title="p",
                source="https://x",
                idempotency_key=new_uuid(),
            ),
        ]
        ids = await integ_store.submit_batch(items)
        # Reads back in submit order.
        kinds = [(await integ_store.get_inquiry(rid)).__class__.__name__ for rid in ids]
        assert kinds == ["Issue", "Artifact", "Paper"]

    async def test_submit_batch_failure_persists_no_rows(
        self,
        integ_store: Store,
    ) -> None:
        """A failing item rolls the whole batch back: zero rows persist.

        The real-DB proof of atomicity the mock tier cannot give. A key is
        first committed as an Issue; the batch's second item reuses that key
        for a different kind (Artifact), which
        ``_lookup_existing_by_change`` rejects with a kind mismatch. The
        batch's first (otherwise valid) Issue must roll back too.
        """
        seeded_key = new_uuid()
        await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="seed",
                idempotency_key=seeded_key,
            ),
        )
        issues_before = len(await integ_store.list_kind("Issue"))
        with pytest.raises(ConflictError, match="already created"):
            await integ_store.submit_batch(
                [
                    SubmitIssue(
                        account="tester@example.com",
                        title="i1",
                        idempotency_key=new_uuid(),
                    ),
                    SubmitArtifact(
                        account="tester@example.com",
                        title="a1",
                        idempotency_key=seeded_key,
                    ),
                ],
            )
        # The batch's new Issue did not persist; only the seed remains.
        assert len(await integ_store.list_kind("Issue")) == issues_before
        assert await integ_store.list_kind("Artifact") == []

    async def test_submit_batch_creates_rows_and_edge_atomically(
        self,
        integ_store: Store,
    ) -> None:
        """Root + inline target + linking edge all land in one request."""
        ids = await integ_store.submit_batch(
            [
                SubmitIssue(
                    account="tester@example.com",
                    title="root",
                    idempotency_key=new_uuid(),
                ),
                SubmitIssue(
                    account="tester@example.com",
                    title="blocker",
                    idempotency_key=new_uuid(),
                ),
            ],
            edges=[BatchEdge(from_index=0, to_index=1, edge_kind="requires")],
        )
        root_id, blocker_id = ids
        edge = await integ_store.get_edge(
            from_id=root_id,
            to_id=blocker_id,
            edge_kind="requires",
        )
        assert edge is not None

    async def test_submit_batch_deep_chain_wires_nonroot_from_index(
        self,
        integ_store: Store,
    ) -> None:
        """A deep chain (Issue#425 item 6) wires edges whose SOURCE is non-root.

        The deep-cursor create flattens ``belief produced websearch produced
        paper`` into a batch of ``produced_by`` edges (stored child -> parent:
        from=produced/younger, to=producer/older). The second edge's
        ``from_index`` is the paper (item 2), pointing at the websearch (item 1)
        as its producer -- NOT the root belief (item 0). Existing batch tests
        only exercise ``from_index=0``; this pins that a non-root source index
        resolves correctly against real Postgres, building belief->search->paper
        rather than belief->{search,paper}.
        """
        ids = await integ_store.submit_batch(
            [
                SubmitBelief(
                    account="tester@example.com",
                    title="bet",
                    idempotency_key=new_uuid(),
                ),  # 0.
                SubmitWebSearch(
                    account="tester@example.com",
                    title="check",
                    query="q",
                    idempotency_key=new_uuid(),
                ),  # 1.
                SubmitPaper(
                    account="tester@example.com",
                    title="hit",
                    idempotency_key=new_uuid(),
                ),  # 2.
            ],
            edges=[
                # Search produced_by belief; paper produced_by search.
                BatchEdge(from_index=1, to_index=0, edge_kind="produced_by"),
                BatchEdge(from_index=2, to_index=1, edge_kind="produced_by"),
            ],
        )
        belief_id, search_id, paper_id = ids
        # The paper was produced_by the search (deep), NOT the belief.
        assert (
            await integ_store.get_edge(
                from_id=paper_id,
                to_id=search_id,
                edge_kind="produced_by",
            )
            is not None
        )
        assert (
            await integ_store.get_edge(
                from_id=paper_id,
                to_id=belief_id,
                edge_kind="produced_by",
            )
            is None
        )

    async def test_submit_batch_edge_to_existing_target_by_id(
        self,
        integ_store: Store,
    ) -> None:
        """A batch edge may point at a pre-existing row by UUID."""
        existing = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="existing"),
        )
        ids = await integ_store.submit_batch(
            [
                SubmitIssue(
                    account="tester@example.com",
                    title="root",
                    idempotency_key=new_uuid(),
                ),
            ],
            edges=[BatchEdge(from_index=0, to_id=existing, edge_kind="requires")],
        )
        edge = await integ_store.get_edge(
            from_id=ids[0],
            to_id=existing,
            edge_kind="requires",
        )
        assert edge is not None

    async def test_submit_batch_duplicate_edge_upserts_and_commits(
        self,
        integ_store: Store,
    ) -> None:
        """A duplicate edge in a batch is an idempotent upsert, not a failure.

        Edge creation is an upsert everywhere (symmetric with ``label add`` and
        every other set): a pre-existing edge does not abort the batch. The root
        row commits and the duplicate edge no-ops (or applies supplied
        annotations) instead of raising and rolling the batch back.
        """
        target = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="target"),
        )
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="first"),
        )
        await integ_store.add_edge(
            from_id=first,
            to_id=target,
            edge_kind="requires",
            actor="user",
        )
        issues_before = len(await integ_store.list_kind("Issue"))
        ids = await integ_store.submit_batch(
            [
                SubmitIssue(
                    account="tester@example.com",
                    title="root",
                    idempotency_key=new_uuid(),
                ),
            ],
            edges=[
                # Reuse first->target; the duplicate upserts, the root commits.
                BatchEdge(from_id=first, to_id=target, edge_kind="requires"),
            ],
        )
        # The root row committed (one new Issue), not rolled back.
        assert len(await integ_store.list_kind("Issue")) == issues_before + 1
        assert len(ids) == 1

    async def test_submit_empty_optionals_store_null(
        self,
        integ_store: Store,
    ) -> None:
        """Submitting empty/blank optionals stores NULL, not the sentinel.

        The insert path shares the storage rule: an empty owner/description
        or all-blank labels is absence -> NULL, so a row created with empty
        values is indistinguishable from one created with them omitted.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="s",
                owner="",
                description="",
                labels=["", "  "],
            ),
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner, description, labels FROM inquiries WHERE id = $1",
                rid,
            )
        assert row is not None
        assert row["owner"] is None
        assert row["description"] is None
        assert row["labels"] is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestHeaderOnlyKeyReplay:
    """A submit keyed by the request header alone replays rather than failing."""

    async def test_header_only_submit_retry_replays_not_500(
        self,
        integ_store: Store,
    ) -> None:
        """A header-only submit (slot set, no body key) replays on retry.

        The ``Idempotency-Key`` header lands in the change-id slot via
        ``set_client_change_id`` with ``SubmitIssue.idempotency_key`` unset.
        The submit path used to ``assert req.idempotency_key is not None`` in
        the collision-recovery branch, so a retried header-only submit hit
        the assert -> 500. Now the effective key (body field OR slot) drives
        both the replay probe and the recovery, so the retry returns the same
        id (REV-OPUS-01).
        """
        key = new_uuid()
        # First header-only submit: slot set, no body idempotency_key.
        set_client_change_id(key)
        first = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="header-only"),
        )
        # Retry with the same header key, still no body key. Must replay the
        # original row, not assert/500.
        set_client_change_id(key)
        second = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="header-only"),
        )
        assert first == second
        assert first != key  # server-minted.
        async with integ_store.engine.acquire() as conn:
            inquiry_count = await fetch_value(
                conn,
                "SELECT COUNT(*) FROM inquiries WHERE id = $1",
                first,
            )
            change_count = await fetch_value(
                conn,
                "SELECT COUNT(*) FROM change_log WHERE subject_id = $1",
                first,
            )
        assert inquiry_count == 1
        assert change_count == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
