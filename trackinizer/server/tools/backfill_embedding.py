#!/bin/sh
# ruff: noqa: EXE003, D300, D205, T201 -- Polyglot script; a CLI main that prints.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Backfill ``session_embeddings`` with dynamic load balancing across GPUs.

One coordinator scans ``session_records`` in keyset order and feeds page
bounds to a multiprocessing queue; N embed workers (one per GPU slot, pinned
via ``CUDA_VISIBLE_DEVICES``) pull pages as they free up, so a straggler page
never idles another GPU. Static hash-sharding measured 2x slower end-to-end:
the last shard ran alone for hours while its siblings' GPUs sat idle.

Each worker owns its own DB connection, skips records whose stored
``text_md5`` already matches (the sweep predicate, so re-runs resume at scan
speed), embeds unit texts length-sorted so a forward batch pads to similar
lengths, and writes one ``unnest`` DELETE+INSERT per flush. The library sweep
(``store/session_embed.py``) remains the steady-state ingest path; this
driver exists because backfill is throughput-bound and the sweep batches only
within one record.

Examples:
  ./backfill_embedding.py "$TRACKINIZER_DSN" --gpus 0,1
  ./backfill_embedding.py "$TRACKINIZER_DSN" --gpus 0 --workers-per-gpu 2

'''
# fmt: on

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from uuid import UUID

import argparse
import asyncio
import hashlib
import multiprocessing
import os
import time

from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.embedders import registry
from trackinizer.server.notify import NOTIFY_CHANNEL, tx
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.session_embed import (
    _BatchEmbedder,
    sweep_session_embeddings,
)
from trackinizer.server.store.session_index import ensure_model_index
from trackinizer.server.tools import model_buckets
from trackinizer.server.values import manifest_bound, vetted_sql


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from multiprocessing import Queue

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.semantic_mapper import IndexUnit, SemanticMapper
    from trackinizer.types.embedder import Embedder

    # (created, session_id, part, idx) -- the console-feed keyset. The queued
    # form stringifies the UUID so the tuple pickles cheaply.
    type RecordKey = tuple[datetime, UUID, int, int]
    type QueuedKey = tuple[datetime, str, int, int]

# (inclusive_lo, lo_key, hi_key) page bounds.
type PageTask = tuple[bool, "QueuedKey", "QueuedKey"]

# The GPU pipeline's default when ``--model`` is unset: the production 4B model.
_DEFAULT_MODEL = "qwen3-embedding-4b@1024"  # house-ignore[globals] -- a CLI default, read once in main().


def main() -> int:
    """Run the backfill.

    Returns:
      exit_code: 0 when every worker exited cleanly, 1 otherwise.

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast("Flags", parser.parse_args())
    models = [name.strip() for name in flags.model.split(",") if name.strip()] or [
        _DEFAULT_MODEL,
    ]
    if flags.follow:
        # Steady-state ingest: loop the tested library sweep per model. CPU-scale
        # (measured: live ingest needs <0.1 vec/s vs ~4 vec/s CPU capacity), so
        # the GPU 3-stage pipeline below is reserved for one-shot bulk backfill.
        return asyncio.run(_run_follow(flags.dsn, models, flags.interval_sec))
    gpus = [gpu.strip() for gpu in flags.gpus.split(",") if gpu.strip()]
    # A CPU slot is the empty-string device: CUDA_VISIBLE_DEVICES="" hides
    # every GPU from that process and the worker falls back to CPU inference.
    slots = gpus * flags.workers_per_gpu + [""] * flags.cpu_workers
    queue: Queue[PageTask | None] = multiprocessing.Queue(maxsize=len(slots) * 2)
    # The GPU pipeline backfills ONE model per invocation (run it again per
    # model to bulk-backfill several); --follow above maintains all of them.
    model = models[0]
    workers = [
        multiprocessing.Process(
            target=_worker_entry,
            args=(
                _WorkerConfig(
                    dsn=flags.dsn,
                    gpu=gpu,
                    flush_units=flags.flush_units,
                    batch_size=flags.batch_size,
                    model=model,
                    dim=flags.dim,
                    gpu_vram_gb=flags.gpu_vram_gb,
                    compile_cache=flags.compile_cache,
                ),
                queue,
            ),
            name=f"embed-worker-{i}-{'gpu' + gpu if gpu else 'cpu'}",
        )
        for i, gpu in enumerate(slots)
    ]
    for worker in workers:
        worker.start()
    # The scanner walks only rows pending for THIS resolved model+mapper, so it
    # needs the stored identity workers write (resolved torch-free here, not by
    # building the embedder) and the mapper name. The GPU pipeline produces ONLY
    # vectors, so it scans the mapper's EMBEDDED kinds (F+E prose); fts-only
    # records (heads, SystemMessage) carry no vector and are marked fresh by the
    # steady-state library sweep (--follow), not this bulk path.
    scan_mapper = FootprintMapper()
    asyncio.run(
        _scan(
            flags.dsn,
            queue,
            flags.page,
            len(workers),
            mapper_name=scan_mapper.name,
            model=registry.resolved_name(model, flags.dim),
            indexed_kinds=sorted(scan_mapper.embedded_kinds),
        ),
    )
    failed = 0
    for worker in workers:
        worker.join()
        failed += worker.exitcode != 0
    print("ALL DONE" if failed == 0 else f"{failed} workers FAILED")
    return 1 if failed else 0


class Flags(Protocol):
    """Parsed command-line flags."""

    dsn: str
    gpus: str
    workers_per_gpu: int
    cpu_workers: int
    page: int
    flush_units: int
    batch_size: int
    model: str
    dim: int | None
    gpu_vram_gb: float | None
    compile_cache: Path
    follow: bool
    interval_sec: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _WorkerConfig:
    """The per-worker payload, one positional argument across the mp boundary.

    Bundled into one frozen dataclass so ``Process(args=(config, queue))`` passes
    two positionals (multiprocessing ``args`` are always positional, so keyword-
    only params cannot cross), and the worker functions take one config rather
    than eight positionals.

    Attributes:
      dsn: Postgres DSN.
      gpu: CUDA device id to pin (empty string is a CPU worker).
      flush_units: Unit texts pooled per embed+write flush.
      batch_size: The non-bucketed fallback's forward batch size.
      model: The embedder identity (bare slug or ``slug@dim``).
      dim: Optional output-dim override; ``None`` uses the model's default.
      gpu_vram_gb: Optional VRAM override; ``None`` queries the pinned device.
      compile_cache: Path to the persisted dynamo/inductor mega-cache; loaded at
        start if present, saved on clean exit.

    """

    dsn: str
    gpu: str
    flush_units: int
    batch_size: int
    model: str
    dim: int | None
    gpu_vram_gb: float | None
    compile_cache: Path


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument("dsn", help="Postgres DSN of the trackinizer database.")
    parser.add_argument(
        "--gpus",
        default="0",
        help="Comma list of CUDA device ids to spread workers over.",
    )
    parser.add_argument(
        "--workers-per-gpu",
        type=int,
        default=1,
        help="Embed processes per GPU (2 can hide DB latency; watch memory).",
    )
    # Measured 2026-09-20 on a 128-core single-socket EPYC with two RTX 5090s:
    # a whole-box CPU forward is 4.4 vec/s vs 30 vec/s per 5090 worker --
    # ~870 cores to equal one GPU, because the 4B model is
    # memory-bandwidth-bound on CPU. Kept only for a host with no GPU at all.
    parser.add_argument(
        "--cpu-workers",
        type=int,
        default=0,
        help="Additional CPU-inference workers; the queue balances them "
        "automatically. Near-worthless beside any GPU (~1/7 of one 5090 per "
        "whole 128-core box); see the measurement comment above.",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=2_000,
        help="Records per queued page task.",
    )
    parser.add_argument(
        "--flush-units",
        type=int,
        default=1_024,
        help="Unit texts pooled per embed+write flush.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=48,
        help="Texts per GPU forward pass (48 fits fp16-4B x 8k tokens in 32 GB).",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Session embedder(s) by stored name (comma-separated). The GPU "
        "pipeline backfills the FIRST; --follow maintains ALL. Empty defaults "
        "to qwen3-embedding-4b@1024.",
    )
    parser.add_argument(
        "--dim",
        type=int,
        default=None,
        help="Output-dim override for a Matryoshka model (any dim in its range); "
        "omit to use the model's registered default. Bucket rows scale with it.",
    )
    parser.add_argument(
        "--gpu-vram-gb",
        dest="gpu_vram_gb",
        type=float,
        default=None,
        help="Override the per-card VRAM (GB) the bucket rows are derived for; "
        "omit to query the pinned device (containers may misreport memory).",
    )
    parser.add_argument(
        "--compile-cache",
        dest="compile_cache",
        type=Path,
        default=Path(
            "/opt/scratch/caches/torch/compile-artifacts/backfill-embedding.bin",
        ),
        help="Path to the persisted dynamo/inductor mega-cache. Loaded at worker "
        "start when present (skips cold re-tracing), saved on clean exit. Default "
        "is under the shared scratch torch cache.",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="After draining, keep maintaining the corpus: loop the library "
        "sweep per --model, sleeping --interval-sec between rounds. CPU-scale "
        "steady-state ingest; the GPU pipeline is for one-shot bulk backfill.",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=60,
        help="Seconds to sleep between --follow sweep rounds (default 60).",
    )


# The scanner keyset-walks ONLY rows still PENDING for this ``(mapper, model)`` --
# the same NOT EXISTS md5 predicate the workers re-check -- so it skips millions of
# already-embedded rows as an index-driven predicate walk instead of paging the whole
# table (measured ~40 min of dead skip-scan at table head on the live corpus). Page
# bounds come from pending-row coordinates; workers refetch the FULL span (their md5
# re-check keeps correctness), so a dead span between two pending rows is never queued.
# The manifest bound still excludes a shrunk part's stale tail. No separate SEEK step
# is needed: a pending-only keyset walk starts at the first pending row by construction.
# A row is INDEXABLE only if its kind yields units and its text is non-empty --
# exactly the mapper's contract (:meth:`FootprintMapper.units`). ``$3`` is the
# mapper's ``indexed_kinds`` as a text[] (single source). Binding this into the
# predicate is the fix for the pending-forever leak: a never-indexed kind
# (telemetry: AgentStatusResult, ContextState, TokenUsage, ...) or an empty-text
# row otherwise never gets a marker, so the ``NOT EXISTS`` md5 check counts it
# pending on every run forever. The fragment is inlined at its two call sites (not
# a module global).
#
# PENDING keys on ``session_index_state`` (the freshness marker), NOT on
# ``session_embeddings``: an fts-only kind (a machine-output head, a SystemMessage)
# writes no vector, so a NOT-EXISTS against the embedding table would count it
# pending forever. The marker is written for every swept record, embedded or not,
# so it is the correct pending oracle.
def _pending_page_sql(join: str, predicate: str, *, first: bool) -> str:
    """Return the keyset page SQL over INDEXABLE + PENDING rows for one scan step."""
    keyset = (
        ""
        if first
        else "(r.created, r.session_id, r.part, r.idx) > ($4, $5, $6, $7) AND "
    )
    limit = "$4" if first else "$8"
    return vetted_sql(
        "SELECT r.created, r.session_id, r.part, r.idx FROM session_records r ",
        join,
        "WHERE ",
        keyset,
        predicate,
        " AND r.kind = ANY($3::text[]) AND r.text <> ''",
        " AND NOT EXISTS (SELECT 1 FROM session_index_state s "
        "WHERE s.session_id = r.session_id AND s.part = r.part "
        "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
        "AND s.text_md5 = md5(r.text)) "
        "ORDER BY r.created, r.session_id, r.part, r.idx LIMIT ",
        limit,
    )


async def _scan(
    dsn: str,
    queue: Queue[PageTask | None],
    page: int,
    n_workers: int,
    *,
    mapper_name: str,
    model: str,
    indexed_kinds: Sequence[str],
) -> None:
    """Feed pending-only page bounds to the queue; close with one sentinel/worker."""
    start = time.monotonic()
    fed = 0
    kinds = list(indexed_kinds)
    async with (
        PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine,
        engine.acquire() as conn,
    ):
        cursor: RecordKey | None = None
        join, predicate = manifest_bound("r")
        while True:
            # Keys only: workers refetch their page's texts themselves, so
            # queue items stay tiny and the scanner never blocks on text IO.
            if cursor is None:
                rows = await conn.fetch(
                    _pending_page_sql(join, predicate, first=True),
                    mapper_name,
                    model,
                    kinds,
                    page,
                )
            else:
                rows = await conn.fetch(
                    _pending_page_sql(join, predicate, first=False),
                    mapper_name,
                    model,
                    kinds,
                    *cursor,
                    page,
                )
            if not rows:
                break
            last = _key(rows[-1])
            task: PageTask = (
                cursor is None,
                _queued(_key(rows[0])),
                _queued(last),
            )
            await asyncio.to_thread(queue.put, task)
            cursor = last
            fed += len(rows)
            elapsed = time.monotonic() - start
            print(
                f"scanner: fed={fed} pending ({fed / elapsed:.0f} rec/s)",
                flush=True,
            )
    for _ in range(n_workers):
        queue.put(None)


def _key(row: Record) -> RecordKey:
    """Extract the keyset tuple from a record row."""
    created = row["created"]
    session_id = row["session_id"]
    part = row["part"]
    idx = row["idx"]
    assert isinstance(created, datetime)
    assert isinstance(session_id, UUID)
    assert isinstance(part, int)
    assert isinstance(idx, int)
    return (created, session_id, part, idx)


def _queued(key: RecordKey) -> QueuedKey:
    """Stringify the key's UUID so the queued tuple pickles cheaply."""
    created, session_id, part, idx = key
    return (created, str(session_id), part, idx)


def _worker_entry(config: _WorkerConfig, queue: Queue[PageTask | None]) -> None:
    """Pin this process to one GPU (or CPU when ``gpu`` is empty); run its loop."""
    # BOTH must precede the first torch import in this process (torch reads them
    # once at init): CUDA_VISIBLE_DEVICES so the embedder sees one card ("cuda:0"
    # is the pinned device; "" hides every GPU -> CPU worker), and
    # PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True so the allocator can grow a
    # segment instead of OOMing on fragmentation -- the live OOM trace flagged its
    # absence explicitly.
    os.environ["CUDA_VISIBLE_DEVICES"] = config.gpu
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    if not config.gpu:
        # Cap intra-op threads so N CPU workers share the box instead of each
        # claiming every core and thrashing.
        cpus = os.cpu_count() or 1
        os.environ["OMP_NUM_THREADS"] = str(max(1, cpus // 8))
    asyncio.run(_worker(config, "cuda:0" if config.gpu else "cpu", queue))


# An explicit ``--gpu-vram-gb`` wins (containers can misreport device memory). Otherwise
# the pinned CUDA device's total memory is queried; a CPU worker or a torch without CUDA
# falls back to the reference card size.
def _resolve_vram_gb(device: str, override: float | None) -> float:
    """Return the card's usable VRAM: the explicit override, else a device query."""
    if override is not None:
        return override
    import torch  # noqa: PLC0415 -- worker-only; keep config-time import torch-free.

    if device.startswith("cuda") and torch.cuda.is_available():
        # Cast the boundary like ``_compiler`` below: with the export gate's
        # useLibraryCodeForTypes=false and no torch stub shipped, the properties
        # object is Unknown there, so its ``total_memory`` read must be typed
        # here rather than inferred from torch.
        props = cast("_DeviceProperties", torch.cuda.get_device_properties(0))
        return props.total_memory / 1e9
    return model_buckets.REFERENCE_VRAM_GB


class _DeviceProperties(Protocol):
    """The one ``get_device_properties`` field the VRAM query reads (bytes)."""

    total_memory: int


def _compiler() -> _Compiler:
    """Return ``torch.compiler`` (indirected so a test can inject a fake)."""
    import torch  # noqa: PLC0415 -- worker-only; keep config-time import torch-free.

    return cast("_Compiler", torch.compiler)


class _Compiler(Protocol):
    """The two mega-cache calls the worker makes on ``torch.compiler``."""

    def load_cache_artifacts(self, artifact_bytes: bytes) -> object:
        """Prime the compile caches from a prior run's serialized artifacts."""
        ...

    def save_cache_artifacts(self) -> tuple[bytes, object] | None:
        """Serialize the compile caches (bytes at element 0), or None if empty."""
        ...


# The dynamo/inductor mega-cache: torch re-traces the static shape set from scratch
# every process (tens of minutes cold for a 36-layer model x N shapes). Persisting the
# artifacts across restarts turns that into a one-time bill -- the whole point on a
# restart-prone one-shot backfill. Load-when-present tolerates a cold first run; save is
# best-effort on a CLEAN exit only (a crashed process leaves the prior cache intact).
def _load_compile_cache(path: Path) -> None:
    """Prime torch's compile caches from ``path`` if a prior run wrote it."""
    if not path.exists():
        return
    _compiler().load_cache_artifacts(path.read_bytes())


# ``save_cache_artifacts`` returns ``None`` when there is nothing to serialize (its
# documented no-artifact case -- e.g. every compile product was a cache hit, or the
# build's dynamo artifacts are not serializable). That is not an error: skip the write
# with a notice and leave any prior cache intact.
def _save_compile_cache(path: Path) -> None:
    """Serialize torch's compile caches to ``path`` (creating parent dirs)."""
    saved = _compiler().save_cache_artifacts()
    if saved is None:
        print(f"compile-cache: nothing to save, keeping {path}", flush=True)
        return
    artifacts, _info = saved
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(artifacts)


# One record pool awaiting embed, or awaiting write once vectors attach.
type Pool = list[tuple[Record, tuple[IndexUnit, ...], str]]


# Each stage is its own coroutine on its own connection so the GPU never waits on the
# database: the reader prefetches and md5-filters the next pages while the embedder
# runs, and finished vectors are written behind the next forward pass. Bounded queues
# (2) cap memory.
async def _worker(
    config: _WorkerConfig,
    device: str,
    queue: Queue[PageTask | None],
) -> None:
    """Run the 3-stage pipeline: read pages, embed pools, write vectors."""
    mapper = FootprintMapper()
    # The registry applies each model's compile policy (Qwen family compiles the
    # forward on cuda -- measured 2.79x on a 5090, dynamic shapes; CPU and Jina
    # do not). ``build_backfill_embedder`` rejects a stub/disabled name and
    # resolves ``(model, dim)`` to the full stored identity.
    embedder = registry.build_backfill_embedder(
        config.model,
        device=device,
        batch_size=config.batch_size,
        dim=config.dim,
    )
    # Prime torch's compile caches from a prior run BEFORE the first forward warms
    # them, so a restart pays the dynamo/inductor tracing bill once, not per process.
    _load_compile_cache(config.compile_cache)
    # Derive this card's bucket plan ONCE: edges are corpus+arch, rows scale with
    # the measured (or overridden) VRAM and the chosen dim. An unknown model logs
    # a warning and falls back to conservative rows (never another model's tuning).
    vram_gb = _resolve_vram_gb(device, config.gpu_vram_gb)
    edges, rows = model_buckets.resolve_plan(
        embedder.name,
        vram_gb=vram_gb,
        dim=embedder.dim,
    )
    async with (
        PostgresEngine(dsn=config.dsn, listen_channel=NOTIFY_CHANNEL) as index_engine,
        index_engine.acquire() as conn,
    ):
        await ensure_model_index(conn, embedder.name, embedder.dim)
        pending = await _pending_count(
            conn,
            mapper_name=mapper.name,
            model=embedder.name,
            indexed_kinds=sorted(mapper.embedded_kinds),
        )
    pools: asyncio.Queue[Pool | None] = asyncio.Queue(maxsize=2)
    writes: asyncio.Queue[tuple[Pool, list[list[float]]] | None] = asyncio.Queue(
        maxsize=2,
    )
    name = multiprocessing.current_process().name
    start = time.monotonic()
    async with (
        PostgresEngine(dsn=config.dsn, listen_channel=NOTIFY_CHANNEL) as engine,
        engine.acquire() as read_conn,
        engine.acquire() as write_conn,
        asyncio.TaskGroup() as group,
    ):
        group.create_task(
            _read_stage(
                read_conn,
                queue,
                pools,
                mapper=mapper,
                embedder=embedder,
                flush_units=config.flush_units,
            ),
        )
        group.create_task(_embed_stage(pools, writes, embedder, edges, rows))
        group.create_task(
            _write_stage(
                write_conn,
                writes,
                embedder=embedder,
                mapper_name=mapper.name,
                name=name,
                start=start,
                pending=pending,
            ),
        )
    # Clean exit only (a crash never reaches here, leaving the prior cache intact):
    # persist this run's freshly warmed compile artifacts for the next process.
    _save_compile_cache(config.compile_cache)
    print(f"{name} DONE in {time.monotonic() - start:.0f}s", flush=True)


async def _read_stage(
    conn: Conn,
    queue: Queue[PageTask | None],
    pools: asyncio.Queue[Pool | None],
    *,
    mapper: SemanticMapper,
    embedder: Embedder,
    flush_units: int,
) -> None:
    """Fetch pages, md5-filter, and emit embed-ready pools."""
    pool: Pool = []
    pooled_units = 0
    while True:
        task = await asyncio.to_thread(queue.get)
        if task is None:
            break
        inclusive, lo, hi = task
        # Two static texts, not an f-string comparator: the only inline piece
        # is the >= / > choice for the first (inclusive) page. The manifest bound
        # (``r.idx < m.records``) excludes stale tail rows and carries no ``>=``,
        # so the first-``>=`` replace still targets the keyset comparator.
        join, predicate = manifest_bound("r")
        inclusive_sql = vetted_sql(
            "SELECT r.created, r.session_id, r.part, r.idx, r.kind, r.text "
            "FROM session_records r ",
            join,
            "WHERE (r.created, r.session_id, r.part, r.idx) >= ($1, $2::uuid, $3, $4) "
            "AND (r.created, r.session_id, r.part, r.idx) <= ($5, $6::uuid, $7, $8) "
            "AND ",
            predicate,
            " ORDER BY r.created, r.session_id, r.part, r.idx",
        )
        exclusive_sql = inclusive_sql.replace(">=", ">", 1)
        rows = await conn.fetch(
            inclusive_sql if inclusive else exclusive_sql,
            *lo,
            *hi,
        )
        # One round-trip for the whole page's md5 state, not one per record.
        stored = {
            (r["session_id"], r["part"], r["idx"]): r["text_md5"]
            for r in await conn.fetch(
                "SELECT DISTINCT e.session_id, e.part, e.idx, e.text_md5 "
                "FROM session_embeddings e JOIN unnest($1::uuid[], $2::int[], "
                "$3::int[]) AS t(session_id, part, idx) "
                "ON e.session_id = t.session_id AND e.part = t.part "
                "AND e.idx = t.idx WHERE e.mapper = $4 AND e.model = $5",
                [r["session_id"] for r in rows],
                [r["part"] for r in rows],
                [r["idx"] for r in rows],
                mapper.name,
                embedder.name,
            )
        }
        for row in rows:
            kind = row["kind"]
            text = row["text"]
            assert isinstance(kind, str)
            assert isinstance(text, str)
            units = tuple(
                unit for unit in mapper.units(kind=kind, text=text) if unit.embed
            )
            if not units:
                continue
            text_md5 = hashlib.md5(  # A change-detector, not a security hash.
                text.encode(),
                usedforsecurity=False,
            ).hexdigest()
            if stored.get((row["session_id"], row["part"], row["idx"])) == text_md5:
                continue
            pool.append((row, units, text_md5))
            pooled_units += len(units)
            if pooled_units >= flush_units:
                await pools.put(pool)
                pool = []
                pooled_units = 0
    if pool:
        await pools.put(pool)
    await pools.put(None)


@runtime_checkable
class _BucketEmbedder(Protocol):
    """An embedder offering the length-bucketed static-shape backfill path.

    Only ``QwenFamilyEmbedder`` implements ``embed_bucketed_batch`` (Jina's custom
    ``encode`` and ``StubEmbedder`` do not). This runner feature-detects it to
    route through the fixed-shape core; every other embedder uses ``embed_batch``.
    Defined here (its only user) rather than in ``session_embed`` so it is not a
    private protocol unused in its own module.
    """

    dim: int
    name: str

    async def embed_bucketed_batch(
        self,
        texts: list[str],
        *,
        edges: Sequence[int],
        rows: Mapping[int, int],
    ) -> list[list[float]]:
        """Embed ``texts`` via length buckets over ``edges``, input order."""
        ...


async def _embed_stage(
    pools: asyncio.Queue[Pool | None],
    writes: asyncio.Queue[tuple[Pool, list[list[float]]] | None],
    embedder: Embedder,
    edges: Sequence[int],
    rows: Mapping[int, int],
) -> None:
    """Embed each pool's unit texts; forward results to write, in input order."""
    while True:
        pool = await pools.get()
        if pool is None:
            break
        texts = [unit.text for _row, units, _md5 in pool for unit in units]
        vectors = await _embed_pool(embedder, texts, edges, rows)
        await writes.put((pool, vectors))
    await writes.put(None)


# A ``_BucketEmbedder`` (QwenFamilyEmbedder with ``compile_forward``) routes by true
# token length into the fixed shape set -- it owns its own ordering, so no pre-sort
# here. Any other embedder falls back to the length-sorted ``embed_batch`` path (a batch
# pads to its longest text, so sorting keeps a short text off a long text's pad).
async def _embed_pool(
    embedder: Embedder,
    texts: list[str],
    edges: Sequence[int],
    rows: Mapping[int, int],
) -> list[list[float]]:
    """Embed a pool's texts, preferring the length-bucketed static-shape path."""
    if isinstance(embedder, _BucketEmbedder):
        return await embedder.embed_bucketed_batch(texts, edges=edges, rows=rows)
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    sorted_vectors = await _embed_batch(embedder, [texts[i] for i in order])
    rank = {slot: at for at, slot in enumerate(order)}
    return [sorted_vectors[rank[i]] for i in range(len(texts))]


# The pending denominator: INDEXABLE records with no up-to-date freshness marker
# for this ``(mapper, model)`` -- the sweep predicate as a COUNT, filtered to what
# the mapper actually indexes (its ``indexed_kinds`` + non-empty text). WITHOUT the
# indexable filter this counted every never-indexed / empty-text row as pending
# forever (3.88M live, dominated by empty AssistantMessage/Thinking), which is the
# leak that made three runs write zero. Keys on ``session_index_state`` (the
# marker), so an fts-only record counts pending until swept and fresh after, never
# forever. ``md5(r.text)`` is computed in-DB, matching the worker's app-side md5.
async def _pending_count(
    conn: Conn,
    *,
    mapper_name: str,
    model: str,
    indexed_kinds: Sequence[str],
) -> int:
    """Return the number of INDEXABLE records needing a (re-)sweep."""
    # Bound by the live manifest prefix (stale tail never sweeps) and the mapper's
    # indexable contract (never-indexed kinds / empty text never sweep).
    join, predicate = manifest_bound("r")
    value = await conn.fetchval(
        vetted_sql(
            "SELECT count(*) FROM session_records r ",
            join,
            "WHERE ",
            predicate,
            " AND r.kind = ANY($3::text[]) AND r.text <> ''",
            " AND NOT EXISTS ("
            "SELECT 1 FROM session_index_state s "
            "WHERE s.session_id = r.session_id AND s.part = r.part "
            "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
            "AND s.text_md5 = md5(r.text))",
        ),
        mapper_name,
        model,
        list(indexed_kinds),
    )
    assert isinstance(value, int)
    return value


async def _write_stage(
    conn: Conn,
    writes: asyncio.Queue[tuple[Pool, list[list[float]]] | None],
    *,
    embedder: Embedder,
    mapper_name: str,
    name: str,
    start: float,
    pending: int,
) -> None:
    """Write each embedded pool; report this worker's shard progress per flush."""
    done = 0
    rate = 0.0  # EMA of records/sec.
    last = start
    while True:
        item = await writes.get()
        if item is None:
            break
        pool, vectors = item
        await _write_pool(conn, pool, vectors, embedder, mapper_name)
        done += len(pool)
        now = time.monotonic()
        rate = _update_rate(rate, len(pool), now - last)
        last = now
        remaining = max(0, pending - done)
        print(
            f"{name}: done={done} pending={remaining} "
            f"rate={rate:.1f} rec/s eta={_format_eta(remaining, rate)}",
            flush=True,
        )


# The decay weights the new instantaneous rate by how much of the half-life the interval
# spans, so an idle gap does not overweight one flush. ``prev == 0`` (the first flush)
# adopts the instantaneous rate outright. ``halflife_sec`` defaults to ~5 min so a run
# crossing a long-text region (which legitimately embeds at ~half the short-region rate)
# does not whipsaw the ETA; it is a kwarg, not a global, so a caller can retune it.
def _update_rate(
    prev: float,
    records: int,
    interval_sec: float,
    *,
    halflife_sec: float = 300.0,
) -> float:
    """Return the EMA records/sec after a flush of ``records`` over ``interval``."""
    if interval_sec <= 0:
        return prev
    instant = records / interval_sec
    if prev <= 0:
        return instant
    weight = 0.5 ** (interval_sec / halflife_sec)
    return weight * prev + (1 - weight) * instant


def _format_eta(remaining: int, rate: float) -> str:
    """Return ``H:MM`` for ``remaining`` records at ``rate`` rec/s, or ``?`` if idle."""
    if rate <= 0 or remaining <= 0:
        return "?"
    seconds = int(remaining / rate)
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}:{minutes:02d}"


async def _write_pool(
    conn: Conn,
    pool: Pool,
    vectors: list[list[float]],
    embedder: Embedder,
    mapper_name: str,
) -> int:
    """Write one embedded pool: one unnest DELETE+INSERT transaction."""
    sids: list[object] = []
    parts: list[object] = []
    idxs: list[object] = []
    fields: list[str] = []
    chunks: list[int] = []
    embeds: list[str] = []
    md5s: list[str] = []
    at = 0
    for row, units, text_md5 in pool:
        take = vectors[at : at + len(units)]
        at += len(units)
        for unit, vector in zip(units, take, strict=True):
            sids.append(row["session_id"])
            parts.append(row["part"])
            idxs.append(row["idx"])
            fields.append(unit.field)
            chunks.append(unit.chunk)
            embeds.append("[" + ",".join(repr(v) for v in vector) + "]")
            md5s.append(text_md5)
    async with tx(conn):
        await conn.execute(
            "DELETE FROM session_embeddings e USING unnest($1::uuid[], "
            "$2::int[], $3::int[]) AS t(session_id, part, idx) "
            "WHERE e.session_id = t.session_id AND e.part = t.part "
            "AND e.idx = t.idx AND e.mapper = $4 AND e.model = $5",
            [row["session_id"] for row, _units, _md5 in pool],
            [row["part"] for row, _units, _md5 in pool],
            [row["idx"] for row, _units, _md5 in pool],
            mapper_name,
            embedder.name,
        )
        await conn.execute(
            "INSERT INTO session_embeddings "
            "(session_id, part, idx, field, chunk, mapper, model, embedding, "
            "text_md5) "
            "SELECT session_id, part, idx, field, chunk, $8, $9, "
            "embedding::halfvec, text_md5 FROM unnest($1::uuid[], $2::int[], "
            "$3::int[], $4::text[], $5::int[], $6::text[], $7::text[]) "
            "AS t(session_id, part, idx, field, chunk, embedding, text_md5)",
            sids,
            parts,
            idxs,
            fields,
            chunks,
            embeds,
            md5s,
            mapper_name,
            embedder.name,
        )
        # The freshness marker, one per embedded record (deduped from the unit-level
        # arrays): the scanner keys PENDING on ``session_index_state``, so an
        # embedded record without a marker would be re-queued every run forever.
        await conn.execute(
            "INSERT INTO session_index_state "
            "(session_id, part, idx, mapper, model, text_md5) "
            "SELECT DISTINCT session_id, part, idx, $4, $5, text_md5 "
            "FROM unnest($1::uuid[], $2::int[], $3::int[], $6::text[]) "
            "AS t(session_id, part, idx, text_md5) "
            "ON CONFLICT (session_id, part, idx, mapper, model) "
            "DO UPDATE SET text_md5 = EXCLUDED.text_md5, created = now()",
            [row["session_id"] for row, _units, _md5 in pool],
            [row["part"] for row, _units, _md5 in pool],
            [row["idx"] for row, _units, _md5 in pool],
            mapper_name,
            embedder.name,
            [text_md5 for _row, _units, text_md5 in pool],
        )
    return len(sids)


async def _embed_batch(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Embed via the batch path when offered (QwenEmbedder), else one by one."""
    if isinstance(embedder, _BatchEmbedder):
        return await embedder.embed_batch(texts)
    return [await embedder.embed(text) for text in texts]


# The steady-state ingest path (design: ``docs/private/session_indexing.md``). Each
# round runs :func:`sweep_session_embeddings` per model; the ``text_md5`` predicate
# makes an already-swept corpus a cheap no-op, so a drained rescan costs a scan, not re-
# embedding. Each model's partial HNSW index is ensured once at startup so a fresh model
# is searchable without a migration.
async def _run_follow(dsn: str, models: list[str], interval_sec: int) -> int:
    """Maintain ``models`` forever: loop the library sweep, sleeping between rounds."""
    mapper = FootprintMapper()
    embedders = [_follow_embedder(name) for name in models]
    async with PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine:
        async with engine.acquire() as conn:
            for embedder in embedders:
                await ensure_model_index(conn, embedder.name, embedder.dim)
        while True:
            for embedder in embedders:
                stats = await sweep_session_embeddings(
                    engine,
                    mapper=mapper,
                    embedder=embedder,
                )
                print(
                    f"follow: model={embedder.name} scanned={stats.records_scanned} "
                    f"embedded={stats.records_embedded} "
                    f"units={stats.units_written} skipped={stats.records_skipped}",
                    flush=True,
                )
            await asyncio.sleep(interval_sec)


def _follow_embedder(name: str) -> Embedder:
    """Build a CPU-side embedder for follow mode; reject a disabled/unknown name."""
    embedder = registry.build_session_embedder(name)
    if embedder is None:
        raise SystemExit(f"--model {name!r} is empty/disabled; follow needs a model")
    return embedder


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
