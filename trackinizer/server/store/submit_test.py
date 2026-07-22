"""Tests for ``Store`` submit paths -- submit_*, submit_batch, idempotency."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import json

import asyncpg
import pytest

from trackinizer.conftest import (
    FakeEngine,
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    queue_field_rows,
)
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.shared import EMBEDDING_DIM
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.bodies import (
    Citation,
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBelief,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
)


class TestSubmit:
    @pytest.mark.asyncio
    async def test_submit_issue_notifies(self) -> None:
        store, engine = make_store()
        issue_id = await store.submit_issue(
            SubmitIssue(title="t", account="tester@example.com")
        )
        assert len(engine.notify_calls) == 1
        ch, payload = engine.notify_calls[0]
        assert ch == NOTIFY_CHANNEL
        decoded = json.loads(payload)
        assert decoded == {"id": str(issue_id)}

    @pytest.mark.asyncio
    async def test_submit_idempotency_change_log_race_returns_winner(self) -> None:
        """Racer that loses the change_log PK race re-probes and returns
        the winner's server-minted inquiry id.

        Walk: pre-probe sees no row -> insert_inquiry succeeds ->
        emit_change attempts change_log INSERT and gets UniqueViolation
        (a concurrent winner committed first) -> the outer ``tx()``
        rolls the racer's row back -> ``_submit_inquiry`` re-probes and
        returns the winner's subject_id from the existing change_log row.

        Queued fetchrows (cost UPDATEs are auto-synthesized):
          1. pre-probe ``_lookup_existing_by_change``: no row.
          2. inside ``emit_change``'s catch: SELECT actor/subject_id/kind
             of the colliding change_log row.
          3. post-rollback re-probe via ``_lookup_existing_by_change``.
        """
        winner_subject_id = new_uuid()
        idempotency_key = new_uuid()
        conn = make_conn()
        queue_field_rows(
            conn,
            None,  # 1: pre-probe -- key is free at this snapshot
            {  # 2: change_log re-read inside emit_change catch
                "actor": "system",
                "subject_id": winner_subject_id,
                "kind": "created",
                "subscribers_snapshot": None,
            },
            {  # 3: post-rollback re-probe surfaces winner
                "subject_id": winner_subject_id,
                "change_kind": "created",
                "subject_kind": "Issue",
            },
        )

        async def execute(sql: str, *args: object) -> str:
            del args
            if "INSERT INTO change_log" in sql:
                exc = asyncpg.UniqueViolationError(
                    "duplicate key value violates unique constraint"
                )
                # The recovery path classifies by constraint: this simulates a
                # change_log PK collision (the idempotency-replay case).
                exc.constraint_name = "change_log_pkey"
                raise exc
            return "OK"

        conn.execute.side_effect = execute
        store, _engine = make_store(conn)
        issue_id = await store.submit_issue(
            SubmitIssue(
                title="t",
                idempotency_key=idempotency_key,
                account="tester@example.com",
            )
        )
        assert issue_id == winner_subject_id

    @pytest.mark.asyncio
    async def test_live_owner_violation_reraises_not_replay(self) -> None:
        """A live-owner unique violation is re-raised, not masqueraded as replay.

        The recovery path classifies by ``constraint_name``: only a
        ``change_log_pkey`` collision is an idempotency replay. A
        ``uq_inquiries_live_session_owner`` violation (two live sessions for one
        routing name) must propagate so ``start_session`` re-reserves -- not be
        swallowed by a re-probe that happens to find no change_log row.
        """
        idempotency_key = new_uuid()
        conn = make_conn()
        queue_field_rows(conn, None)  # pre-probe: key is free.

        async def execute(sql: str, *args: object) -> str:
            del args
            if "INSERT INTO inquiries" in sql:
                exc = asyncpg.UniqueViolationError(
                    "duplicate key value violates unique constraint"
                )
                # A live routing-name collision, NOT a change_log PK replay.
                exc.constraint_name = "uq_inquiries_live_session_owner"
                raise exc
            return "OK"

        conn.execute.side_effect = execute
        store, _engine = make_store(conn)
        with pytest.raises(asyncpg.UniqueViolationError) as caught:
            await store.submit_agentsession(
                SubmitAgentSession(
                    title="s",
                    cli="codex",
                    owner="alice",
                    idempotency_key=idempotency_key,
                    account="tester@example.com",
                )
            )
        assert caught.value.constraint_name == "uq_inquiries_live_session_owner"

    @pytest.mark.asyncio
    async def test_submit_issue_inserts_requires_edges(self) -> None:
        prerequisite_id = new_uuid()
        conn = make_conn()
        # insert_edge looks up the prerequisite's kind before inserting.
        # It then checks cycles and uses RETURNING for insertion. The edge is the
        # only one between the pair (the mock's ``fetch`` returns no other edge
        # kinds), so no ``produced_by`` is inferred -- this test isolates the
        # requires edge (this issue -> its prerequisite), not provenance.
        conn.fetchval.side_effect = ["Issue", False, new_uuid()]
        store, _engine = make_store(conn)
        await store.submit_issue(
            SubmitIssue(
                title="t",
                requires=[prerequisite_id],
                account="tester@example.com",
            )
        )
        # Edge INSERT now uses fetchval (RETURNING); covered via fetchval calls.
        assert any(
            "INSERT INTO edges" in c.args[0] for c in conn.fetchval.call_args_list
        )

    @pytest.mark.asyncio
    async def test_submit_belief_wires_citations_without_recomputing(self) -> None:
        evidence_id = new_uuid()
        conn = make_conn()
        # cite_peer_as_from citations look up the cited artifact's kind first
        # (peer lookup -> "Experiment"), then insert_edge looks up the stored
        # to-side (the belief -> "Belief"), runs the cycle check (False), and
        # INSERTs RETURNING from_id.
        conn.fetchval.side_effect = ["Experiment", "Belief", False, new_uuid()]
        store, _engine = make_store(conn)
        await store.submit_belief(
            SubmitBelief(
                title="c",
                proved_by=[
                    Citation(
                        artifact_id=evidence_id, artifact_kind="Experiment", valence=0.8
                    )
                ],
                account="tester@example.com",
            )
        )
        sqls = executed_sql(conn)
        edge_inserts = [
            c for c in conn.fetchval.call_args_list if "INSERT INTO edges" in c.args[0]
        ]
        assert edge_inserts
        # Belief citations now store as Artifact -> Belief (cite_peer_as_from):
        # the citing artifact is the `from_id`, the belief it bears on is the
        # `to_id`, edge_kind is `proves`. For-vs-against is the sign of valence.
        # primitives.insert_edge SQL parameter order:
        # (from_id, from_kind, to_id, to_kind, edge_kind, priority,
        #  note, valence, labels). call.args is (sql, $1, $2, ...).
        insert_args = edge_inserts[0].args
        assert insert_args[1] == evidence_id  # $1 = from_id (the citing artifact)
        assert insert_args[2] == "Experiment"  # $2 = from_kind
        assert insert_args[5] == "proves"  # $5 = edge_kind
        assert insert_args[8] == 0.8  # $8 = valence (signed citation weight)
        assert not any("UPDATE inquiries SET judgement" in sql for sql in sqls)

    @pytest.mark.asyncio
    async def test_submit_experiment_validates_codechanges_before_insert(self) -> None:
        # An invalid codechange ref must fail BEFORE the row is inserted and
        # embedded: validation is a pre_insert gate, so a bad ref wastes no
        # embed work and writes no inquiries row.
        bad_ref = new_uuid()
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])  # lookup_kinds: ref not found.
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError, match="not found"):
            await store.submit_experiment(
                SubmitExperiment(title="e", codechanges=[bad_ref])
            )
        sqls = executed_sql(conn)
        assert not any("INSERT INTO inquiries" in s for s in sqls)
        assert not any("INSERT INTO inquiry_embeddings" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_submit_belief_inserts_and_logs(self) -> None:
        store, engine = make_store()
        await store.submit_belief(SubmitBelief(title="C", account="tester@example.com"))
        sqls = executed_sql(engine.conn)
        assert any("INSERT INTO inquiries" in s for s in sqls)
        assert any("INSERT INTO change_log" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_submit_without_account_raises(self) -> None:
        """The Store requires a route-resolved account; it invents no fallback.

        An absent account is a caller that skipped resolution, not a state to
        paper over with the audit ``actor`` -- so the submit raises rather than
        silently stamping an unvalidated (or spoofable) value.
        """
        store, engine = make_store()
        with pytest.raises(ValueError, match="requires a resolved account"):
            await store.submit_issue(SubmitIssue(title="t"))
        # No row was written: the guard fires before the insert.
        assert not any("INSERT INTO inquiries" in s for s in executed_sql(engine.conn))

    @pytest.mark.asyncio
    async def test_submit_paper_authors_preserve_dups_matching_edit(self) -> None:
        # Submit and edit must share one author contract: strip per element,
        # drop blanks, preserve order + duplicates. The submit path used
        # canonical_strs (which dedups), disagreeing with set_authors.
        conn = make_conn()
        store, _engine = make_store(conn)
        await store.submit_paper(
            SubmitPaper(
                title="p",
                authors=[" Ada ", "Ada", "", "Grace"],
                account="tester@example.com",
            )
        )
        insert = next(
            c
            for c in conn.execute.call_args_list
            if c.args and "INSERT INTO inquiries" in c.args[0]
        )
        # Locate the paper_authors bind by its column position rather than a
        # hardcoded index: insert_inquiry derives the column order from
        # COLUMN_SPECS, so the offset is (args[0]=SQL, then row_id, kind, status,
        # *derived) -- find paper_authors within the SQL's derived column list.
        sql = "".join(a for a in insert.args if isinstance(a, str))
        cols = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        derived = cols[cols.index("status") + 1 :]  # columns after status
        # binds: args[0]=SQL, [1]=row_id, [2]=kind, [3]=status, [4:]=derived
        authors = insert.args[4 + derived.index("paper_authors")]
        # byline_strs preserves order + duplicates (unlike canonical_strs dedup),
        # matching set_authors. Stored as tuple/list -- both bind to TEXT[].
        assert list(authors) == ["Ada", "Ada", "Grace"]


class TestIdempotentShortCircuitConsumesChangeId:
    """Idempotent submit short-circuit must consume the Idempotency-Key slot.

    Failure mode: a batch retry shares one ``Idempotency-Key`` across
    multiple submits. Item A's idempotent short-circuit returns the
    cached row without entering ``emit_change``; the contextvar slot
    still holds the client UUID. Item B then calls ``emit_change``,
    consumes the leftover UUID, and either collides on
    ``change_log.id`` (UniqueViolation) or quietly mis-attributes the
    second mutation to A's logical change id.
    """

    @pytest.mark.asyncio
    async def test_pre_probe_replay_does_not_leak_key_forward(self) -> None:
        """A submit whose pre-probe replays must not leak its
        idempotency_key into the next submit's ``emit_change``.

        Walk: submit_issue replays via the pre-probe (no emit_change
        runs); a follow-up submit_artifact with no key inherits a fresh
        server-minted change_log.id, not the prior submit's key.
        """
        conn = make_conn()
        winner_subject_id = new_uuid()
        prior_key = new_uuid()
        queue_field_rows(
            conn,
            # 1: pre-probe for submit_issue: key already used; replay.
            {
                "subject_id": winner_subject_id,
                "change_kind": "created",
                "subject_kind": "Issue",
            },
        )
        store, _engine = make_store(conn)
        returned = await store.submit_issue(
            SubmitIssue(title="a", idempotency_key=prior_key)
        )
        assert returned == winner_subject_id
        await store.submit_artifact(
            SubmitArtifact(title="b", account="tester@example.com")
        )
        change_ids = [
            call.args[1]  # ``id`` is the first column in ``emit_change``.
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        # Only the artifact's ``created`` emit_change ran; its
        # change_log.id must NOT be the prior submit's key.
        assert len(change_ids) == 1
        assert change_ids[0] != prior_key, (
            f"submit_artifact reused the prior submit's idempotency_key "
            f"{prior_key}; pre-probe replay failed to clear the contextvar slot"
        )

    @pytest.mark.asyncio
    async def test_pre_probe_replay_clears_externally_set_slot(self) -> None:
        """A pre-probe replay must clear any slot the *caller* set.

        The route layer's ``Idempotency-Key`` middleware can set the slot
        before the submit body's ``idempotency_key`` is parsed. A
        pre-probe early-return must not leak that external value to
        the next ``emit_change`` in the same task. Reproduces the
        leak: pre-probe replay; follow-up submit_artifact (no key)
        must NOT pick up the externally-set UUID.
        """
        conn = make_conn()
        winner_subject_id = new_uuid()
        external_key = new_uuid()  # whatever was set before the submit ran
        replay_key = new_uuid()
        queue_field_rows(
            conn,
            # 1: pre-probe for submit_issue: key already used; replay.
            {
                "subject_id": winner_subject_id,
                "change_kind": "created",
                "subject_kind": "Issue",
            },
        )
        store, _engine = make_store(conn)
        set_client_change_id(external_key)
        returned = await store.submit_issue(
            SubmitIssue(title="a", idempotency_key=replay_key)
        )
        assert returned == winner_subject_id
        await store.submit_artifact(
            SubmitArtifact(title="b", account="tester@example.com")
        )
        change_ids = [
            call.args[1]
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        assert len(change_ids) == 1
        assert change_ids[0] != external_key, (
            f"submit_artifact inherited the externally-set slot value "
            f"{external_key}; pre-probe early-return failed to clear it"
        )

    @pytest.mark.asyncio
    async def test_change_log_race_replay_does_not_leak_key_forward(self) -> None:
        """The change_log-collision recovery path must also clear the slot.

        Walk: submit_issue's pre-probe misses, insert runs, change_log
        INSERT collides on the racer's idempotency_key,
        ``_submit_inquiry`` re-probes and returns the winner;
        a follow-up submit_artifact must get a fresh change_log.id.
        """
        conn = make_conn()
        winner_subject_id = new_uuid()
        racer_key = new_uuid()
        queue_field_rows(
            conn,
            None,  # pre-probe: key looks free
            {  # emit_change re-reads change_log on UniqueViolation
                "actor": "system",
                "subject_id": winner_subject_id,
                "kind": "created",
                "subscribers_snapshot": None,
            },
            {  # post-rollback re-probe surfaces winner
                "subject_id": winner_subject_id,
                "change_kind": "created",
                "subject_kind": "Issue",
            },
        )

        raised: list[bool] = []

        async def execute(sql: str, *args: object) -> str:
            del args
            if "INSERT INTO change_log" in sql and not raised:
                raised.append(True)
                exc = asyncpg.UniqueViolationError(
                    "duplicate key value violates unique constraint"
                )
                # Classified by constraint: a change_log PK collision (replay).
                exc.constraint_name = "change_log_pkey"
                raise exc
            return "OK"

        conn.execute.side_effect = execute
        store, _engine = make_store(conn)
        returned = await store.submit_issue(
            SubmitIssue(
                title="a", idempotency_key=racer_key, account="tester@example.com"
            )
        )
        assert returned == winner_subject_id
        await store.submit_artifact(
            SubmitArtifact(title="b", account="tester@example.com")
        )
        change_ids = [
            call.args[1]
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        # First INSERT raised; second (the artifact's) succeeded. Its
        # change_log.id must NOT be the racer's key.
        assert len(change_ids) == 2
        assert change_ids[1] != racer_key, (
            f"submit_artifact reused the racer's idempotency_key "
            f"{racer_key}; race-recovery path failed to clear the contextvar slot"
        )


class TestSubmitExceptionDrainsHeaderSlot:
    """A submit that raises must drain any externally-set Idempotency-Key slot.

    Failure mode (F30): the route layer's ``Idempotency-Key`` middleware
    sets the slot before the submit body is parsed, so on the header-only
    path ``req.idempotency_key`` is ``None``. If ``insert_inquiry`` raises
    before ``emit_change`` consumes the slot, the old ``finally`` only
    cleared when ``req.idempotency_key is not None`` -- so the externally
    set UUID leaks. The next submit in the same task picks it up via the
    peek/consume, conflating two submits' logical change identity.
    """

    @pytest.mark.asyncio
    async def test_header_only_slot_drained_on_insert_failure(self) -> None:
        """An exception on the header-only path must leave the slot empty.

        Walk: route middleware set the slot (body key ``None``);
        ``insert_inquiry`` raises before ``emit_change`` consumes it; the
        submit propagates the error and the slot must read ``None``
        afterward (it currently leaks the externally-set key).
        """
        header_key = new_uuid()
        conn = make_conn()
        queue_field_rows(conn, None)  # pre-probe: header key looks free.

        async def execute(sql: str, *args: object) -> str:
            del args
            if "INSERT INTO inquiries" in sql:
                raise RuntimeError("boom")
            return "OK"

        conn.execute.side_effect = execute
        store, _engine = make_store(conn)
        set_client_change_id(header_key)
        with pytest.raises(RuntimeError, match="boom"):
            await store.submit_issue(
                SubmitIssue(title="t", account="tester@example.com")
            )
        assert _peek_client_change_id() is None, (
            f"submit left the externally-set Idempotency-Key {header_key} in "
            f"the slot after raising; it will leak into the next submit's "
            f"emit_change"
        )


class _BeginRecordingEmbedder:
    """Embedder that records whether ``BEGIN`` ran on ``conn`` at embed time.

    The single-submit path must embed BEFORE opening the transaction (F56),
    matching ``set_title``: a network embedder held inside the tx pins the
    single PGlite connection across the round-trip, serializing every writer.
    """

    name = "begin-recorder"
    dim = EMBEDDING_DIM

    def __init__(self, conn: AsyncMock) -> None:
        self._conn = conn
        self.began_before_embed: list[bool] = []

    async def embed(self, text: str) -> list[float]:
        del text
        self.began_before_embed.append(
            any(
                call.args and call.args[0] == "BEGIN"
                for call in self._conn.execute.call_args_list
            )
        )
        return [0.0] * self.dim


class TestSubmitEmbedsBeforeTx:
    """Single-submit embeds before the owned tx (F56 lock-contention fix)."""

    @pytest.mark.asyncio
    async def test_single_submit_embeds_before_begin(self) -> None:
        """``submit_issue`` must embed before issuing ``BEGIN`` on its conn.

        Embedding inside the tx pins the single PGlite connection across the
        embedder round-trip; ``set_title`` already embeds first, and submit
        must match so a submit doesn't block every other writer.
        """
        conn = make_conn()
        embedder = _BeginRecordingEmbedder(conn)
        store = Store(cast(DatabaseEngine, FakeEngine(conn)), embed=embedder)
        await store.submit_issue(SubmitIssue(title="t", account="tester@example.com"))
        assert embedder.began_before_embed == [False], (
            "submit embedded after BEGIN -- the embedder round-trip holds the "
            "single PGlite connection across the tx, serializing writers"
        )


def _tx_verbs(conn: AsyncMock) -> list[str]:
    """The BEGIN/COMMIT/ROLLBACK verbs issued on ``conn``, in order.

    Sourced from :func:`executed_sql`, which spans both ``execute`` and
    ``fetch``: ``tx()`` issues the error-path ``ROLLBACK`` over the extended
    protocol (``fetch``) to dodge a pglite 0.5 simple-query mis-frame.
    """
    return [s for s in executed_sql(conn) if s in ("BEGIN", "COMMIT", "ROLLBACK")]


class TestSubmitBatch:
    """``submit_batch`` is all-or-nothing in one shared transaction."""

    @pytest.mark.asyncio
    async def test_commits_all_items_in_one_transaction(self) -> None:
        """A successful batch opens one tx, commits once, returns ids in order."""
        conn = make_conn()
        store, _engine = make_store(conn)
        ids = await store.submit_batch(
            [
                SubmitIssue(
                    title="i1",
                    idempotency_key=new_uuid(),
                    account="tester@example.com",
                ),
                SubmitArtifact(
                    title="a1",
                    idempotency_key=new_uuid(),
                    account="tester@example.com",
                ),
            ]
        )
        assert len(ids) == 2
        verbs = _tx_verbs(conn)
        assert verbs.count("BEGIN") == 1
        assert verbs.count("COMMIT") == 1
        assert "ROLLBACK" not in verbs

    @pytest.mark.asyncio
    async def test_failure_rolls_back_whole_batch(self) -> None:
        """An item failure rolls the single shared transaction back entirely."""
        conn = make_conn()
        inserts = {"n": 0}
        base = conn.execute.side_effect

        async def execute(sql: str, *args: object) -> object:
            if "INSERT INTO inquiries" in sql:
                inserts["n"] += 1
                if inserts["n"] == 2:
                    raise RuntimeError("boom")
            return await base(sql, *args) if base is not None else "OK"

        conn.execute.side_effect = execute
        store, _engine = make_store(conn)
        with pytest.raises(RuntimeError, match="boom"):
            await store.submit_batch(
                [
                    SubmitIssue(
                        title="i1",
                        idempotency_key=new_uuid(),
                        account="tester@example.com",
                    ),
                    SubmitArtifact(
                        title="a1",
                        idempotency_key=new_uuid(),
                        account="tester@example.com",
                    ),
                ]
            )
        verbs = _tx_verbs(conn)
        assert verbs.count("BEGIN") == 1
        assert verbs.count("ROLLBACK") == 1
        assert "COMMIT" not in verbs

    @pytest.mark.asyncio
    async def test_probes_reuse_held_connection_without_reentrant_acquire(
        self,
    ) -> None:
        """Per-item idempotency probes run on the held conn, never re-acquire.

        ``FakeEngine.acquire`` rejects a reentrant acquire (modeling the
        single-connection PGlite substrate). A batch whose per-item probes
        run inside the shared transaction therefore completes only if those
        probes reuse the held connection -- the regression that 500'd a
        live create when the probe re-acquired.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        # Completing at all is the assertion: a reentrant acquire would have
        # raised inside the batch. Confirm the whole batch ran under exactly
        # one acquire by counting its single BEGIN/COMMIT pair.
        ids = await store.submit_batch(
            [
                SubmitIssue(
                    title="i1",
                    idempotency_key=new_uuid(),
                    account="tester@example.com",
                ),
                SubmitArtifact(
                    title="a1",
                    idempotency_key=new_uuid(),
                    account="tester@example.com",
                ),
            ]
        )
        assert len(ids) == 2
        verbs = _tx_verbs(conn)
        assert verbs == ["BEGIN", "COMMIT"]

    # Edge wiring (index/UUID resolution + atomicity with rows) is proven
    # against real Postgres in integration_test.py
    # ``test_submit_batch_creates_rows_and_edge_atomically`` -- the mock conn
    # cannot faithfully drive insert_edge's cycle-check / ON CONFLICT path.

    @pytest.mark.asyncio
    async def test_retried_item_short_circuits_without_reinsert(self) -> None:
        """An item whose key already exists returns the prior id, no insert."""
        conn = make_conn()
        winner = new_uuid()
        key = new_uuid()
        queue_field_rows(
            conn,
            {"subject_id": winner, "change_kind": "created", "subject_kind": "Issue"},
        )
        store, _engine = make_store(conn)
        ids = await store.submit_batch([SubmitIssue(title="i1", idempotency_key=key)])
        assert ids == [winner]
        assert not any(
            "INSERT INTO inquiries" in call.args[0]
            for call in conn.execute.call_args_list
            if call.args
        )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
