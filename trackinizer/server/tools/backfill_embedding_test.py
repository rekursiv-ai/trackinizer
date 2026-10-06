"""Follow-mode: round 1 embeds the corpus, round 2 no-ops (the sweep predicate).

Drives ``_run_follow`` for exactly two rounds against pglite with a counting
wrapper over the 1024-dim stub, proving the steady-state contract: the first
round embeds a freshly inserted record, the second re-scans and embeds NOTHING
(``md5(text)`` already matches). Also checks the follow path ensures each model's
partial index at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Full
from typing import TYPE_CHECKING, Protocol, Self, cast, override
from uuid import UUID, uuid4

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import multiprocessing
import os
import sys
import time

import pytest
import pytest_asyncio

from trackinizer.server.embedders import registry
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.semantic_mapper import IndexUnit
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_embed import (
    SweepStats,
    sweep_session_embeddings,
)
from trackinizer.server.store.session_index import index_name_for
from trackinizer.server.tools import backfill_embedding, model_buckets
from trackinizer.server.values import manifest_bound


if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Callable,
        Coroutine,
        Mapping,
        Sequence,
    )
    from multiprocessing import Queue

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, PostgresEngine


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


class _FakeAsyncio:
    """An ``asyncio`` stand-in whose ``sleep`` records; the last round's ends follow."""

    def __init__(self, *, rounds: int) -> None:
        self.sleeps: list[float] = []
        self._rounds = rounds

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if len(self.sleeps) == self._rounds:
            raise _StopFollowError


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

    stopper = _FakeAsyncio(rounds=2)
    monkeypatch.setattr(backfill_embedding, "asyncio", stopper)

    with pytest.raises(_StopFollowError):
        await backfill_embedding._run_follow(pg_dsn, ["stub-1024"], interval_sec=0)

    # The record embedded exactly once: round 1 wrote it, round 2's md5 matched
    # and skipped it. Two sleeps means two full rounds ran.
    assert counting.embedded == [
        "a session line to embed exactly once across two rounds",
    ]
    assert stopper.sleeps == [0, 0]


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

    def use_stub(name: str) -> StubEmbedder:
        del name
        return StubEmbedder(dim=1024)

    monkeypatch.setattr(backfill_embedding, "asyncio", _FakeAsyncio(rounds=1))
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


async def _scan_tasks(
    pg_dsn: str,
    *,
    page: int,
) -> list[backfill_embedding.PageTask]:
    """Run ``_scan`` once and return every queued page task, sentinel dropped."""
    queue = _FakeQueue()
    await backfill_embedding._scan(
        pg_dsn,
        cast("Queue[backfill_embedding.PageTask | None]", queue),
        page,
        n_workers=1,
        mapper_name=_MAPPER,
        model=_MODEL,
        indexed_kinds=sorted(FootprintMapper().embedded_kinds),
        alive=lambda: True,
    )
    *tasks, sentinel = queue.put_items
    assert sentinel is None  # The one worker sentinel closes the feed.
    return [task for task in tasks if task is not None]


async def _drain_scan(pg_dsn: str, *, page: int) -> list[tuple[int, int]]:
    """Run ``_scan`` once and return each queued page's (lo_idx, hi_idx) bounds."""
    # (…, part, idx) -> idx is element 3.
    return [(lo[3], hi[3]) for lo, hi in await _scan_tasks(pg_dsn, page=page)]


async def _read_idxs(
    store: Store,
    tasks: Sequence[backfill_embedding.PageTask],
) -> list[int]:
    """Feed ``tasks`` through ``_read_stage``; return the pooled records' idxs."""
    pools: asyncio.Queue[backfill_embedding.Pool | None] = asyncio.Queue()
    async with store.engine.acquire() as conn:
        await backfill_embedding._read_stage(
            conn,
            cast("Queue[backfill_embedding.PageTask | None]", _FakeQueue(*tasks, None)),
            pools,
            mapper=FootprintMapper(),
            embedder=StubEmbedder(dim=1024),
            indexed_kinds=sorted(FootprintMapper().embedded_kinds),
            flush_units=1_000,
        )
    idxs: list[int] = []
    while (pool := pools.get_nowait()) is not None:
        for row, _units, _md5 in pool:
            idx = row["idx"]
            assert isinstance(idx, int)
            idxs.append(idx)
    return idxs


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_scan_then_read_embeds_every_pending_row_across_pages(
    store: Store,
    pg_dsn: str,
) -> None:
    """The scanner's page bounds and the read stage agree: no row is dropped.

    Four pending rows in 2-row pages. A later page whose lo is its own first
    pending row must be read inclusively; reading it with ``>`` lost idx 2.
    """
    await _session_with_records(store, 4)

    tasks = await _scan_tasks(pg_dsn, page=2)

    assert len(tasks) == 2
    assert await _read_idxs(store, tasks) == [0, 1, 2, 3]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_read_stage_skips_rows_whose_freshness_marker_matches(
    store: Store,
) -> None:
    """The read stage's re-check uses the scanner's oracle (the marker).

    A fresh marker with no vector row (an fts-only sweep, or a pruned vector)
    must not be re-embedded; a stale marker must be.
    """
    session_id = await _session_with_records(store, 3)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_index_state "
            "(session_id, part, idx, mapper, model, text_md5) VALUES "
            "($1, 0, 0, $2, $3, md5('scan record 0 about deploys and locks')), "
            "($1, 0, 1, $2, $3, 'stale')",
            session_id,
            _MAPPER,
            _MODEL,
        )
        first = await conn.fetchval(
            "SELECT min(created) FROM session_records WHERE session_id = $1",
            session_id,
        )
        last = await conn.fetchval(
            "SELECT max(created) FROM session_records WHERE session_id = $1",
            session_id,
        )
    assert isinstance(first, datetime)
    assert isinstance(last, datetime)
    task: backfill_embedding.PageTask = (
        (first, str(session_id), 0, 0),
        (last, str(session_id), 0, 2),
    )

    assert await _read_idxs(store, [task]) == [1, 2]


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
    # The store fixture is truncated per test, so the global count is this session's.
    del session_id
    pending = await backfill_embedding._pending_count(
        pg_dsn,
        mapper_name=_MAPPER,
        model=_MODEL,
        indexed_kinds=sorted(FootprintMapper().embedded_kinds),
    )

    queued_idxs = {idx for lo, hi in bounds for idx in (lo, hi)}
    assert queued_idxs == {0}  # Only the indexed, non-empty row.
    assert pending == 1  # ContextState + empty-text row are NOT counted.


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


@pytest.mark.parametrize(("cpu_count", "expected_threads"), [(8, "1"), (16, "2")])
def test_worker_entry_sets_process_environment_and_runs_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cpu_count: int,
    expected_threads: str,
) -> None:
    config = backfill_embedding._WorkerConfig(
        dsn="postgresql://test",
        gpu="",
        flush_units=2,
        batch_size=3,
        model="stub-1024",
        dim=1024,
        gpu_vram_gb=8.0,
        compile_cache=tmp_path / "compile.bin",
    )
    queue = cast("Queue[backfill_embedding.PageTask | None]", object())
    progress = _progress(pending=0)
    calls: list[tuple[backfill_embedding._WorkerConfig, str, object, object]] = []

    async def fake_worker(
        worker_config: backfill_embedding._WorkerConfig,
        device: str,
        worker_queue: object,
        worker_progress: object,
    ) -> None:
        calls.append((worker_config, device, worker_queue, worker_progress))

    def run(coroutine: Coroutine[object, object, object]) -> None:
        try:
            coroutine.send(None)
        except StopIteration:
            return
        raise AssertionError("fake worker unexpectedly suspended")

    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: cpu_count)
    monkeypatch.setattr(backfill_embedding, "_worker", fake_worker)
    monkeypatch.setattr(asyncio, "run", run)

    assert backfill_embedding._worker_entry(config, queue, progress) is None
    assert calls == [(config, "cpu", queue, progress)]
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    assert os.environ["OMP_NUM_THREADS"] == expected_threads


