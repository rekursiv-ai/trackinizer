"""Reads, lists, and next-issue selection against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from trackinizer.server.store.store_test_support import fetch_value
from trackinizer.types.cost import Cost
from trackinizer.types.inquiries import Issue, Paper
from trackinizer.wire.bodies import (
    Citation,
    SubmitBelief,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
)
from trackinizer.wire.filters import Filter


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Reads, lists, and next-issue selection, each read back from the database."""

    async def test_blocked_task_skipped_by_next_issue(
        self,
        integ_store: Store,
    ) -> None:
        blocker_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="first"),
        )
        await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="dependent",
                requires=[blocker_id],
            ),
        )
        nxt = await integ_store.next_issue()
        assert nxt is not None
        assert nxt.id == blocker_id
        await integ_store.set_status(blocker_id, "complete", actor="user")
        nxt2 = await integ_store.next_issue()
        assert nxt2 is not None
        assert nxt2.title == "dependent"

    async def test_get_inquiry_materializes_the_row_with_both_edge_directions(
        self,
        integ_store: Store,
    ) -> None:
        blocker_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="blocker"),
        )
        dependent_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="dependent",
                requires=[blocker_id],
            ),
        )

        dependent = await integ_store.get_inquiry(dependent_id)
        blocker = await integ_store.get_inquiry(blocker_id)

        assert isinstance(dependent, Issue)
        assert isinstance(blocker, Issue)
        assert (dependent.id, dependent.title) == (dependent_id, "dependent")
        assert [edge.id for edge in dependent.requires] == [blocker_id]
        assert [edge.id for edge in blocker.required_by] == [dependent_id]

    async def test_filter_by_venue(
        self,
        integ_store: Store,
    ) -> None:
        """A ``venue = NeurIPS`` filter selects only the matching Paper."""
        neurips = await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="n", venue="NeurIPS"),
        )
        await integ_store.submit_paper(
            SubmitPaper(account="tester@example.com", title="i", venue="ICML"),
        )
        rows = await integ_store.list_kind(
            "Paper",
            filters=(Filter(field="paper_venue", op="is", value="NeurIPS"),),
        )
        ids = {r.id for r in rows}
        assert neurips in ids
        assert all(cast(Paper, r).venue == "NeurIPS" for r in rows)

    async def test_cost_rollup_honors_ownership(
        self,
        integ_store: Store,
    ) -> None:
        broader_issue_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="root",
                marginal_cost=Cost(agent_usd=0.01),
            ),
        )
        await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="child task",
                narrows=[(broader_issue_id, None)],
                marginal_cost=Cost(agent_usd=0.04),
            ),
        )
        self_only = await integ_store.inquiry_cost(broader_issue_id, deep=False)
        assert self_only is not None
        assert self_only.agent_usd == pytest.approx(0.01)
        rolled = await integ_store.inquiry_cost(broader_issue_id, deep=True)
        assert rolled is not None
        assert rolled.agent_usd == pytest.approx(0.05)

    async def test_next_issue_skips_superseded_issues(
        self,
        integ_store: Store,
    ) -> None:
        """An Issue with a ``supersedes`` successor must not be scheduled."""
        old_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="old"),
        )
        new_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="new"),
        )
        await integ_store.add_edge(
            from_id=new_id,
            to_id=old_id,
            edge_kind="supersedes",
            actor="user",
        )
        nxt = await integ_store.next_issue()
        assert nxt is not None
        assert nxt.id == new_id

    async def test_proves_belief_excludes_superseded_artifact(
        self,
        integ_store: Store,
    ) -> None:
        """A superseded Artifact stops counting as currently-true evidence."""
        old_exp = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="old",
                codechanges=[],
                outcome="ok",
            ),
        )
        new_exp = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="new",
                codechanges=[],
                outcome="ok",
            ),
        )
        await integ_store.set_status(old_exp, "complete", actor="user")
        await integ_store.set_status(new_exp, "complete", actor="user")
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="c",
                judgement="proven",
                proved_by=[
                    Citation(
                        artifact_id=old_exp,
                        artifact_kind="Experiment",
                        valence=1.0,
                    ),
                ],
            ),
        )
        # Initially old_exp is currently-true evidence.
        assert [e.id for e in await integ_store.proves_belief(belief_id)] == [old_exp]
        # Once new_exp supersedes old_exp, old_exp drops out.
        await integ_store.add_edge(
            from_id=new_exp,
            to_id=old_exp,
            edge_kind="supersedes",
            actor="user",
        )
        assert await integ_store.proves_belief(belief_id) == []

    async def test_list_kind_filters_apply_before_limit(
        self,
        integ_store: Store,
    ) -> None:
        """Filters must bound the result set, not the pre-filter scan.

        Regression for Issue#256: ``list_kind(filters=...)`` previously
        post-filtered a server-truncated batch, so a needle past the
        ``limit`` recency window was unreachable. Here ``needle`` is
        the first-inserted (oldest) Issue and ``limit=5`` is well
        below the 30 newer Issues that follow; the contract is that
        every match comes back regardless of where it sits in the
        natural recency order.
        """
        needle_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="needle in the haystack",
                labels=["target"],
            ),
        )
        for i in range(30):
            await integ_store.submit_issue(
                SubmitIssue(account="tester@example.com", title=f"noise {i}"),
            )

        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="title", op="re", value="needle"),),
            limit=5,
        )
        assert [r.id for r in rows] == [needle_id], (
            f"expected only the needle past the LIMIT window; got {[r.id for r in rows]}"
        )

        label_rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="labels", op="is", value="target"),),
            limit=5,
        )
        assert [r.id for r in label_rows] == [needle_id]

    async def test_unset_optional_columns_store_null_and_isnull_finds_them(
        self,
        integ_store: Store,
    ) -> None:
        """An unset optional column stores SQL NULL, and ``isnull`` finds it.

        Regression for the nullable-columns migration: ``owner`` / ``labels``
        were ``NOT NULL DEFAULT '' / '{}'``, so an unset value stored an empty
        sentinel that ``isnull`` could never see and ``notnull`` always matched.
        With the columns nullable, an unspecified owner is genuinely NULL (not
        actor-stamped), ``owner isnull`` returns it, and ``owner notnull``
        excludes it. The same holds for the ``labels`` array column.
        """
        unowned = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="no owner here"),
        )
        owned = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="has an owner",
                owner="dana",
                labels=["tagged"],
            ),
        )

        # The unset owner is stored as SQL NULL, not the actor or an empty string.
        async with integ_store.engine.acquire() as conn:
            stored = await fetch_value(
                conn,
                "SELECT owner FROM inquiries WHERE id = $1",
                unowned,
            )
        assert stored is None

        isnull = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="owner", op="isnull", value=""),),
        )
        assert unowned in {r.id for r in isnull}
        assert owned not in {r.id for r in isnull}

        notnull = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="owner", op="notnull", value=""),),
        )
        assert owned in {r.id for r in notnull}
        assert unowned not in {r.id for r in notnull}

        # The array column behaves identically: no labels -> NULL -> isnull.
        labels_isnull = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="labels", op="isnull", value=""),),
        )
        assert unowned in {r.id for r in labels_isnull}
        assert owned not in {r.id for r in labels_isnull}

    async def test_list_kind_filter_agent_cost_matches_cost_bearing_row(
        self,
        integ_store: Store,
    ) -> None:
        """``agent-cost gt 0`` must match a row with a positive cost.

        Cost lives at ``marginal_cost_agent_usd`` on the asyncpg row
        and ``marginal_cost.agent_usd`` on the JSON-tagged view.
        :func:`_filter_value` handles both shapes; this test pins
        that the wire round-trip flows through the store correctly.
        """
        priced_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="costs money"),
        )
        await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="free"),
        )
        await integ_store.add_cost(
            priced_id,
            Cost(agent_usd=0.5),
            actor="alice",
        )

        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="agent-cost", op="gt", value="0.1"),),
        )
        assert [r.id for r in rows] == [priced_id], [
            (r.id, r.marginal_cost) for r in rows
        ]
        # Same row excluded under a tighter lower bound -- proves the
        # comparison reads the actual cost, not a constant.
        rows = await integ_store.list_kind(
            "Issue",
            filters=(Filter(field="agent-cost", op="gt", value="5"),),
        )
        assert [r.id for r in rows] == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
