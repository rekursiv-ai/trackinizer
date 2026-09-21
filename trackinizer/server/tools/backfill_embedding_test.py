"""Follow-mode: round 1 embeds the corpus, round 2 no-ops (the sweep predicate).

Drives ``_run_follow`` for exactly two rounds against pglite with a counting
wrapper over the 1024-dim stub, proving the steady-state contract: the first
round embeds a freshly inserted record, the second re-scans and embeds NOTHING
(``md5(text)`` already matches). Also checks the follow path ensures each model's
partial index at startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override
from uuid import UUID, uuid4

import multiprocessing

import pytest
import pytest_asyncio

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_embed import sweep_session_embeddings
from trackinizer.server.store.session_index import index_name_for
from trackinizer.server.tools import backfill_embedding


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from uuid import UUID

    from trackinizer.lib.postgres import PostgresEngine


class _StopFollowError(Exception):
    """Breaks the otherwise-infinite follow loop after a fixed round count."""


class _CountingStub(StubEmbedder):
    """A 1024-dim stub that counts how many texts it embedded per call."""

    def __init__(self) -> None:
        super().__init__(dim=1024)
        self.embedded: list[str] = []

    @override
    async def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        return await super().embed(text)


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Bootstrapped store with the session tables emptied."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with built.engine.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_embeddings, session_index_state, session_records, "
            "session_manifests, inquiries CASCADE",
        )
    yield built


async def _one_record(store: Store) -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'follow test')",
            session_id,
        )
        await conn.execute(
            "INSERT INTO session_records "
            "(session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, 0, 'UserMessage', '{}'::json, $2)",
            session_id,
            "a session line to embed exactly once across two rounds",
        )
        # The sweep reads only the live manifest prefix (idx < records), so the
        # record needs a manifest that covers it -- production writes both.
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', 1)",
            session_id,
        )
    return session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_follow_embeds_round_one_then_no_ops_round_two(
    store: Store,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 embeds the record; round 2 re-scans and embeds nothing."""
    await _one_record(store)
    counting = _CountingStub()

    def use_counting(name: str) -> _CountingStub:
        del name
        return counting

    monkeypatch.setattr(backfill_embedding, "_follow_embedder", use_counting)

    rounds = {"n": 0}

    async def stop_after_two(interval: float) -> None:
        del interval
        rounds["n"] += 1
        if rounds["n"] >= 2:
            raise _StopFollowError

    monkeypatch.setattr(
        "trackinizer.server.tools.backfill_embedding.asyncio.sleep",
        stop_after_two,
    )

    with pytest.raises(_StopFollowError):
        await backfill_embedding._run_follow(pg_dsn, ["stub-1024"], interval_sec=0)

    # The record embedded exactly once: round 1 wrote it, round 2's md5 matched
    # and skipped it. Two sleeps means two full rounds ran.
    assert counting.embedded == [
        "a session line to embed exactly once across two rounds",
    ]
    assert rounds["n"] == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_follow_ensures_the_partial_index(
    store: Store,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow ensures each model's partial HNSW index before sweeping."""
    async with store.engine.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS " + index_name_for("stub-1024"))

    async def stop_immediately(interval: float) -> None:
        del interval
        raise _StopFollowError

    def use_stub(name: str) -> StubEmbedder:
        del name
        return StubEmbedder(dim=1024)

    monkeypatch.setattr(
        "trackinizer.server.tools.backfill_embedding.asyncio.sleep",
        stop_immediately,
    )
    monkeypatch.setattr(backfill_embedding, "_follow_embedder", use_stub)
    with pytest.raises(_StopFollowError):
        await backfill_embedding._run_follow(pg_dsn, ["stub-1024"], interval_sec=0)

    async with store.engine.acquire() as conn:
        present = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = $1",
            index_name_for("stub-1024"),
        )
    assert present == 1


_MAPPER = FootprintMapper().name
_MODEL = StubEmbedder(dim=1024).name