def test_worker_entry_defaults_to_one_thread_when_cpu_count_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = backfill_embedding._WorkerConfig(
        dsn="postgresql://test",
        gpu="",
        flush_units=1,
        batch_size=1,
        model="stub-1024",
        dim=None,
        gpu_vram_gb=None,
        compile_cache=tmp_path / "compile.bin",
    )
    queue = cast("Queue[backfill_embedding.PageTask | None]", object())

    async def fake_worker(
        worker_config: backfill_embedding._WorkerConfig,
        device: str,
        worker_queue: object,
        worker_progress: object,
    ) -> None:
        del worker_config, device, worker_queue, worker_progress

    def run(coroutine: Coroutine[object, object, object]) -> None:
        try:
            coroutine.send(None)
        except StopIteration:
            return
        raise AssertionError("fake worker unexpectedly suspended")

    cpu_count_calls: list[None] = []

    def unknown_cpu_count() -> None:
        cpu_count_calls.append(None)

    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", unknown_cpu_count)
    monkeypatch.setattr(backfill_embedding, "_worker", fake_worker)
    monkeypatch.setattr(asyncio, "run", run)

    backfill_embedding._worker_entry(config, queue, _progress(pending=0))

    assert cpu_count_calls == [None]
    assert os.environ["OMP_NUM_THREADS"] == "1"


def test_worker_entry_uses_pinned_gpu_without_cpu_thread_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = backfill_embedding._WorkerConfig(
        dsn="postgresql://test",
        gpu="3",
        flush_units=2,
        batch_size=3,
        model="stub-1024",
        dim=None,
        gpu_vram_gb=None,
        compile_cache=tmp_path / "compile.bin",
    )
    queue = cast("Queue[backfill_embedding.PageTask | None]", object())
    progress = _progress(pending=0)
    calls: list[tuple[backfill_embedding._WorkerConfig, str, object, object]] = []

    async def fake_worker(
        worker_config: backfill_embedding._WorkerConfig,
        device: str,
        worker_queue: object,
        worker_progress: object,
    ) -> None:
        calls.append((worker_config, device, worker_queue, worker_progress))

    def run(coroutine: Coroutine[object, object, object]) -> None:
        try:
            coroutine.send(None)
        except StopIteration:
            return
        raise AssertionError("fake worker unexpectedly suspended")

    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(backfill_embedding, "_worker", fake_worker)
    monkeypatch.setattr(asyncio, "run", run)

    assert backfill_embedding._worker_entry(config, queue, progress) is None
    assert calls == [(config, "cuda:0", queue, progress)]
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "3"
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    assert "OMP_NUM_THREADS" not in os.environ


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


def test_update_rate_uses_the_configured_half_life_and_additive_ema() -> None:
    observed = backfill_embedding._update_rate(
        8.0,
        2,
        300.0,
        halflife_sec=300.0,
    )
    assert observed == pytest.approx(4.003333333333333)


def test_update_rate_handles_subsecond_intervals_and_unit_prev_rate() -> None:
    observed = backfill_embedding._update_rate(1.0, 3, 0.5)
    expected_weight = 0.5 ** (0.5 / 300.0)
    assert observed == pytest.approx(expected_weight + (1 - expected_weight) * 6.0)


def test_format_eta_is_hours_and_minutes() -> None:
    """ETA renders ``H:MM`` from the remaining count and rate."""
    assert backfill_embedding._format_eta(7200, 1.0) == "2:00"
    assert backfill_embedding._format_eta(90, 1.0) == "0:01"


def test_format_eta_is_a_question_mark_when_idle_or_done() -> None:
    """No rate (or nothing left) has no meaningful ETA."""
    assert backfill_embedding._format_eta(100, 0.0) == "?"
    assert backfill_embedding._format_eta(0, 5.0) == "?"


def test_format_eta_divides_remaining_by_rate_for_subminute_result() -> None:
    assert backfill_embedding._format_eta(3600, 2.0) == "0:30"


