"""Field edits against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncio

from treekle import from_plain

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.server.store.store_test_support import fetch_value
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Experiment, Issue, Paper
from trackinizer.wire.bodies import SubmitExperiment, SubmitIssue, SubmitPaper
from trackinizer.wire.filters import Filter


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Field edits, each read back from the database."""

    async def test_experiment_config_reorder_is_noop_edit(
        self,
        integ_store: Store,
    ) -> None:
        """Re-setting a key-reordered but equal config is a no-op (no phantom row).

        JSONB dedup relies on Python dict ``==`` (order-insensitive) in
        ``_set_field``; a reordered-equal config must return ``None`` (no change)
        rather than write a phantom ``experiment_config`` audit row.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="run",
                config={"a": 1, "b": 2},
            ),
        )
        change_id = await integ_store.set_config(
            eid,
            {"b": 2, "a": 1},
            actor="scientist",
        )
        assert change_id is None

    async def test_experiment_config_round_trips_and_edits(
        self,
        integ_store: Store,
    ) -> None:
        """``config`` stores a JSON object, reads back as a dict, and edits.

        The wandb ``wandb.init(config=...)`` analogue: hyperparameters ride on
        the Experiment row as one JSONB object, opaque to trackinizer.
        """
        cfg: dict[str, object] = {
            "lr": 3e-4,
            "batch": 32,
            "nested": {"warmup": 100},
            "tags": ["a"],
        }
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run", config=cfg),
        )
        row = await integ_store.get_inquiry(eid)
        assert isinstance(row, Experiment)
        assert row.config == cfg  # `dict` in, dict out (asyncpg jsonb codec)

        # Edit via set_config: overwrite the whole object.
        new_cfg: dict[str, object] = {"lr": 1e-4, "batch": 64}
        await integ_store.set_config(eid, new_cfg, actor="scientist")
        row2 = await integ_store.get_inquiry(eid)
        assert isinstance(row2, Experiment)
        assert row2.config == new_cfg

        # Clear to NULL.
        await integ_store.set_config(eid, None, actor="scientist")
        row3 = await integ_store.get_inquiry(eid)
        assert isinstance(row3, Experiment)
        assert row3.config is None

    async def test_set_source_updates_self_describing_id(
        self,
        integ_store: Store,
    ) -> None:
        """``set_source`` writes the single scheme-tagged ``source`` identifier."""
        paper_id = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="https://x"),
        )
        await integ_store.set_source(paper_id, "doi:10.1/abc", actor="user")
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.source == "doi:10.1/abc"

    async def test_google_scholar_cluster_id_round_trips_on_submit_and_edit(
        self,
        integ_store: Store,
    ) -> None:
        """``google_scholar_cluster_id`` persists via submit and is editable.

        The Scholar cluster handle coexists with ``source`` (e.g. a DOI) on one
        Paper: cite by DOI, keep the Scholar identity separately. A plain optional
        identifier -- set at create, re-pointed via ``set_google_scholar_cluster_id``,
        cleared with an empty value.
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="p",
                source="doi:10.1/abc",
                google_scholar_cluster_id="12345678901234567890",
            ),
        )
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.google_scholar_cluster_id == "12345678901234567890"
        # Coexists with source -- both stored on the one row.
        assert paper.source == "doi:10.1/abc"
        # Re-point via the setter.
        await integ_store.set_google_scholar_cluster_id(paper_id, "99999", actor="user")
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.google_scholar_cluster_id == "99999"
        # An empty value clears to NULL (the "unset is NULL" rule).
        await integ_store.set_google_scholar_cluster_id(paper_id, "", actor="user")
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.google_scholar_cluster_id is None

    async def test_google_scholar_cluster_id_edit_is_audited(
        self,
        integ_store: Store,
    ) -> None:
        """A ``google_scholar_cluster_id`` edit records old/new values in change_log.

        The change_log ``old_/new_`` mirror derives from CHANGE_LOG_COLUMN_ORDER;
        this pins that the mirror carries the field's transition (no silent audit
        loss -- the GSI-01 class).
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="p",
                google_scholar_cluster_id="old-id",
            ),
        )
        await integ_store.set_google_scholar_cluster_id(
            paper_id,
            "new-id",
            actor="user",
        )
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT old_paper_google_scholar_cluster_id, "
                "new_paper_google_scholar_cluster_id "
                "FROM change_log WHERE subject_id = $1 "
                "AND kind = 'paper_google_scholar_cluster_id'",
                paper_id,
            )
        assert row is not None
        assert row["old_paper_google_scholar_cluster_id"] == "old-id"
        assert row["new_paper_google_scholar_cluster_id"] == "new-id"

    async def test_set_source_rejects_unschemed_value(
        self,
        integ_store: Store,
    ) -> None:
        """The edit path enforces the same ``<scheme>:<rest>`` shape as create.

        ``SubmitPaper`` already rejects a bare source at the wire; ``set_source``
        must too, so the rule holds on both paths -- a bare ``2405.16391`` is a
        clean ``ConflictError``, and the stored source is left unchanged.
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="p",
                source="arXiv:2405.16391",
            ),
        )
        with pytest.raises(ConflictError, match="scheme-tagged"):
            await integ_store.set_source(paper_id, "2405.16391", actor="user")
        # The pre-edit source survives the rejected write.
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.source == "arXiv:2405.16391"

    async def test_paper_bib_fields_round_trip(
        self,
        integ_store: Store,
    ) -> None:
        """Submit a Paper with every bib field, read it back, edit, re-read."""
        published = datetime(2024, 5, 16, tzinfo=UTC)
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="Attention Is All You Need",
                abstract="The dominant sequence transduction models...",
                authors=["Vaswani", "Shazeer", "Parmar"],
                publication_type="inproceedings",
                venue="NeurIPS",
                subvenue="Main",
                publish_date=published,
                source="arXiv:1706.03762",
            ),
        )
        paper = await integ_store.get_inquiry(paper_id)
        assert isinstance(paper, Paper)
        assert paper.abstract == "The dominant sequence transduction models..."
        assert paper.authors == ("Vaswani", "Shazeer", "Parmar")
        assert paper.publication_type == "inproceedings"
        assert paper.venue == "NeurIPS"
        assert paper.subvenue == "Main"
        assert paper.publish_date == published
        assert paper.source == "arXiv:1706.03762"

        # Edit a closed-set field and a list field, then re-read.
        await integ_store.set_publication_type(paper_id, "article", actor="u")
        await integ_store.set_venue(paper_id, "JMLR", actor="u")
        await integ_store.add_author(paper_id, "Uszkoreit", actor="u")
        edited = await integ_store.get_inquiry(paper_id)
        assert isinstance(edited, Paper)
        assert edited.publication_type == "article"
        assert edited.venue == "JMLR"
        assert edited.authors == ("Vaswani", "Shazeer", "Parmar", "Uszkoreit")

    async def test_byline_add_remove_preserve_duplicates(
        self,
        integ_store: Store,
    ) -> None:
        """A byline treats duplicate authors as distinct.

        Add always appends, remove drops only the first match (not a set
        collapse).
        """
        paper_id = await integ_store.submit_paper(
            SubmitPaper(
                account="tester@example.com",
                title="dup byline",
                authors=["Smith", "Jones", "Smith"],
            ),
        )
        # Add of an already-present author appends a second entry.
        await integ_store.add_author(paper_id, "Smith", actor="u")
        added = await integ_store.get_inquiry(paper_id)
        assert isinstance(added, Paper)
        assert added.authors == ("Smith", "Jones", "Smith", "Smith")
        # Remove drops only the first occurrence.
        await integ_store.remove_author(paper_id, "Smith", actor="u")
        removed = await integ_store.get_inquiry(paper_id)
        assert isinstance(removed, Paper)
        assert removed.authors == ("Jones", "Smith", "Smith")

    async def test_set_codechanges_rejects_unknown_id(
        self,
        integ_store: Store,
    ) -> None:
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="e",
                codechanges=[],
                outcome="",
            ),
        )
        bogus = new_uuid()
        with pytest.raises(ConflictError, match="not found"):
            await integ_store.set_codechanges(exp_id, [bogus], actor="user")

    async def test_subscribers_edit_routes_to_removed_subscriber(
        self,
        integ_store: Store,
    ) -> None:
        """A just-removed subscriber sees the audit row that removed them.

        Regression: ``subscribers_snapshot`` captured the post-edit list,
        so unsubscribe events were invisible to the removed agent.
        """
        issue_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="i",
                subscribers=["alice", "bob"],
            ),
        )
        await integ_store.set_subscribers(issue_id, ["bob"], actor="user")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT subscribers_snapshot FROM change_log "
                "WHERE subject_id = $1 AND kind = 'subscribers'",
                issue_id,
            )
        assert row is not None
        snapshot = from_plain(row["subscribers_snapshot"], list[str])
        assert "alice" in snapshot
        assert "bob" in snapshot

    async def test_add_label_is_atomic_and_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        """``add_label`` is race-safe (the OG ``subscribe_self`` pattern)."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i"),
        )
        await asyncio.gather(
            integ_store.add_label(issue_id, "x", actor="alice"),
            integ_store.add_label(issue_id, "y", actor="bob"),
            integ_store.add_label(issue_id, "z", actor="carol"),
        )
        # Re-adding "x" is idempotent.
        await integ_store.add_label(issue_id, "x", actor="alice")
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.labels is not None
        assert sorted(issue.labels) == ["x", "y", "z"]
        await integ_store.remove_label(issue_id, "y", actor="bob")
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.labels is not None
        assert sorted(issue.labels) == ["x", "z"]

    async def test_remove_issue_kind_rejects_empty_via_db_check(
        self,
        integ_store: Store,
    ) -> None:
        """The non-empty invariant on ``Issue.issue_kind`` lives in the DB CHECK.

        ``array_length(issue_kind, 1) >= 1``) and propagates from
        :class:`ColumnSpec.min_items`. Removing the last kind surfaces as
        ``ConflictError``, not as a silent empty array.
        """
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i", issue_kind=["bug"]),
        )
        with pytest.raises(ConflictError, match="check constraint"):
            await integ_store.remove_issue_kind(issue_id, "bug", actor="alice")
        # The pre-removal state is preserved.
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.issue_kind == ("bug",)
        # Adding a second kind then removing the original is allowed.
        await integ_store.add_issue_kind(issue_id, "task", actor="alice")
        await integ_store.remove_issue_kind(issue_id, "bug", actor="alice")
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.issue_kind == ("task",)

    async def test_transition_status_is_compare_and_set(
        self,
        integ_store: Store,
    ) -> None:
        """Concurrent ``close`` + ``done`` cannot both succeed."""
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i"),
        )
        await integ_store.transition_status(
            issue_id,
            expected_from="active",
            to="complete",
            actor="alice",
        )
        # Second transition from "active" fails: the row is already complete.
        with pytest.raises(ConflictError, match="expected 'active'"):
            await integ_store.transition_status(
                issue_id,
                expected_from="active",
                to="abandoned",
                actor="bob",
            )
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.status == "complete"

    async def test_add_subscriber_is_atomic_and_idempotent(
        self,
        integ_store: Store,
    ) -> None:
        """Concurrent ``add_subscriber`` calls converge to the right set.

        Regression: the previous CLI Watch composed read-modify-write
        client-side, so two concurrent ``trax watch`` calls each read
        the same pre-edit list and the second POST clobbered the first.
        The server-side primitive uses ``FOR UPDATE`` and is idempotent.

        ``add_subscriber`` also supports ``subscriber != author``: an
        admin / router can subscribe someone else without round-trips
        through the racy whole-list edit path.
        """
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i"),
        )
        # Parallel self-subscribes by three distinct actors.
        await asyncio.gather(
            integ_store.add_subscriber(issue_id, "alice", actor="alice"),
            integ_store.add_subscriber(issue_id, "bob", actor="bob"),
            integ_store.add_subscriber(issue_id, "carol", actor="carol"),
        )
        # Re-subscribing alice is a no-op (idempotent).
        await integ_store.add_subscriber(issue_id, "alice", actor="alice")
        # An admin subscribes dave on dave's behalf -- the
        # arbitrary-actor capability the whole-list rejection map
        # promises.
        await integ_store.add_subscriber(issue_id, "dave", actor="admin")
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.subscribers is not None
        assert sorted(issue.subscribers) == ["alice", "bob", "carol", "dave"]
        # Remove is atomic too and accepts a non-self subscriber.
        await integ_store.remove_subscriber(issue_id, "bob", actor="admin")
        issue = await integ_store.get_inquiry(issue_id)
        assert isinstance(issue, Issue)
        assert issue.subscribers is not None
        assert sorted(issue.subscribers) == ["alice", "carol", "dave"]

    async def test_clearing_nullable_columns_stores_null_not_sentinel(
        self,
        integ_store: Store,
    ) -> None:
        """Clearing a nullable column stores SQL NULL, not the empty sentinel.

        Unset is one encoding -- NULL -- at insert *and* on edit. Clearing
        labels (set_labels(None)) or blanking a scalar (set_description(""))
        previously stored '{}' / '' so ``isnull`` could not see the cleared
        row. Both clear paths now collapse to NULL.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="s",
                description="d",
                labels=["x"],
            ),
        )
        await integ_store.set_labels(rid, None, actor="u")
        await integ_store.set_description(rid, "", actor="u")
        await integ_store.set_owner(rid, "", actor="u")

        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner, description, labels FROM inquiries WHERE id = $1",
                rid,
            )
        assert row is not None
        assert row["labels"] is None
        assert row["description"] is None
        assert row["owner"] is None

        # ``isnull`` now matches the cleared row on every column.
        for field in ("labels", "description", "owner"):
            rows = await integ_store.list_kind(
                "Issue",
                filters=(Filter(field=field, op="isnull", value=""),),
            )
            assert rid in {r.id for r in rows}, field

    async def test_blank_only_list_edit_storage_and_audit_agree(
        self,
        integ_store: Store,
    ) -> None:
        """A blank-only list edit collapses post-normalize: storage and audit agree.

        ``set_labels(["", "  "])`` normalizes to ``()`` -- the empty result is
        absence, so storage is NULL. Setting it on an already-unset row is a
        no-op (no phantom change row), and any emitted audit must record NULL,
        never the ``()`` sentinel that contradicts storage.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s"),
        )
        # Already-unset labels; a blank-only set is a no-op.
        change_id = await integ_store.set_labels(rid, ["", "  "], actor="u")
        assert change_id is None
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT labels FROM inquiries WHERE id = $1",
                rid,
            )
        assert stored is None

        # From a populated state, a blank-only set clears to NULL with a
        # NULL-recording audit (not ()).
        await integ_store.set_labels(rid, ["keep"], actor="u")
        change_id = await integ_store.set_labels(rid, ["", "  "], actor="u")
        assert change_id is not None
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow("SELECT labels FROM inquiries WHERE id = $1", rid)
            new_labels = await fetch_value(
                conn,
                "SELECT new_labels FROM change_log WHERE id = $1",
                change_id,
            )
        assert row is not None
        assert row["labels"] is None
        assert new_labels is None

    async def test_set_issue_kind_blank_only_raises_clean_conflict(
        self,
        integ_store: Store,
    ) -> None:
        """Emptying a min_items column via the setter raises ``ConflictError``.

        ``issue_kind`` normalizes a blank-only input to ``()``; the DB
        ``cardinality >= 1`` CHECK rejects it. ``_set_field`` wraps that as a
        clean ``ConflictError`` (a 4xx), not a raw asyncpg 500.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", issue_kind=["bug"]),
        )
        with pytest.raises(ConflictError, match="check constraint"):
            # Deliberately blank-only input: it canonicalizes to (), exercising
            # the normalize-to-empty path against the min_items CHECK.
            await integ_store.set_issue_kind(
                rid,
                ["", "  "],  # ty: ignore[invalid-argument-type] -- The integration fixture passes a dynamically-shaped request payload.  # pyright: ignore[reportArgumentType] -- The integration fixture passes a dynamically-shaped request payload.  # pyrefly: ignore[bad-argument-type] -- The integration fixture passes a dynamically-shaped request payload.
                actor="u",
            )

    async def test_set_source_blank_is_consistent_no_op(
        self,
        integ_store: Store,
    ) -> None:
        """``set_source("")`` on an unset source is a no-op, not a phantom change.

        The compare and audit must use the collapsed value, matching storage:
        blanking an already-NULL source changes nothing.
        """
        pid = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p"),
        )
        change_id = await integ_store.set_source(pid, "  ", actor="u")
        assert change_id is None
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT paper_source FROM inquiries WHERE id = $1",
                pid,
            )
        assert stored is None

    async def test_edit_from_null_records_null_old_side_in_audit(
        self,
        integ_store: Store,
    ) -> None:
        """An edit from an unset (NULL) column logs NULL on the audit old side.

        The change-log distinguishes NULL ("event did not touch / value was
        absent") from () ("explicitly cleared"). Editing labels from unset
        must record ``old_labels IS NULL``, not ``'{}'``.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s"),
        )
        change_id = await integ_store.set_labels(rid, ["x"], actor="u")
        assert change_id is not None
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT old_labels, new_labels FROM change_log WHERE id = $1",
                change_id,
            )
        assert row is not None
        assert row["old_labels"] is None
        new_labels = row["new_labels"]
        assert new_labels == ["x"]

    async def test_removing_last_list_item_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """Removing the last label stores SQL NULL, not the empty sentinel.

        The list add/remove path must honor the same "unset is NULL" rule as
        the whole-list clear: an empty result is absence, so ``labels isnull``
        finds the row after its last label is removed.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", labels=["only"]),
        )
        await integ_store.remove_label(rid, "only", actor="u")
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT labels FROM inquiries WHERE id = $1",
                rid,
            )
        assert stored is None
        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="labels", op="isnull", value=""),),
        )
        assert rid in {r.id for r in rows}

    async def test_min_items_column_is_exempt_from_null_collapse(
        self,
        integ_store: Store,
    ) -> None:
        """A ``min_items`` column empties to a CHECK error, never NULL.

        ``labels`` (min_items 0) collapses to NULL when emptied, but
        ``issue_kind`` (min_items 1) must reach the DB CHECK and raise --
        the null-collapse rule does not weaken the non-empty invariant.
        """
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", issue_kind=["bug"]),
        )
        with pytest.raises(ConflictError, match="check constraint"):
            await integ_store.remove_issue_kind(rid, "bug", actor="u")
        kept = await integ_store.get_inquiry(rid)
        assert isinstance(kept, Issue)
        assert kept.issue_kind == ("bug",)

    async def test_set_source_empty_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """``set_source('')`` clears to NULL, like every other nullable setter."""
        rid = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="p", source="http://x"),
        )
        await integ_store.set_source(rid, "", actor="u")
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT paper_source FROM inquiries WHERE id = $1",
                rid,
            )
        assert stored is None

    async def test_blanking_scalar_with_whitespace_stores_null(
        self,
        integ_store: Store,
    ) -> None:
        """A whitespace-only scalar edit collapses to NULL, matching migration 003."""
        rid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="s", owner="dana"),
        )
        await integ_store.set_owner(rid, "   ", actor="u")
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT owner FROM inquiries WHERE id = $1",
                rid,
            )
        assert stored is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