async def _session_with_records(store: Store, count: int) -> UUID:
    """Seed one AgentSession with ``count`` records and a manifest covering them."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'scan test')",
            session_id,
        )
        for idx in range(count):
            await conn.execute(
                "INSERT INTO session_records "
                "(session_id, part, idx, kind, payload, text) "
                "VALUES ($1, 0, $2, 'UserMessage', '{}'::json, $3)",
                session_id,
                idx,
                f"scan record {idx} about deploys and locks",
            )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', $2)",
            session_id,
            count,
        )
    return session_id


async def _drain_scan(pg_dsn: str, *, page: int) -> list[tuple[int, int]]:
    """Run ``_scan`` once and return each queued page's (lo_idx, hi_idx) bounds."""
    queue: multiprocessing.Queue[backfill_embedding.PageTask | None] = (
        multiprocessing.Queue()
    )
    await backfill_embedding._scan(
        pg_dsn,
        queue,
        page,
        n_workers=1,
        mapper_name=_MAPPER,
        model=_MODEL,
        indexed_kinds=sorted(FootprintMapper().embedded_kinds),
    )
    bounds: list[tuple[int, int]] = []
    while True:
        task = queue.get()
        if task is None:  # The one worker sentinel closes the feed.
            break
        _inclusive, lo, hi = task
        bounds.append((lo[3], hi[3]))  # (…, part, idx) -> idx is element 3.
    return bounds