def test_format_eta_reports_one_remaining_record() -> None:
    assert backfill_embedding._format_eta(1, 1.0) == "0:00"


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
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cache with no artifacts logs and leaves no file behind."""
    fake = _FakeCompiler(save_returns_none=True)
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    path = tmp_path / "compile.bin"
    backfill_embedding._save_compile_cache(path)
    assert fake.saved_calls == 1
    assert not path.exists()  # Nothing written; no crash.
    assert capsys.readouterr().out == (
        f"compile-cache: nothing to save, keeping {path}\n"
    )


def test_save_compile_cache_flushes_the_no_artifact_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeCompiler(save_returns_none=True)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_print(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    monkeypatch.setattr(backfill_embedding, "print", record_print, raising=False)
    path = tmp_path / "compile.bin"

    backfill_embedding._save_compile_cache(path)

    assert calls == [
        ((f"compile-cache: nothing to save, keeping {path}",), {"flush": True}),
    ]


def test_save_compile_cache_creates_nested_parent_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeCompiler()
    monkeypatch.setattr(backfill_embedding, "_compiler", lambda: fake)
    path = tmp_path / "one" / "two" / "compile.bin"

    backfill_embedding._save_compile_cache(path)

    assert path.read_bytes() == b"COMPILED-ARTIFACTS"


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


def test_add_arguments_registers_defaults_and_overrides() -> None:
    parser = argparse.ArgumentParser()
    backfill_embedding._add_arguments(parser)
    actions = {action.dest: action for action in parser._actions}

    def default(dest: str) -> object:
        return cast(object, actions[dest].default)

    assert actions["dsn"].option_strings == []
    assert actions["dsn"].required is True
    assert actions["gpus"].option_strings == ["--gpus"]
    assert default("gpus") == "0"
    assert actions["workers_per_gpu"].type is int
    assert default("workers_per_gpu") == 1
    assert actions["cpu_workers"].type is int
    assert default("cpu_workers") == 0
    assert actions["page"].type is int
    assert default("page") == 2_000
    assert actions["flush_units"].type is int
    assert default("flush_units") == 1_024
    assert actions["batch_size"].type is int
    assert default("batch_size") == 48
    assert default("model") == ""
    assert actions["dim"].type is int
    assert default("dim") is None
    assert actions["gpu_vram_gb"].type is float
    assert default("gpu_vram_gb") is None
    assert actions["compile_cache"].type is Path
    assert default("compile_cache") == Path(
        "/opt/scratch/caches/torch/compile-artifacts/backfill-embedding.bin",
    )
    assert actions["follow"].option_strings == ["--follow"]
    assert actions["follow"].nargs == 0
    assert actions["interval_sec"].type is int
    assert default("interval_sec") == 60

    defaults = cast(
        "backfill_embedding.Flags",
        parser.parse_args(["postgresql://db"]),
    )
    assert defaults.gpus == "0"
    assert defaults.workers_per_gpu == 1
    assert defaults.cpu_workers == 0
    assert defaults.page == 2_000
    assert defaults.flush_units == 1_024
    assert defaults.batch_size == 48
    assert defaults.model == ""
    assert defaults.gpu_vram_gb is None
    assert defaults.compile_cache == Path(
        "/opt/scratch/caches/torch/compile-artifacts/backfill-embedding.bin",
    )
    assert defaults.interval_sec == 60
    assert defaults.dsn == "postgresql://db"
    assert defaults.follow is False
    assert "Postgres DSN of the trackinizer database." in parser.format_help()

    explicit = cast(
        "backfill_embedding.Flags",
        parser.parse_args(
            [
                "postgresql://db",
                "--gpus",
                "2,3",
                "--workers-per-gpu",
                "2",
                "--cpu-workers",
                "1",
                "--page",
                "7",
                "--flush-units",
                "9",
                "--batch-size",
                "5",
                "--model",
                "stub-1024",
                "--dim",
                "16",
                "--gpu-vram-gb",
                "4.5",
                "--follow",
                "--interval-sec",
                "12",
            ],
        ),
    )
    assert explicit.gpus == "2,3"
    assert explicit.workers_per_gpu == 2
    assert explicit.cpu_workers == 1
    assert explicit.page == 7
    assert explicit.flush_units == 9
    assert explicit.batch_size == 5
    assert explicit.model == "stub-1024"
    assert explicit.dim == 16
    assert explicit.gpu_vram_gb == 4.5
    assert explicit.follow is True
    assert explicit.interval_sec == 12


def test_key_and_queued_preserve_order_and_stringify_uuid() -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)
    session_id = uuid4()
    key = backfill_embedding._key(
        cast(
            "Record",
            {"created": created, "session_id": session_id, "part": 3, "idx": 4},
        ),
    )
    assert key == (created, session_id, 3, 4)
    assert backfill_embedding._queued(key) == (created, str(session_id), 3, 4)


def test_pending_page_sql_returns_exact_first_and_keyset_queries() -> None:
    join, predicate = manifest_bound("r")
    first = backfill_embedding._pending_page_sql(join, predicate, first=True)
    later = backfill_embedding._pending_page_sql(join, predicate, first=False)

    expected_prefix = (
        "SELECT r.created, r.session_id, r.part, r.idx FROM session_records r "
        "JOIN session_manifests m ON m.session_id = r.session_id AND m.part = r.part "
        "WHERE "
    )
    expected_suffix = (
        "r.idx < m.records AND r.kind = ANY($3::text[]) AND r.text <> '' "
        "AND NOT EXISTS (SELECT 1 FROM session_index_state s "
        "WHERE s.session_id = r.session_id AND s.part = r.part "
        "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
        "AND s.text_md5 = md5(r.text)) "
        "ORDER BY r.created, r.session_id, r.part, r.idx LIMIT "
    )
    assert first == expected_prefix + expected_suffix + "$4"
    assert later == (
        expected_prefix
        + "(r.created, r.session_id, r.part, r.idx) > ($4, $5, $6, $7) AND "
        + expected_suffix
        + "$8"
    )


def test_compiler_returns_torch_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = type(
        "Compiler",
        (),
        {"is_compiling": staticmethod(lambda: False)},
    )()
    torch = type("Torch", (), {"compiler": compiler})()
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert backfill_embedding._compiler() is compiler


def test_resolve_vram_uses_override_without_importing_torch() -> None:
    assert backfill_embedding._resolve_vram_gb("cpu", 12.5) == 12.5


class _CudaFake:
    def __init__(self, *, available: bool, total_memory: int) -> None:
        self.available = available
        self.total_memory = total_memory
        self.property_indices: list[int] = []

    def is_available(self) -> bool:
        return self.available

    def get_device_properties(self, index: int) -> object:
        self.property_indices.append(index)
        return type("Properties", (), {"total_memory": self.total_memory})()


class _TorchFake:
    def __init__(self, cuda: _CudaFake) -> None:
        self.cuda = cuda


def test_resolve_vram_queries_cuda_zero_and_scales_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cuda = _CudaFake(available=True, total_memory=12_500_000_000)
    monkeypatch.setitem(sys.modules, "torch", _TorchFake(cuda))

    assert backfill_embedding._resolve_vram_gb("cuda:7", None) == 12.5
    assert cuda.property_indices == [0]


def test_resolve_vram_uses_reference_for_cpu_even_when_cuda_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cuda = _CudaFake(available=True, total_memory=12_500_000_000)
    monkeypatch.setitem(sys.modules, "torch", _TorchFake(cuda))

    assert backfill_embedding._resolve_vram_gb("cpu", None) == (
        model_buckets.REFERENCE_VRAM_GB
    )
    assert cuda.property_indices == []


def test_resolve_vram_uses_reference_when_cuda_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cuda = _CudaFake(available=False, total_memory=12_500_000_000)
    monkeypatch.setitem(sys.modules, "torch", _TorchFake(cuda))

    assert backfill_embedding._resolve_vram_gb("cuda:0", None) == (
        model_buckets.REFERENCE_VRAM_GB
    )
    assert cuda.property_indices == []


def test_follow_embedder_delegates_to_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = StubEmbedder(dim=4)
    calls: list[str] = []

    def build(name: str) -> StubEmbedder:
        calls.append(name)
        return embedder

    monkeypatch.setattr(registry, "build_session_embedder", build)

    assert backfill_embedding._follow_embedder("stub-4") is embedder
    assert calls == ["stub-4"]


def test_follow_embedder_rejects_disabled_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disabled(name: str) -> None:
        del name

    monkeypatch.setattr(registry, "build_session_embedder", disabled)

    with pytest.raises(SystemExit, match="--model 'disabled' is empty/disabled"):
        backfill_embedding._follow_embedder("disabled")


@pytest.mark.asyncio
async def test_pending_count_returns_integer_and_binds_all_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.args: tuple[object, ...] | None = None

        async def fetchval(self, query: str, *args: object) -> int:
            self.args = (query, *args)
            return 7

    conn = Connection()
    engines = _EngineFactory(conn)
    monkeypatch.setattr(backfill_embedding, "PostgresEngine", engines)
    result = await backfill_embedding._pending_count(
        "postgresql://count",
        mapper_name="mapper",
        model="model",
        indexed_kinds=["UserMessage", "AssistantMessage"],
    )

    assert result == 7
    assert engines.opened == [("postgresql://count", NOTIFY_CHANNEL)]
    assert conn.args is not None
    query, *args = conn.args
    assert isinstance(query, str)
    assert query == (
        "SELECT count(*) FROM session_records r "
        "JOIN session_manifests m ON m.session_id = r.session_id AND m.part = r.part "
        "WHERE r.idx < m.records AND r.kind = ANY($3::text[]) AND r.text <> '' "
        "AND NOT EXISTS (SELECT 1 FROM session_index_state s "
        "WHERE s.session_id = r.session_id AND s.part = r.part "
        "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
        "AND s.text_md5 = md5(r.text))"
    )
    assert args == ["mapper", "model", ["UserMessage", "AssistantMessage"]]


class _ExecuteConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> None:
        self.calls.append((query, args))


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


@pytest.mark.asyncio
async def test_write_pool_executes_delete_vectors_and_freshness_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ExecuteConnection()
    transaction_connections: list[object] = []

    def fake_tx(connection: object) -> _Transaction:
        transaction_connections.append(connection)
        return _Transaction()

    monkeypatch.setattr(backfill_embedding, "tx", fake_tx)
    session_id = uuid4()
    row = cast(
        "Record",
        {"session_id": session_id, "part": 2, "idx": 3},
    )
    pool: backfill_embedding.Pool = [
        (row, (IndexUnit(text="hello", field="content", chunk=1),), "digest"),
    ]
    embedder = _LengthFake()

    result = await backfill_embedding._write_pool(
        cast("Conn", conn),
        pool,
        [[1.25, -2.5]],
        embedder,
        "mapper",
    )

    assert result == 1
    assert transaction_connections == [conn]
    assert len(conn.calls) == 3
    assert conn.calls[0][1] == ([session_id], [2], [3], "mapper", embedder.name)
    assert conn.calls[1][1] == (
        [session_id],
        [2],
        [3],
        ["content"],
        [1],
        ["[1.25,-2.5]"],
        ["digest"],
        "mapper",
        embedder.name,
    )
    assert conn.calls[2][1] == (
        [session_id],
        [2],
        [3],
        "mapper",
        embedder.name,
        ["digest"],
    )
    assert conn.calls[0][0] == (
        "DELETE FROM session_embeddings e USING unnest($1::uuid[], "
        "$2::int[], $3::int[]) AS t(session_id, part, idx) "
        "WHERE e.session_id = t.session_id AND e.part = t.part "
        "AND e.idx = t.idx AND e.mapper = $4 AND e.model = $5"
    )
    assert conn.calls[1][0] == (
        "INSERT INTO session_embeddings "
        "(session_id, part, idx, field, chunk, mapper, model, embedding, "
        "text_md5) "
        "SELECT session_id, part, idx, field, chunk, $8, $9, "
        "embedding::halfvec, text_md5 FROM unnest($1::uuid[], $2::int[], "
        "$3::int[], $4::text[], $5::int[], $6::text[], $7::text[]) "
        "AS t(session_id, part, idx, field, chunk, embedding, text_md5)"
    )
    assert conn.calls[2][0] == (
        "INSERT INTO session_index_state "
        "(session_id, part, idx, mapper, model, text_md5) "
        "SELECT DISTINCT session_id, part, idx, $4, $5, text_md5 "
        "FROM unnest($1::uuid[], $2::int[], $3::int[], $6::text[]) "
        "AS t(session_id, part, idx, text_md5) "
        "ON CONFLICT (session_id, part, idx, mapper, model) "
        "DO UPDATE SET text_md5 = EXCLUDED.text_md5, created = now()"
    )


@pytest.mark.asyncio
async def test_write_pool_consumes_vectors_across_multiple_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ExecuteConnection()

    def fake_tx(conn: object) -> _Transaction:
        del conn
        return _Transaction()

    monkeypatch.setattr(backfill_embedding, "tx", fake_tx)
    row = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 0})
    second_row = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 1})
    third_row = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 2})
    pool: backfill_embedding.Pool = [
        (
            row,
            (
                IndexUnit(text="a", field="content", chunk=0),
                IndexUnit(text="b", field="content", chunk=1),
            ),
            "digest",
        ),
        (second_row, (IndexUnit(text="c", field="content", chunk=0),), "digest-2"),
        (third_row, (IndexUnit(text="d", field="content", chunk=0),), "digest-3"),
    ]

    await backfill_embedding._write_pool(
        cast("Conn", conn),
        pool,
        [[1.0], [2.0], [3.0], [4.0]],
        _LengthFake(),
        "mapper",
    )

    assert conn.calls[1][1][3:7] == (
        ["content", "content", "content", "content"],
        [0, 1, 0, 0],
        ["[1.0]", "[2.0]", "[3.0]", "[4.0]"],
        ["digest", "digest", "digest-2", "digest-3"],
    )
    with pytest.raises(ValueError, match="zip\\(\\) argument"):
        await backfill_embedding._write_pool(
            cast("Conn", conn),
            pool,
            [[1.0]],
            _LengthFake(),
            "mapper",
        )


@pytest.mark.asyncio
async def test_embed_batch_falls_back_to_one_text_at_a_time() -> None:
    embedder = StubEmbedder(dim=4)
    vectors = await backfill_embedding._embed_batch(embedder, ["a", "bb"])
    assert len(vectors) == 2
    assert all(len(vector) == 4 for vector in vectors)


class _BatchFake:
    dim = 2
    name = "batch-fake"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, text: str) -> list[float]:
        return [float(len(text))]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(text))] for text in texts]


@pytest.mark.asyncio
async def test_embed_batch_forwards_the_batch_size_boundary() -> None:
    embedder = _BatchFake()
    texts = [f"text-{index:02d}" for index in range(48)]

    vectors = await backfill_embedding._embed_batch(embedder, texts)

    assert embedder.calls == [texts]
    assert vectors == [[float(len(text))] for text in texts]


class _LengthFake:
    dim = 1
    name = "length-fake"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append([text])
        return [float(len(text))]


@pytest.mark.asyncio
async def test_embed_stage_embeds_pools_and_forwards_sentinel() -> None:
    pools: asyncio.Queue[backfill_embedding.Pool | None] = asyncio.Queue()
    writes: asyncio.Queue[tuple[backfill_embedding.Pool, list[list[float]]] | None] = (
        asyncio.Queue()
    )
    row = cast("Record", {"id": 1})
    pool: backfill_embedding.Pool = [
        (row, (IndexUnit(text="hello"),), "digest"),
    ]
    await pools.put(pool)
    await pools.put(None)

    embedder = _BucketFake()
    await backfill_embedding._embed_stage(
        pools,
        writes,
        embedder,
        edges=(8,),
        rows={8: 2},
    )

    assert await writes.get() == (pool, [[5.0]])
    assert await writes.get() is None
    assert embedder.calls == [(["hello"], (8,), {8: 2})]


@pytest.mark.asyncio
async def test_write_stage_writes_reports_and_stops_on_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: asyncio.Queue[tuple[backfill_embedding.Pool, list[list[float]]] | None] = (
        asyncio.Queue()
    )
    pool: backfill_embedding.Pool = [
        (cast("Record", {"id": 1}), (), "digest"),
    ]
    second_pool: backfill_embedding.Pool = [
        (cast("Record", {"id": 2}), (), "digest-2"),
    ]
    await writes.put((pool, []))
    await writes.put((second_pool, []))
    await writes.put(None)
    calls: list[tuple[object, ...]] = []

    async def fake_write_pool(
        conn: object,
        received_pool: backfill_embedding.Pool,
        vectors: list[list[float]],
        embedder: object,
        mapper_name: str,
    ) -> int:
        calls.append((conn, received_pool, vectors, embedder, mapper_name))
        return 1

    monkeypatch.setattr(backfill_embedding, "_write_pool", fake_write_pool)
    monotonic_values = iter([1.0, 101.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_values, 101.0))
    update_calls: list[tuple[float, int, float]] = []

    def update_rate(prev: float, records: int, interval: float) -> float:
        update_calls.append((prev, records, interval))
        return 10.0 + len(update_calls)

    monkeypatch.setattr(backfill_embedding, "_update_rate", update_rate)
    printed: list[tuple[object, dict[str, object]]] = []

    def record_print(text: object, **kwargs: object) -> None:
        printed.append((text, kwargs))

    monkeypatch.setattr(backfill_embedding, "print", record_print, raising=False)

    embedder = _LengthFake()
    conn = cast("Conn", object())
    progress = _progress(pending=2)
    await backfill_embedding._write_stage(
        conn,
        writes,
        embedder=embedder,
        mapper_name="mapper",
        name="worker-1",
        start=0.0,
        progress=progress,
    )

    assert calls == [
        (conn, pool, [], embedder, "mapper"),
        (conn, second_pool, [], embedder, "mapper"),
    ]
    assert update_calls == [(0.0, 1, 1.0), (11.0, 1, 100.0)]
    assert printed == [
        ("worker-1: done=1 pending=1 rate=11.0 rec/s eta=0:00", {"flush": True}),
        ("worker-1: done=2 pending=0 rate=12.0 rec/s eta=?", {"flush": True}),
    ]
    assert progress.done.value == 2


def _progress(*, pending: int) -> backfill_embedding._Progress:
    return backfill_embedding._Progress(
        pending=pending,
        done=multiprocessing.Value(ctypes.c_int64, 0),
    )


@pytest.mark.asyncio
async def test_write_stages_report_run_wide_progress_against_one_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers share one pending total; each line reports the run, not a shard.

    Previously each worker counted the GLOBAL pending as its own shard and only
    its own writes as done, so with N workers the remaining (and ETA) was ~N
    times too high: worker-2 below would have printed ``pending=9``.
    """

    async def fake_write_pool(*args: object) -> int:
        del args
        return 0

    monkeypatch.setattr(backfill_embedding, "_write_pool", fake_write_pool)
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)
    printed = _PrintRecorder()
    monkeypatch.setattr(backfill_embedding, "print", printed, raising=False)
    progress = _progress(pending=10)
    pool: backfill_embedding.Pool = [(cast("Record", {"id": 1}), (), "digest")]

    for name in ("worker-1", "worker-2"):
        writes: asyncio.Queue[
            tuple[backfill_embedding.Pool, list[list[float]]] | None
        ] = asyncio.Queue()
        await writes.put((pool, []))
        await writes.put(None)
        await backfill_embedding._write_stage(
            cast("Conn", object()),
            writes,
            embedder=_LengthFake(),
            mapper_name="mapper",
            name=name,
            start=0.0,
            progress=progress,
        )

    lines = [str(args[0]) for args, _kwargs in printed.calls]
    assert [line.split(" rate=")[0] for line in lines] == [
        "worker-1: done=1 pending=9",
        "worker-2: done=2 pending=8",
    ]


