"""Tests for ``Store`` construction, embedders, bootstrap, and pure helpers."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import asyncio

import asyncpg
import pytest

from trackinizer.conftest import (
    FakeEngine,
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
)
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.store.core import (
    Store,
    StubEmbedder,
    _xorshift_floats,
)
from trackinizer.server.store.shared import EMBEDDING_DIM
from trackinizer.wire.bodies import SubmitIssue


class TestPureFunctions:
    def test_xorshift_floats_deterministic(self) -> None:
        a = _xorshift_floats(42)
        b = _xorshift_floats(42)
        assert [next(a) for _ in range(5)] == [next(b) for _ in range(5)]

    def test_xorshift_floats_in_range(self) -> None:
        rng = _xorshift_floats(1)
        for _ in range(50):
            v = next(rng)
            assert -1.0 <= v <= 1.0


class TestStubEmbedder:
    @pytest.mark.asyncio
    async def test_embed_unit_norm_correct_dim(self) -> None:
        emb = StubEmbedder()
        v = await emb.embed("hello")
        assert len(v) == EMBEDDING_DIM
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-9

    @pytest.mark.asyncio
    async def test_embed_deterministic(self) -> None:
        emb = StubEmbedder()
        assert await emb.embed("foo") == await emb.embed("foo")
        assert await emb.embed("foo") != await emb.embed("bar")


class _NamedStub(StubEmbedder):
    """StubEmbedder variant with a configurable ``name`` so multiple
    embedders coexist per ``inquiry_embeddings`` PK ``(inquiry_id, model)``.
    """

    def __init__(self, name: str) -> None:
        self.name = name


class TestStoreEmbedders:
    def test_single_embedder_normalized_to_tuple(self) -> None:
        store, _ = make_store()
        assert len(store.embedders) == 1
        assert store.embedders[0].name == "stub"

    def test_sequence_of_embedders_preserved(self) -> None:
        engine = FakeEngine()
        store = Store(
            cast(DatabaseEngine, engine),
            embed=[_NamedStub("a"), _NamedStub("b")],
        )
        assert [e.name for e in store.embedders] == ["a", "b"]

    def test_empty_embedders_rejected(self) -> None:
        engine = FakeEngine()
        with pytest.raises(ValueError, match="at least one embedder"):
            Store(cast(DatabaseEngine, engine), embed=[])

    def test_duplicate_embedder_names_rejected(self) -> None:
        engine = FakeEngine()
        with pytest.raises(ValueError, match=r"duplicates.*'dup'"):
            Store(
                cast(DatabaseEngine, engine),
                embed=[_NamedStub("dup"), _NamedStub("dup")],
            )

    def test_dim_mismatch_rejected(self) -> None:
        class _WrongDim:
            name = "wrong"
            dim = 17

            async def embed(self, text: str) -> list[float]:
                del text
                return [0.0] * self.dim

        engine = FakeEngine()
        with pytest.raises(ValueError, match=r"dim=384"):
            Store(cast(DatabaseEngine, engine), embed=[_NamedStub("ok"), _WrongDim()])

    @pytest.mark.asyncio
    async def test_submit_writes_one_embedding_row_per_embedder(self) -> None:
        conn = make_conn()
        engine = FakeEngine(conn)
        store = Store(
            cast(DatabaseEngine, engine),
            embed=[_NamedStub("a"), _NamedStub("b")],
        )
        await store.submit_issue(SubmitIssue(title="t", account="tester@example.com"))
        emb_upserts = [
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO inquiry_embeddings" in c.args[0]
        ]
        assert len(emb_upserts) == 2
        assert {c.args[2] for c in emb_upserts} == {"a", "b"}
        # Upsert (re-runs on title edit) implies ON CONFLICT clause.
        assert all("ON CONFLICT" in c.args[0] for c in emb_upserts)

    @pytest.mark.asyncio
    async def test_embed_all_runs_concurrently(self) -> None:
        """``_embed_all`` issues every ``embed`` call before awaiting any.

        The previous sequential ``[await e.embed(...) for ...]`` form
        scaled latency as sum-of-embed-times. The current implementation
        uses ``asyncio.gather`` -- proven here by gating each embedder
        on a shared :class:`asyncio.Event` and asserting all stalls
        materialize before any vector returns.
        """
        gate = asyncio.Event()
        stall_counter = {"n": 0}

        class _GatedEmbedder:
            def __init__(self, name: str) -> None:
                self.name = name
                self.dim = EMBEDDING_DIM

            async def embed(self, text: str) -> list[float]:
                del text
                stall_counter["n"] += 1
                await gate.wait()
                return [0.0] * self.dim

        engine = FakeEngine()
        store = Store(
            cast(DatabaseEngine, engine),
            embed=[_GatedEmbedder("a"), _GatedEmbedder("b"), _GatedEmbedder("c")],
        )
        task = asyncio.create_task(store._embed_all("x"))
        # Yield until every embedder is stalled inside ``embed``. Under
        # the old serial form, only one would be stalled at a time.
        for _ in range(20):
            await asyncio.sleep(0)
            if stall_counter["n"] == 3:
                break
        assert stall_counter["n"] == 3, (
            f"expected 3 concurrent stalls, got {stall_counter['n']}; "
            "_embed_all is not awaiting in parallel"
        )
        gate.set()
        result = await task
        assert [name for name, _ in result] == ["a", "b", "c"]


class TestStoreBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_applies_fresh_baseline_and_marks_migrations(self) -> None:
        store, engine = make_store()
        await store.bootstrap()
        sqls = executed_sql(engine.conn)
        assert any("applied_migrations" in s for s in sqls)
        assert any("CREATE TABLE IF NOT EXISTS inquiries" in s for s in sqls)
        assert not any("UPDATE inquiries SET kind = 'Belief'" in s for s in sqls)
        assert not any("{change_log_mirror}" in s for s in sqls)
        inserts = [
            c.args[1]
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO applied_migrations" in c.args[0]
        ]
        # A fresh database records the baseline. The schema is squashed to a
        # single baseline, so there are no numbered migrations to mark.
        assert inserts == ["schema.sql"]

    @pytest.mark.asyncio
    async def test_bootstrap_skips_baseline_for_existing_db(self) -> None:
        conn = make_conn()

        # Only the applied_migrations probe reports a prior schema; the
        # embedding-backfill scan keeps the default empty result.
        async def fetch(sql: str, *_args: object) -> list[dict[str, str]]:
            return [{"name": "schema.sql"}] if "applied_migrations" in sql else []

        conn.fetch.side_effect = fetch
        store, engine = make_store(conn)
        await store.bootstrap()
        sqls = executed_sql(engine.conn)
        assert not any("CREATE TABLE IF NOT EXISTS inquiries" in s for s in sqls)
        inserts = [
            c.args[1]
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO applied_migrations" in c.args[0]
        ]
        # The baseline is already applied and the schema is squashed to a single
        # baseline, so there are no numbered migrations left to run or record.
        assert inserts == []

    @pytest.mark.asyncio
    async def test_bootstrap_partial_state_does_not_recreate_baseline(
        self,
    ) -> None:
        """Partial/manual state: ``inquiries`` exists but the ledger is empty.

        Someone loaded the current baseline ``schema.sql`` by hand, or a
        SIGKILL landed between the baseline DDL and the ledger INSERTs.
        ``is_fresh_database`` (the ``to_regclass('public.inquiries')`` probe)
        reports not-fresh, so bootstrap must NOT re-create the baseline
        (``CREATE TABLE`` against existing tables would error). It records the
        baseline so a later deploy never replays it.
        """
        conn = make_conn()
        # Ledger empty; ``inquiries`` already exists (manual/partial setup).
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value="public.inquiries")
        store, engine = make_store(conn)
        await store.bootstrap()
        sqls = executed_sql(engine.conn)
        # Not-fresh: the baseline schema is never re-created.
        assert not any("CREATE TABLE IF NOT EXISTS inquiries" in s for s in sqls)
        # Every migration ends up recorded so a later deploy never replays them.
        recorded = {
            c.args[1]
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO applied_migrations" in c.args[0]
        }
        assert recorded == {"schema.sql"}

    @pytest.mark.asyncio
    async def test_bootstrap_reconciles_lagging_sequences(self) -> None:
        """A sequence behind its data is bumped via monotonic ``setval``."""
        conn = make_conn()
        conn.fetchval.return_value = 265
        store, engine = make_store(conn)
        await store.bootstrap()
        setvals = [
            c for c in engine.conn.execute.call_args_list if "setval" in c.args[0]
        ]
        # One reconcile per kind, each guarded by GREATEST so a healthy
        # sequence is never lowered, each carrying the table max as the floor.
        assert len(setvals) == 9
        assert all(
            "GREATEST" in c.args[0] and "last_value" in c.args[0] for c in setvals
        )
        assert all(c.args[1] == 265 for c in setvals)

    @pytest.mark.asyncio
    async def test_bootstrap_skips_sequence_reconcile_for_empty_kinds(self) -> None:
        """A kind with no rows leaves its sequence minting from the start."""
        store, engine = make_store()  # fetchval defaults to None (empty table).
        await store.bootstrap()
        assert not any(
            "setval" in c.args[0] for c in engine.conn.execute.call_args_list
        )

    @pytest.mark.asyncio
    async def test_bootstrap_backfills_missing_embeddings(self) -> None:
        """Inquiries lacking an embedding row are re-embedded at boot."""
        conn = make_conn()
        missing_id = new_uuid()

        # Only the NOT EXISTS backfill query returns rows; other fetches stay [].
        async def fetch(sql: str, *_args: object) -> list[dict[str, object]]:
            return (
                [{"id": missing_id, "title": "needs embedding"}]
                if "NOT EXISTS" in sql and "inquiry_embeddings" in sql
                else []
            )

        conn.fetch.side_effect = fetch
        store, engine = make_store(conn)
        await store.bootstrap()
        inserts = [
            c.args[1:]
            for c in engine.conn.execute.call_args_list
            if "INSERT INTO inquiry_embeddings" in c.args[0]
        ]
        assert len(inserts) == 1
        assert inserts[0][0] == missing_id  # inquiry_id positional arg.

    @pytest.mark.asyncio
    async def test_bootstrap_skips_backfill_when_fully_embedded(self) -> None:
        """No embedding inserts fire when every inquiry already has a row."""
        store, engine = make_store()  # fetch defaults to [] (nothing missing).
        await store.bootstrap()
        assert not any(
            "INSERT INTO inquiry_embeddings" in c.args[0]
            for c in engine.conn.execute.call_args_list
        )

    @pytest.mark.asyncio
    async def test_bootstrap_raises_when_schema_assets_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "trackinizer.server.store.core.schema_migrations",
            lambda: iter(()),
        )
        store, _engine = make_store()
        with pytest.raises(RuntimeError, match="schema_migrations"):
            await store.bootstrap()

    @pytest.mark.asyncio
    async def test_bootstrap_retries_transient_connection_death(self) -> None:
        """A connection dropped mid-bootstrap retries on a fresh acquire.

        Under whole-suite load the PGlite Node child can be starved off its
        socket mid-DDL, surfacing as ``ConnectionDoesNotExistError`` partway
        through the held ``acquire()`` block. Re-entering ``acquire()``
        reconnects, and the idempotent pass replays. Without the retry the first
        ``ConnectionDoesNotExistError`` propagates and bootstrap fails.
        """
        conn = make_conn()
        calls = {"n": 0}
        first_real_execute = conn.execute.side_effect

        async def flaky_execute(sql: str, *args: object) -> object:
            calls["n"] += 1
            # Die once, on the advisory-lock acquire opening the first pass,
            # mimicking the Node socket dropping under load. Later passes see a
            # healthy connection.
            if calls["n"] == 1:
                raise asyncpg.exceptions.ConnectionDoesNotExistError(
                    "connection was closed in the middle of operation"
                )
            if first_real_execute is not None:
                return await first_real_execute(sql, *args)
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=flaky_execute)
        store, engine = make_store(conn)

        await store.bootstrap()

        sqls = executed_sql(engine.conn)
        assert any("CREATE TABLE IF NOT EXISTS inquiries" in s for s in sqls)
        assert calls["n"] > 1

    @pytest.mark.asyncio
    async def test_bootstrap_does_not_retry_non_connection_error(self) -> None:
        """A deterministic DDL failure raises on the first pass, never retried.

        The retry is scoped to transient connection deaths; a real schema bug
        (here a generic ``PostgresError``) must surface immediately so a broken
        migration is not silently re-attempted into the same failure.
        """
        conn = make_conn()
        conn.execute = AsyncMock(
            side_effect=asyncpg.exceptions.SyntaxOrAccessError("bad DDL")
        )
        store, _engine = make_store(conn)

        with pytest.raises(asyncpg.exceptions.SyntaxOrAccessError):
            await store.bootstrap()
        assert conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_bootstrap_does_not_retry_missing_schema_asset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing schema asset is deterministic and surfaces on the first pass.

        ``schema_migrations`` reads SQL files; a missing one raises
        ``FileNotFoundError`` (an ``OSError``). The retry set is narrowed to
        ``ConnectionRefusedError``, so this is NOT retried -- otherwise a broken
        deploy would burn the whole backoff budget before failing.
        """
        calls = {"n": 0}

        def boom() -> object:
            calls["n"] += 1
            raise FileNotFoundError("schema.sql missing")

        monkeypatch.setattr("trackinizer.server.store.core.schema_migrations", boom)
        store, _engine = make_store()

        with pytest.raises(FileNotFoundError):
            await store.bootstrap()
        # Surfaced on the first pass -- not retried through the backoff budget.
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_bootstrap_does_not_retry_interface_misuse(self) -> None:
        """An asyncpg API-misuse ``InterfaceError`` surfaces, not retried.

        ``InterfaceError`` covers both the transient connection-closed death
        (retry) and deterministic API misuse like "another operation is in
        progress" (a bug -- must surface). Only the connection-closed phrasing
        is retried; this misuse message must raise on the first pass.
        """
        conn = make_conn()
        conn.execute = AsyncMock(
            side_effect=asyncpg.InterfaceError("another operation is in progress")
        )
        store, _engine = make_store(conn)

        with pytest.raises(asyncpg.InterfaceError):
            await store.bootstrap()
        assert conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_bootstrap_retries_connection_closed_interface_error(self) -> None:
        """A connection-closed ``InterfaceError`` is transient and retried.

        This is the real PGlite Node-death signal (asyncpg raises a plain
        ``InterfaceError('connection is closed')``), so it must still retry --
        narrowing by message must not lose the case the retry exists for.
        """
        conn = make_conn()
        calls = {"n": 0}
        first_real = conn.execute.side_effect

        async def flaky(sql: str, *args: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncpg.InterfaceError("connection is closed")
            if first_real is not None:
                return await first_real(sql, *args)
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=flaky)
        store, engine = make_store(conn)

        await store.bootstrap()

        assert calls["n"] > 1
        assert any(
            "CREATE TABLE IF NOT EXISTS inquiries" in s
            for s in executed_sql(engine.conn)
        )

    @pytest.mark.asyncio
    async def test_bootstrap_finally_unlock_preserves_original_cause(self) -> None:
        """A dead-connection unlock in ``finally`` must not mask the real error.

        When the body dies because the connection dropped, the
        ``pg_advisory_unlock`` in the ``finally`` raises the same family; without
        suppression that fresh exception replaces the original in the traceback.
        The retried ``bootstrap`` still catches it, so assert against
        ``_bootstrap_once`` directly to see the surfaced cause.
        """
        conn = make_conn()

        async def execute(sql: str, *_args: object) -> object:
            if "advisory_unlock" in sql:
                raise asyncpg.exceptions.InterfaceError("unlock on dead conn")
            if "applied_migrations" in sql and "CREATE" in sql:
                raise asyncpg.exceptions.ConnectionDoesNotExistError("DDL died first")
            return "OK"

        conn.execute = AsyncMock(side_effect=execute)
        store, _engine = make_store(conn)

        with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError) as exc:
            await store._bootstrap_once()
        assert "DDL died first" in str(exc.value)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