async def _session_with_typed_records(
    store: Store,
    rows: list[tuple[str, str]],
) -> UUID:
    """Seed one session with explicit ``(kind, text)`` rows + a covering manifest."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'scan test')",
            session_id,
        )
        for idx, (kind, text) in enumerate(rows):
            await conn.execute(
                "INSERT INTO session_records "
                "(session_id, part, idx, kind, payload, text) "
                "VALUES ($1, 0, $2, $3, '{}'::json, $4)",
                session_id,
                idx,
                kind,
                text,
            )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', $2)",
            session_id,
            len(rows),
        )
    return session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_scanner_skips_unembeddable_kinds_and_empty_text(
    store: Store,
    pg_dsn: str,
) -> None:
    """Never-indexed kinds and empty-text rows are NEVER queued as pending.

    The production leak: a kind the mapper maps to no units (telemetry like
    ``ContextState``) or an empty-text row never gets a freshness marker, so the
    md5 NOT EXISTS predicate counted it pending on EVERY run forever (3.88M live,
    zero writes). The scanner must filter to the mapper's embeddable contract.
    """
    session_id = await _session_with_typed_records(
        store,
        [
            ("UserMessage", "an indexed non-empty row that must embed"),  # `idx` 0.
            ("ContextState", "telemetry never indexed, 21KB in prod"),  # `idx` 1.
            ("UserMessage", ""),  # `idx` 2: indexed kind but empty text -> no unit.
        ],
    )

    bounds = await _drain_scan(pg_dsn, page=10)
    pending = await _pending_via_count(store, session_id)

    queued_idxs = {idx for lo, hi in bounds for idx in (lo, hi)}
    assert queued_idxs == {0}  # Only the indexed, non-empty row.
    assert pending == 1  # ContextState + empty-text row are NOT counted.


async def _pending_via_count(store: Store, session_id: UUID) -> int:
    """Return ``_pending_count`` scoped to one session (for the exclusion test)."""
    del session_id  # The store fixture is truncated per test, so the count is global.
    async with store.engine.acquire() as conn:
        return await backfill_embedding._pending_count(
            conn,
            mapper_name=_MAPPER,
            model=_MODEL,
            indexed_kinds=sorted(FootprintMapper().embedded_kinds),
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_scanner_feeds_only_pending_pages(store: Store, pg_dsn: str) -> None:
    """The scanner queues pages of PENDING rows only, skipping embedded spans.

    Seeds 6 records, embeds the first 4 (idx 0-3) via the real sweep, leaves idx
    4-5 pending. The scanner must queue exactly the pending span -- never a page
    starting inside the already-embedded head (the ~40-min dead skip-scan bug).
    """
    session_id = await _session_with_records(store, 6)
    # Shrink the manifest so only idx 0-3 are live, embed them, then restore so
    # 4-5 become live-and-pending -- the cheap way to seed a mixed corpus.
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_manifests SET records = 4 WHERE session_id = $1",
            session_id,
        )
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_manifests SET records = 6 WHERE session_id = $1",
            session_id,
        )

    bounds = await _drain_scan(pg_dsn, page=10)

    # Only idx 4 and 5 are pending; the scanner queues them and nothing from 0-3.
    queued_idxs = {idx for lo, hi in bounds for idx in (lo, hi)}
    assert queued_idxs <= {4, 5}
    assert queued_idxs  # It did queue the pending work, not an empty feed.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_scanner_pages_pending_rows_without_gap_or_overlap(
    store: Store,
    pg_dsn: str,
) -> None:
    """Small pages over pending rows lose no row and re-queue none (keyset ``>``).

    All 5 records pending, page size 2 -> pages [0,1],[2,3],[4,4]. The keyset must
    advance by ``>`` (exclusive): a ``>=`` off-by-one would re-queue the cursor
    row as the next page's lo. Mutation-proved by that boundary.
    """
    await _session_with_records(store, 5)

    bounds = await _drain_scan(pg_dsn, page=2)

    # Every pending idx appears, once, across contiguous non-overlapping pages.
    assert [lo for lo, _hi in bounds] == [0, 2, 4]
    assert [hi for _lo, hi in bounds] == [1, 3, 4]


def test_update_rate_adopts_the_first_flush_then_smooths() -> None:
    """The first flush takes the instantaneous rate; later flushes are an EMA."""
    first = backfill_embedding._update_rate(0.0, 100, 10.0)
    assert first == pytest.approx(10.0)  # 100 records / 10s, adopted outright.
    # A slower flush pulls the rate down but not all the way (EMA smoothing).
    smoothed = backfill_embedding._update_rate(first, 10, 10.0)
    assert 1.0 < smoothed < first


def test_update_rate_ignores_a_zero_interval() -> None:
    """A zero/negative interval cannot divide; the prior rate is kept."""
    assert backfill_embedding._update_rate(5.0, 10, 0.0) == 5.0


def test_format_eta_is_hours_and_minutes() -> None:
    """ETA renders ``H:MM`` from the remaining count and rate."""
    assert backfill_embedding._format_eta(7200, 1.0) == "2:00"
    assert backfill_embedding._format_eta(90, 1.0) == "0:01"


def test_format_eta_is_a_question_mark_when_idle_or_done() -> None:
    """No rate (or nothing left) has no meaningful ETA."""
    assert backfill_embedding._format_eta(100, 0.0) == "?"
    assert backfill_embedding._format_eta(0, 5.0) == "?"


class _FakeCompiler:
    """Records the bytes save/load the mega-cache seam calls torch with.

    ``save_returns_none`` models torch's documented no-artifact case:
    ``save_cache_artifacts`` returns ``None`` when there is nothing to serialize
    (the live crash that motivated this seam's None handling).
    """

    def __init__(self, *, save_returns_none: bool = False) -> None:
        self.loaded: bytes | None = None
        self.saved_calls = 0
        self._save_returns_none = save_returns_none

    def load_cache_artifacts(self, blob: bytes) -> object:
        """Record the loaded blob (mirrors ``torch.compiler.load_cache_artifacts``)."""
        self.loaded = blob
        return None

    def save_cache_artifacts(self) -> tuple[bytes, object] | None:
        """Return fresh cache bytes, or None when there is nothing to serialize."""
        self.saved_calls += 1
        if self._save_returns_none:
            return None
        return b"COMPILED-ARTIFACTS", None


def test_load_compile_cache_loads_when_the_file_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A present cache file is fed to ``load_cache_artifacts``."""
    fake = _FakeCompiler()
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    path = tmp_path / "compile.bin"
    path.write_bytes(b"WARMED-CACHE")
    backfill_embedding._load_compile_cache(path)
    assert fake.loaded == b"WARMED-CACHE"


def test_load_compile_cache_is_a_noop_on_first_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing cache file (cold first run) loads nothing and does not error."""
    fake = _FakeCompiler()
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    backfill_embedding._load_compile_cache(tmp_path / "absent.bin")
    assert fake.loaded is None


def test_save_compile_cache_writes_the_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A clean exit saves the compiled artifacts to the path, creating parents."""
    fake = _FakeCompiler()
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    path = tmp_path / "nested" / "compile.bin"  # Parent does not exist yet.
    backfill_embedding._save_compile_cache(path)
    assert fake.saved_calls == 1
    assert path.read_bytes() == b"COMPILED-ARTIFACTS"


def test_save_compile_cache_tolerates_a_none_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``save_cache_artifacts`` returning None writes nothing and does not crash.

    torch returns None when there is nothing to serialize (all cache hits, or a
    build whose dynamo artifacts are unserializable). The live run crashed both
    workers at clean exit on the unhandled None; this pins the skip-and-continue.
    """
    fake = _FakeCompiler(save_returns_none=True)
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    path = tmp_path / "compile.bin"
    backfill_embedding._save_compile_cache(path)
    assert fake.saved_calls == 1
    assert not path.exists()  # Nothing written; no crash.


def test_compile_cache_round_trips_through_the_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bytes saved by one worker load into the next (the whole point)."""
    saver = _FakeCompiler()
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: saver)
    path = tmp_path / "compile.bin"
    backfill_embedding._save_compile_cache(path)
    loader = _FakeCompiler()
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: loader)
    backfill_embedding._load_compile_cache(path)
    assert loader.loaded == b"COMPILED-ARTIFACTS"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
