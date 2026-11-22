"""Purges and the alerts a change cascades against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.server.store.store_test_support import append_user_message
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Belief
from trackinizer.wire.bodies import (
    Citation,
    SubmitAgentSession,
    SubmitBelief,
    SubmitExperiment,
    SubmitIssue,
)


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Purges and the alerts a change cascades, each read back from the database."""

    async def test_session_records_cascade_on_session_purge(
        self,
        integ_store: Store,
    ) -> None:
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="claude"),
            audience="open",
        )
        await append_user_message(integ_store, sid, idx=0, text="captured")
        await integ_store.purge(sid, actor="user")
        # FK ON DELETE CASCADE drops the records with the Session row.
        assert await integ_store.read_session_records(sid, part=0) == []

    async def test_child_change_alerts_parent_without_flipping_judgement(
        self,
        integ_store: Store,
    ) -> None:
        exp_id = await integ_store.submit_experiment(
            SubmitExperiment(
                account="tester@example.com",
                title="positive measurement",
                codechanges=[],
                outcome="ok",
            ),
        )
        belief_id = await integ_store.submit_belief(
            SubmitBelief(
                account="tester@example.com",
                title="system works",
                judgement="proven",
                subscribers=["alice"],
                proved_by=[
                    Citation(
                        artifact_id=exp_id,
                        artifact_kind="Experiment",
                        valence=1.0,
                    ),
                ],
            ),
        )
        await integ_store.set_status(exp_id, "complete", actor="user")
        belief = await integ_store.get_inquiry(belief_id)
        assert isinstance(belief, Belief)
        assert belief.judgement == "proven"
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM change_log WHERE subject_id = $1 "
                "AND kind = 'dependency_changed'",
                belief_id,
            )
        assert row is not None
        assert row["new_peer_id"] == exp_id
        assert row["actor"] == "librarian"
        assert row["subscribers_snapshot"] == ["alice"]

    async def test_purge_emits_edge_removed_for_peers(
        self,
        integ_store: Store,
    ) -> None:
        blocked_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="blocked"),
        )
        blocker_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="blocker"),
        )
        # Requires is stored from=requirer, to=prerequisite: the blocked issue
        # requires the blocker (prerequisite).
        await integ_store.add_edge(
            from_id=blocked_id,
            to_id=blocker_id,
            edge_kind="requires",
            actor="user",
        )
        await integ_store.purge(blocker_id, actor="user")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT old_peer_id, old_peer_edge_kind FROM change_log "
                "WHERE subject_id = $1 AND kind = 'edge_removed'",
                blocked_id,
            )
        assert row is not None
        assert row["old_peer_id"] == blocker_id
        assert row["old_peer_edge_kind"] == "requires"

    async def test_narrower_change_alerts_narrows(
        self,
        integ_store: Store,
    ) -> None:
        """The cascade walks narrower -> broader along ``narrows``.

        Regression: the cascade was inverted for ``narrows``, so a
        narrower Issue changing never alerted its broader parent.
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
        await integ_store.set_status(narrower_id, "complete", actor="user")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM change_log "
                "WHERE subject_id = $1 AND kind = 'dependency_changed'",
                broader_id,
            )
        assert row is not None
        assert row["new_peer_id"] == narrower_id
        assert row["new_peer_edge_kind"] == "narrows"
        assert row["subscribers_snapshot"] == ["alice"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestClientChangeIdReplay:
    """A client change id replays the edit it named, and only that edit."""

    async def test_same_id_same_operation_returns_original_outcome(
        self,
        integ_store: Store,
    ) -> None:
        issue_id = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="i"),
        )
        client_id = uuid.uuid4()
        # First edit: client_id becomes change_log.id.
        set_client_change_id(client_id)
        await integ_store.set_priority(issue_id, 1, actor="user")
        async with integ_store.engine.acquire() as conn:
            rows_before = await conn.fetch(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = $2",
                issue_id,
                "issue_priority",
            )
        assert len(rows_before) == 1
        assert rows_before[0]["id"] == client_id

        # Retry with same client_id: middleware would set it again. The
        # second call must collide on the PK, the savepoint rolls back
        # the cost UPDATE, and emit_change returns without writing a
        # second change_log row or double-charging cost.
        set_client_change_id(client_id)
        await integ_store.set_priority(issue_id, 1, actor="user")
        async with integ_store.engine.acquire() as conn:
            rows_after = await conn.fetch(
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = $2",
                issue_id,
                "issue_priority",
            )
        assert len(rows_after) == 1, "retry must replay, not double-write to change_log"

    async def test_same_id_different_operation_raises_conflict(
        self,
        integ_store: Store,
    ) -> None:
        issue_a = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="a"),
        )
        issue_b = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="b"),
        )
        client_id = uuid.uuid4()

        set_client_change_id(client_id)
        await integ_store.set_priority(issue_a, 1, actor="user")

        # Same client_id, different subject -> the row in change_log is
        # for issue_a/'priority' but the new request would write
        # one for issue_b. ConflictError is the right answer (client bug
        # or attempted UUID-reuse, not a legitimate retry).
        set_client_change_id(client_id)
        with pytest.raises(ConflictError, match="idempotency_key"):
            await integ_store.set_priority(issue_b, 1, actor="user")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