@pytest.mark.asyncio
async def test_embed_pool_sorts_non_bucket_texts_then_restores_order() -> None:
    embedder = _LengthFake()
    texts = ["four", "x", "three"]

    vectors = await backfill_embedding._embed_pool(
        embedder,
        texts,
        edges=(),
        rows={},
    )

    assert embedder.calls == [["x"], ["four"], ["three"]]
    assert vectors == [[4.0], [1.0], [5.0]]


class _BucketFake:
    dim = 2
    name = "bucket-fake"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], tuple[int, ...], dict[int, int]]] = []

    async def embed(self, text: str) -> list[float]:
        return [float(len(text))]

    async def embed_bucketed_batch(
        self,
        texts: list[str],
        *,
        edges: tuple[int, ...],
        rows: dict[int, int],
    ) -> list[list[float]]:
        self.calls.append((texts, edges, rows))
        return [[float(len(text))] for text in texts]


@pytest.mark.asyncio
async def test_embed_pool_passes_bucket_plan_without_resorting_texts() -> None:
    embedder = _BucketFake()
    texts = ["four", "x", "three"]
    result = await backfill_embedding._embed_pool(
        embedder,
        texts,
        edges=(8, 16),
        rows={8: 4, 16: 2},
    )
    assert result == [[4.0], [1.0], [5.0]]
    assert embedder.calls == [(texts, (8, 16), {8: 4, 16: 2})]


class _FakeEngine:
    """An entered engine whose ``acquire`` lends the next scripted connection."""

    def __init__(self, conns: list[object]) -> None:
        self._conns = conns

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[object]:
        yield self._conns.pop(0)


class _EngineFactory:
    """Stands in for ``PostgresEngine``: records how each engine was opened."""

    def __init__(self, *conns: object) -> None:
        self._conns = list(conns)
        self.opened: list[tuple[str, str]] = []
        self.engines: list[_FakeEngine] = []

    def __call__(self, dsn: str, *, listen_channel: str) -> _FakeEngine:
        self.opened.append((dsn, listen_channel))
        engine = _FakeEngine(self._conns)
        self.engines.append(engine)
        return engine


class _ScriptedConn:
    """Answers each ``fetch`` with the next scripted row list; records the call."""

    def __init__(self, *responses: Sequence[Mapping[str, object]]) -> None:
        self._responses = list(responses)
        self.fetches: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(
        self,
        query: str,
        *args: object,
    ) -> Sequence[Mapping[str, object]]:
        self.fetches.append((query, args))
        return self._responses.pop(0)


class _FakeQueue:
    """The two ``multiprocessing.Queue`` calls the pipeline makes."""

    def __init__(self, *items: backfill_embedding.PageTask | None) -> None:
        self._items = list(items)
        self.put_items: list[backfill_embedding.PageTask | None] = []
        self.put_timeouts: list[float] = []

    def get(self) -> backfill_embedding.PageTask | None:
        return self._items.pop(0)

    def put(
        self,
        item: backfill_embedding.PageTask | None,
        *,
        timeout: float,
    ) -> None:
        self.put_timeouts.append(timeout)
        self.put_items.append(item)


class _FullQueue:
    """A queue nobody drains: every timed ``put`` raises ``queue.Full``."""

    def __init__(self) -> None:
        self.attempts = 0

    def put(self, item: object, *, timeout: float) -> None:
        del item, timeout
        self.attempts += 1
        raise Full


def test_put_aborts_when_every_consumer_has_exited() -> None:
    """A put blocked on a full queue re-checks liveness and aborts on all-dead.

    The old untimed ``queue.put`` blocked forever once every worker died.
    """
    queue = _FullQueue()
    liveness = iter([True, True, False])

    with pytest.raises(backfill_embedding._NoLiveWorkersError):
        backfill_embedding._put(
            cast("Queue[backfill_embedding.PageTask | None]", queue),
            None,
            alive=lambda: next(liveness),
            poll_sec=0.0,
        )

    assert queue.attempts == 3


def test_put_returns_once_the_item_is_accepted() -> None:
    queue = _FakeQueue()
    backfill_embedding._put(
        cast("Queue[backfill_embedding.PageTask | None]", queue),
        None,
        alive=lambda: False,
        poll_sec=0.5,
    )
    assert queue.put_items == [None]
    assert queue.put_timeouts == [0.5]


class _Clock:
    """A ``time`` stand-in whose ``monotonic`` returns scripted ticks, once each."""

    def __init__(self, *ticks: float) -> None:
        self._ticks = list(ticks)

    def monotonic(self) -> float:
        return self._ticks.pop(0)


class _PrintRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


class _HexDigest(Protocol):
    def hexdigest(self) -> str: ...


class _RecordingHashlib:
    """A ``hashlib`` stand-in recording each ``md5`` call's data and flag."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, bool]] = []

    def md5(self, data: bytes, *, usedforsecurity: bool) -> _HexDigest:
        self.calls.append((data, usedforsecurity))
        return hashlib.md5(data, usedforsecurity=False)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


class _UnitsByKind:
    """``One``/``Two`` yield that many embed units; anything else is fts-only."""

    name = "mapper-v1"

    def units(self, *, kind: str, text: str) -> tuple[IndexUnit, ...]:
        count = {"One": 1, "Two": 2}.get(kind, 0)
        if not count:
            return (IndexUnit(text=text, embed=False),)
        return tuple(IndexUnit(text=text, chunk=chunk) for chunk in range(count))


_SID = UUID(int=7)
_CREATED = datetime(2026, 1, 2, tzinfo=UTC)
_READ_SQL = (
    "SELECT r.created, r.session_id, r.part, r.idx, r.kind, r.text "
    "FROM session_records r "
    "JOIN session_manifests m ON m.session_id = r.session_id AND m.part = r.part "
    "WHERE (r.created, r.session_id, r.part, r.idx) >= ($4, $5::uuid, $6, $7) "
    "AND (r.created, r.session_id, r.part, r.idx) <= ($8, $9::uuid, $10, $11) "
    "AND r.idx < m.records AND r.kind = ANY($3::text[]) AND r.text <> '' "
    "AND NOT EXISTS (SELECT 1 FROM session_index_state s "
    "WHERE s.session_id = r.session_id AND s.part = r.part "
    "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
    "AND s.text_md5 = md5(r.text)) "
    "ORDER BY r.created, r.session_id, r.part, r.idx"
)


def _record(idx: int, kind: str, text: str) -> dict[str, object]:
    return {"session_id": _SID, "part": 0, "idx": idx, "kind": kind, "text": text}


def _queued(idx: int) -> tuple[datetime, str, int, int]:
    return (_CREATED, str(_SID), 0, idx)


@pytest.mark.asyncio
async def test_read_stage_skips_unembedded_rows_and_flushes_at_the_unit_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skips unit-less rows; flushes once pooled units reach the budget.

    Freshness is filtered in SQL (asserted on the bound query), so every row the
    page read returns is pending.
    """
    page_one = [
        _record(0, "FtsOnly", "no vector"),
        _record(2, "One", "a"),
        _record(3, "One", "b"),
        _record(4, "One", "c"),
    ]
    page_two = [_record(5, "Two", "d"), _record(6, "One", "e")]
    conn = _ScriptedConn(page_one, page_two)
    queue = _FakeQueue((_queued(0), _queued(4)), (_queued(5), _queued(6)), None)
    hashes = _RecordingHashlib()
    monkeypatch.setattr(backfill_embedding, "hashlib", hashes)
    pools: asyncio.Queue[backfill_embedding.Pool | None] = asyncio.Queue()

    await backfill_embedding._read_stage(
        cast("Conn", conn),
        cast("Queue[backfill_embedding.PageTask | None]", queue),
        pools,
        mapper=_UnitsByKind(),
        embedder=_LengthFake(),
        indexed_kinds=("One", "Two"),
        flush_units=2,
    )

    emitted: list[backfill_embedding.Pool | None] = []
    while not pools.empty():
        emitted.append(pools.get_nowait())
    assert emitted == [
        [
            (page_one[1], (IndexUnit(text="a"),), _md5("a")),
            (page_one[2], (IndexUnit(text="b"),), _md5("b")),
        ],
        [
            (page_one[3], (IndexUnit(text="c"),), _md5("c")),
            (
                page_two[0],
                (IndexUnit(text="d", chunk=0), IndexUnit(text="d", chunk=1)),
                _md5("d"),
            ),
        ],
        [(page_two[1], (IndexUnit(text="e"),), _md5("e"))],
        None,
    ]
    assert hashes.calls == [
        (text.encode(), False) for text in ("a", "b", "c", "d", "e")
    ]
    head = ("mapper-v1", "length-fake", ["One", "Two"])
    assert conn.fetches == [
        (_READ_SQL, (*head, *_queued(0), *_queued(4))),
        (_READ_SQL, (*head, *_queued(5), *_queued(6))),
    ]


@pytest.mark.asyncio
async def test_scan_feeds_keyset_pages_then_one_sentinel_per_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages advance by the last key; each task spans its own rows; progress prints."""
    keys = [
        {"created": _CREATED, "session_id": _SID, "part": 0, "idx": idx}
        for idx in range(3)
    ]
    conn = _ScriptedConn(keys[:2], keys[2:], [])
    engines = _EngineFactory(conn)
    queue = _FakeQueue()
    printed = _PrintRecorder()
    monkeypatch.setattr(backfill_embedding, "PostgresEngine", engines)
    monkeypatch.setattr(backfill_embedding, "time", _Clock(100.0, 100.5, 103.0))
    monkeypatch.setattr(backfill_embedding, "print", printed, raising=False)

    await backfill_embedding._scan(
        "postgresql://scan",
        cast("Queue[backfill_embedding.PageTask | None]", queue),
        2,
        3,
        mapper_name="mapper-v1",
        model="model@4",
        indexed_kinds=("One", "Two"),
        alive=lambda: True,
        poll_sec=0.25,
    )

    join, predicate = manifest_bound("r")
    first = backfill_embedding._pending_page_sql(join, predicate, first=True)
    later = backfill_embedding._pending_page_sql(join, predicate, first=False)
    head = ("mapper-v1", "model@4", ["One", "Two"])
    assert engines.opened == [("postgresql://scan", NOTIFY_CHANNEL)]
    assert conn.fetches == [
        (first, (*head, 2)),
        (later, (*head, _CREATED, _SID, 0, 1, 2)),
        (later, (*head, _CREATED, _SID, 0, 2, 2)),
    ]
    assert queue.put_timeouts == [0.25] * 5
    assert queue.put_items == [
        (_queued(0), _queued(1)),
        (_queued(2), _queued(2)),
        None,
        None,
        None,
    ]
    assert printed.calls == [
        (("scanner: fed=2 pending (4 rec/s)",), {"flush": True}),
        (("scanner: fed=3 pending (1 rec/s)",), {"flush": True}),
    ]


@pytest.mark.asyncio
async def test_worker_wires_the_three_stages_and_persists_the_compile_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every collaborator gets the config's values; stages share bounded queues."""
    cache = tmp_path / "cache.bin"
    config = backfill_embedding._WorkerConfig(
        dsn="postgresql://worker",
        gpu="1",
        flush_units=5,
        batch_size=7,
        model="qwen",
        dim=4,
        gpu_vram_gb=24.0,
        compile_cache=cache,
    )
    embedder = _LengthFake()
    index_conn, read_conn, write_conn = object(), object(), object()
    engines = _EngineFactory(index_conn, read_conn, write_conn)
    queue = cast("Queue[backfill_embedding.PageTask | None]", _FakeQueue())
    mapper = FootprintMapper()
    calls: list[tuple[object, ...]] = []
    stages: dict[str, tuple[object, ...]] = {}

    def build(name: str, *, device: str, batch_size: int, dim: int | None) -> object:
        calls.append(("build", name, device, batch_size, dim))
        return embedder

    def load(path: Path) -> None:
        calls.append(("load", path))

    def save(path: Path) -> None:
        calls.append(("save", path))

    def vram(device: str, override: float | None) -> float:
        calls.append(("vram", device, override))
        return 31.5

    def plan(
        name: str,
        *,
        vram_gb: float,
        dim: int | None,
    ) -> tuple[tuple[int, ...], dict[int, int]]:
        calls.append(("plan", name, vram_gb, dim))
        return (8, 16), {8: 4, 16: 2}

    async def ensure(conn: object, model: str, dim: int) -> None:
        calls.append(("ensure", conn, model, dim))

    async def read_stage(
        conn: object,
        source: object,
        pools: object,
        *,
        mapper: object,
        embedder: object,
        indexed_kinds: list[str],
        flush_units: int,
    ) -> None:
        stages["read"] = (
            conn,
            source,
            pools,
            mapper,
            embedder,
            indexed_kinds,
            flush_units,
        )

    async def embed_stage(
        pools: object,
        writes: object,
        embedder: object,
        edges: object,
        rows: object,
    ) -> None:
        stages["embed"] = (pools, writes, embedder, edges, rows)

    async def write_stage(
        conn: object,
        writes: object,
        *,
        embedder: object,
        mapper_name: str,
        name: str,
        start: float,
        progress: object,
    ) -> None:
        stages["write"] = (conn, writes, embedder, mapper_name, name, start, progress)

    printed = _PrintRecorder()
    monkeypatch.setattr(registry, "build_backfill_embedder", build)
    monkeypatch.setattr(backfill_embedding, "_load_compile_cache", load)
    monkeypatch.setattr(backfill_embedding, "_save_compile_cache", save)
    monkeypatch.setattr(backfill_embedding, "_resolve_vram_gb", vram)
    monkeypatch.setattr(model_buckets, "resolve_plan", plan)
    monkeypatch.setattr(backfill_embedding, "PostgresEngine", engines)
    monkeypatch.setattr(backfill_embedding, "ensure_model_index", ensure)
    monkeypatch.setattr(backfill_embedding, "_read_stage", read_stage)
    monkeypatch.setattr(backfill_embedding, "_embed_stage", embed_stage)
    monkeypatch.setattr(backfill_embedding, "_write_stage", write_stage)
    monkeypatch.setattr(multiprocessing.current_process(), "name", "embed-worker-3")
    monkeypatch.setattr(backfill_embedding, "time", _Clock(50.0, 53.4))
    monkeypatch.setattr(backfill_embedding, "print", printed, raising=False)

    progress = _progress(pending=9)
    await backfill_embedding._worker(config, "cuda:0", queue, progress)

    assert calls == [
        ("build", "qwen", "cuda:0", 7, 4),
        ("load", cache),
        ("vram", "cuda:0", 24.0),
        ("plan", "length-fake", 31.5, 1),
        ("ensure", index_conn, "length-fake", 1),
        ("save", cache),
    ]
    assert engines.opened == [
        ("postgresql://worker", NOTIFY_CHANNEL),
        ("postgresql://worker", NOTIFY_CHANNEL),
    ]
    conn, source, pools, read_mapper, read_embedder, kinds, flush_units = stages["read"]
    assert (conn, source, read_embedder, flush_units) == (read_conn, queue, embedder, 5)
    assert kinds == sorted(mapper.embedded_kinds)
    assert isinstance(read_mapper, FootprintMapper)
    assert isinstance(pools, asyncio.Queue)
    assert pools.maxsize == 2
    embed_pools, writes, *embed_rest = stages["embed"]
    assert embed_pools is pools
    assert isinstance(writes, asyncio.Queue)
    assert writes.maxsize == 2
    assert embed_rest == [embedder, (8, 16), {8: 4, 16: 2}]
    assert stages["write"] == (
        write_conn,
        writes,
        embedder,
        mapper.name,
        "embed-worker-3",
        50.0,
        progress,
    )
    assert printed.calls == [(("embed-worker-3 DONE in 3s",), {"flush": True})]


class _NamedEmbedder:
    def __init__(self, name: str, dim: int) -> None:
        self.name = name
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        return [float(len(text))] * self.dim


@pytest.mark.asyncio
async def test_run_follow_ensures_indexes_then_sweeps_every_model_per_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each model's index is ensured once; each round sweeps all, then sleeps."""
    conn = object()
    engines = _EngineFactory(conn)
    ensured: list[tuple[object, str, int]] = []
    swept: list[tuple[object, object, object]] = []
    fake_asyncio = _FakeAsyncio(rounds=1)
    printed = _PrintRecorder()
    stats = {
        "a": SweepStats(
            records_scanned=4,
            records_embedded=3,
            units_written=5,
            records_skipped=1,
        ),
        "b": SweepStats(records_scanned=2),
    }

    async def ensure(conn: object, model: str, dim: int) -> None:
        ensured.append((conn, model, dim))

    async def sweep(
        engine: object,
        *,
        mapper: object,
        embedder: _NamedEmbedder,
    ) -> SweepStats:
        swept.append((engine, mapper, embedder.name))
        return stats[embedder.name]

    def follow_embedder(name: str) -> _NamedEmbedder:
        return _NamedEmbedder(name, {"a": 3, "b": 5}[name])

    monkeypatch.setattr(backfill_embedding, "_follow_embedder", follow_embedder)
    monkeypatch.setattr(backfill_embedding, "PostgresEngine", engines)
    monkeypatch.setattr(backfill_embedding, "ensure_model_index", ensure)
    monkeypatch.setattr(backfill_embedding, "sweep_session_embeddings", sweep)
    monkeypatch.setattr(backfill_embedding, "asyncio", fake_asyncio)
    monkeypatch.setattr(backfill_embedding, "print", printed, raising=False)

    with pytest.raises(_StopFollowError):
        await backfill_embedding._run_follow("postgresql://follow", ["a", "b"], 11)

    assert engines.opened == [("postgresql://follow", NOTIFY_CHANNEL)]
    assert ensured == [(conn, "a", 3), (conn, "b", 5)]
    (engine,) = engines.engines
    mapper = swept[0][1]
    assert isinstance(mapper, FootprintMapper)
    assert swept == [(engine, mapper, "a"), (engine, mapper, "b")]
    assert printed.calls == [
        (
            ("follow: model=a scanned=4 embedded=3 units=5 skipped=1",),
            {"flush": True},
        ),
        (
            ("follow: model=b scanned=2 embedded=0 units=0 skipped=0",),
            {"flush": True},
        ),
    ]
    assert fake_asyncio.sleeps == [11]


def test_main_help_shows_the_prose_below_the_polyglot_header(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` prints the docstring prose, not the shell re-exec lines."""
    monkeypatch.setattr(sys, "argv", ["backfill", "--help"])

    with pytest.raises(SystemExit, match=r"^0$"):
        backfill_embedding.main()

    out = capsys.readouterr().out
    assert out.split("\n\n")[1] == (
        "Backfill ``session_embeddings`` with dynamic load balancing across GPUs."
    )
    assert "exec uv" not in out


def test_main_help_has_no_description_without_a_docstring(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under ``python -OO`` (no docstrings) ``--help`` omits the description."""
    monkeypatch.setattr(backfill_embedding, "__doc__", None)
    monkeypatch.setattr(sys, "argv", ["backfill", "--help"])

    with pytest.raises(SystemExit, match=r"^0$"):
        backfill_embedding.main()

    assert capsys.readouterr().out.split("\n\n")[1].startswith("positional arguments:")


@pytest.mark.parametrize(
    ("model_flag", "models"),
    [("a,, b ", ["a", "b"]), (" , ", ["qwen3-embedding-4b@1024"])],
)
def test_main_follow_maintains_every_named_model(
    monkeypatch: pytest.MonkeyPatch,
    model_flag: str,
    models: list[str],
) -> None:
    """``--follow`` hands every non-blank ``--model`` (or the default) to follow."""
    calls: list[tuple[str, list[str], int]] = []

    async def run_follow(dsn: str, models: list[str], interval_sec: int) -> int:
        calls.append((dsn, models, interval_sec))
        return 7

    monkeypatch.setattr(backfill_embedding, "_run_follow", run_follow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill", "dsn", "--model", model_flag, "--follow", "--interval-sec", "3"],
    )

    assert backfill_embedding.main() == 7
    assert calls == [("dsn", models, 3)]


@dataclass(kw_only=True, slots=True)
class _FakeProcess:
    target: object
    args: tuple[object, ...]
    name: str
    exitcode: int
    events: list[str]

    def start(self) -> None:
        self.events.append(f"start {self.name}")

    def join(self) -> None:
        self.events.append(f"join {self.name}")

    def is_alive(self) -> bool:
        return False


class _MainQueue:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel_join_thread(self) -> None:
        self.cancelled = True


def test_main_rejects_zero_worker_slots_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--gpus ''`` with no CPU workers is a usage error, not an "ALL DONE" run.

    Previously the scanner fed pages to nobody and the run exited 0.
    """
    scanned: list[object] = []

    async def scan(*args: object, **kwargs: object) -> None:
        scanned.append((args, kwargs))

    async def pending_count(*args: object, **kwargs: object) -> int:
        scanned.append((args, kwargs))
        return 0

    monkeypatch.setattr(backfill_embedding, "_pending_count", pending_count)
    monkeypatch.setattr(backfill_embedding, "_scan", scan)
    monkeypatch.setattr(sys, "argv", ["backfill", "postgresql://main", "--gpus", ""])

    with pytest.raises(SystemExit, match=r"^2$"):
        backfill_embedding.main()

    assert scanned == []
    assert "no worker slots" in capsys.readouterr().err


def test_main_aborts_with_failure_when_every_worker_dies_mid_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan aborted for lack of live workers exits 1, never "ALL DONE".

    The workers' exit codes are 0 here, so only the abort itself can fail the
    run; the queue's feeder thread is released so the process can exit.
    """
    queue = _MainQueue()
    events: list[str] = []
    liveness: list[bool] = []

    def make_process(
        *,
        target: object,
        args: tuple[object, ...],
        name: str,
    ) -> _FakeProcess:
        return _FakeProcess(
            target=target,
            args=args,
            name=name,
            exitcode=0,
            events=events,
        )

    async def scan(*args: object, alive: Callable[[], bool], **kwargs: object) -> None:
        del args, kwargs
        liveness.append(alive())
        raise backfill_embedding._NoLiveWorkersError("every embed worker exited")

    async def pending_count(dsn: str, **kwargs: object) -> int:
        del dsn, kwargs
        return 0

    def make_queue(maxsize: int) -> _MainQueue:
        del maxsize
        return queue

    printed = _PrintRecorder()
    monkeypatch.setattr(multiprocessing, "Process", make_process)
    monkeypatch.setattr(multiprocessing, "Queue", make_queue)
    monkeypatch.setattr(backfill_embedding, "_pending_count", pending_count)
    monkeypatch.setattr(backfill_embedding, "_scan", scan)
    monkeypatch.setattr(backfill_embedding, "print", printed, raising=False)
    monkeypatch.setattr(sys, "argv", ["backfill", "postgresql://main"])

    assert backfill_embedding.main() == 1
    assert liveness == [False]
    assert queue.cancelled
    assert [args[0] for args, _kwargs in printed.calls][-1] != "ALL DONE"


@pytest.mark.parametrize(
    ("exit_codes", "message", "status"),
    [((0, 1, 0), "1 workers FAILED", 1), ((0, 0, 0), "ALL DONE", 0)],
)
def test_main_runs_one_worker_per_slot_scans_then_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exit_codes: tuple[int, ...],
    message: str,
    status: int,
) -> None:
    """Workers start, the scanner feeds them, joins tally failures."""
    cache = tmp_path / "cc.bin"
    events: list[str] = []
    processes: list[_FakeProcess] = []
    maxsizes: list[int] = []
    scans: list[tuple[object, ...]] = []
    counts: list[tuple[object, ...]] = []
    resolved: list[tuple[str, int | None]] = []
    queue = object()
    codes = iter(exit_codes)

    def make_process(
        *,
        target: object,
        args: tuple[object, ...],
        name: str,
    ) -> _FakeProcess:
        process = _FakeProcess(
            target=target,
            args=args,
            name=name,
            exitcode=next(codes),
            events=events,
        )
        processes.append(process)
        return process

    def make_queue(maxsize: int) -> object:
        maxsizes.append(maxsize)
        return queue

    def resolved_name(name: str, dim: int | None) -> str:
        resolved.append((name, dim))
        return f"{name}@resolved"

    async def pending_count(
        dsn: str,
        *,
        mapper_name: str,
        model: str,
        indexed_kinds: list[str],
    ) -> int:
        events.append("count")
        counts.append((dsn, mapper_name, model, indexed_kinds))
        return 11

    async def scan(
        dsn: str,
        source: object,
        page: int,
        n_workers: int,
        *,
        mapper_name: str,
        model: str,
        indexed_kinds: list[str],
        alive: Callable[[], bool],
    ) -> None:
        events.append("scan")
        assert alive() is False  # Every fake process reports dead.
        scans.append((dsn, source, page, n_workers, mapper_name, model, indexed_kinds))

    printed = _PrintRecorder()
    monkeypatch.setattr(multiprocessing, "Process", make_process)
    monkeypatch.setattr(multiprocessing, "Queue", make_queue)
    monkeypatch.setattr(registry, "resolved_name", resolved_name)
    monkeypatch.setattr(backfill_embedding, "_pending_count", pending_count)
    monkeypatch.setattr(backfill_embedding, "_scan", scan)
    monkeypatch.setattr(backfill_embedding, "print", printed, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill",
            "postgresql://main",
            "--gpus",
            "0, 1",
            "--cpu-workers",
            "1",
            "--page",
            "5",
            "--flush-units",
            "6",
            "--batch-size",
            "7",
            "--model",
            "m1, m2",
            "--dim",
            "4",
            "--gpu-vram-gb",
            "20",
            "--compile-cache",
            str(cache),
        ],
    )

    assert backfill_embedding.main() == status

    def config(gpu: str) -> backfill_embedding._WorkerConfig:
        return backfill_embedding._WorkerConfig(
            dsn="postgresql://main",
            gpu=gpu,
            flush_units=6,
            batch_size=7,
            model="m1",
            dim=4,
            gpu_vram_gb=20.0,
            compile_cache=cache,
        )

    names = ["embed-worker-0-gpu0", "embed-worker-1-gpu1", "embed-worker-2-cpu"]
    assert maxsizes == [6]
    progress = processes[0].args[2]
    assert isinstance(progress, backfill_embedding._Progress)
    assert progress.pending == 11
    assert [(p.target, p.args, p.name) for p in processes] == [
        (backfill_embedding._worker_entry, (config(gpu), queue, progress), name)
        for gpu, name in zip(("0", "1", ""), names, strict=True)
    ]
    assert events == [
        "count",
        *(f"start {name}" for name in names),
        "scan",
        *(f"join {name}" for name in names),
    ]
    mapper = FootprintMapper()
    assert resolved == [("m1", 4)]
    assert counts == [
        (
            "postgresql://main",
            mapper.name,
            "m1@resolved",
            sorted(mapper.embedded_kinds),
        ),
    ]
    assert scans == [
        (
            "postgresql://main",
            queue,
            5,
            3,
            mapper.name,
            "m1@resolved",
            sorted(mapper.embedded_kinds),
        ),
    ]
    assert printed.calls == [((message,), {})]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
