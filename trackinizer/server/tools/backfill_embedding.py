#!/bin/sh
# ruff: noqa: EXE003, D300, D205, T201 -- Polyglot script; a CLI main that prints.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 -P "$0" "$@"
Backfill ``session_embeddings`` with dynamic load balancing across GPUs.

One coordinator scans ``session_records`` in keyset order and feeds page
bounds to a multiprocessing queue; N embed workers (one per GPU slot, pinned
via ``CUDA_VISIBLE_DEVICES``) pull pages as they free up, so a straggler page
never idles another GPU. Static hash-sharding measured 2x slower end-to-end:
the last shard ran alone for hours while its siblings' GPUs sat idle.

Each worker owns its own DB connection, skips records whose freshness marker
``text_md5`` already matches (the sweep predicate, so re-runs resume at scan
speed), embeds unit texts length-sorted so a forward batch pads to similar
lengths, and writes one ``unnest`` DELETE+INSERT per flush. The library sweep
(``store/session_embed.py``) remains the steady-state ingest path; this
driver exists because backfill is throughput-bound and the sweep batches only
within one page.

A run is resumable: every pool commits its vectors with its markers, so a killed
run loses only what its workers held, and the next run reads on from the
markers. A failed run runs again while each run cuts the pending count. A run
holds the model's writer lock (``sole_writer``) from its first page until its
workers exit, and refuses to start while another process writes that model.

The models come from ``--model``, else from the server's own setting
(``TRACKINIZER_SESSION_EMBEDDERS``, else ``TRACKINIZER_SESSION_EMBEDDER``), so
the follow service maintains exactly what the server searches. ``--follow``
with no model configured exits 0: semantic search is off.

Examples:
  ./backfill_embedding.py "$TRACKINIZER_DSN" --gpus 0,1
  ./backfill_embedding.py "$TRACKINIZER_DSN" --gpus 0 --workers-per-gpu 2
  ./backfill_embedding.py "$TRACKINIZER_DSN" --follow --gpu-backfill --gpus 0,1

'''
# fmt: on

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.sharedctypes import Synchronized
from pathlib import Path
from queue import Full
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from uuid import UUID

import argparse
import asyncio
import ctypes
import hashlib
import multiprocessing
import os
import subprocess
import time

from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.config import Config
from trackinizer.server.embedders import registry
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.session_embed import (
    current_snapshot,
    embed_texts,
    pending_page_sql,
    pending_where,
    save_cursor,
    sole_writer,
    sweep_session_embeddings,
    write_pool,
)
from trackinizer.server.store.session_index import ensure_model_index
from trackinizer.server.tools import model_buckets
from trackinizer.server.values import manifest_bound, vetted_sql


if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping, Sequence
    from multiprocessing import Queue

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.semantic_mapper import SemanticMapper
    from trackinizer.server.store.session_embed import Pool
    from trackinizer.server.tools.bucket_embed import Ticket
    from trackinizer.types.embedder import Embedder

    # (created, session_id, part, idx) -- the console-feed keyset. The queued
    # form stringifies the UUID so the tuple pickles cheaply.
    type RecordKey = tuple[datetime, UUID, int, int]
    type QueuedKey = tuple[datetime, str, int, int]

# (lo_key, hi_key): the page's first and last pending rows, both inclusive.
type PageTask = tuple["QueuedKey", "QueuedKey"]


def main() -> int:
    """Run the backfill, or the follow service with ``--follow``.

    Returns:
      exit_code: 0 when every worker exited cleanly, 1 otherwise.

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast("Flags", parser.parse_args())
    models = [name.strip() for name in flags.model.split(",") if name.strip()] or list(
        Config.from_env().maintained_embedders(),
    )
    gpus = [gpu.strip() for gpu in flags.gpus.split(",") if gpu.strip()]
    if flags.follow:
        if not models:
            print(
                "follow: no session embedder is configured "
                "(TRACKINIZER_SESSION_EMBEDDER is blank), so semantic search is off "
                "and there is nothing to maintain",
                flush=True,
            )
            return 0
        if flags.gpu_backfill:
            _backfill_on_idle_gpus(flags, models=models, gpus=gpus)
        # After the GPU workers, which are processes of their own, and before this
        # process's first torch import, which reads both once. Without hiding the
        # GPUs the CPU loop's compile opened a CUDA context that held 500 MB of GPU 0
        # (2026-10-31), so the card read as busy to every idle check after it.
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        _cap_cpu_threads(flags.cpu_threads)
        print(f"follow: embedding on CPU with {flags.cpu_threads} threads", flush=True)
        return asyncio.run(_run_follow(flags.dsn, models, flags.interval_sec))
    if not models:
        parser.error("no --model and no TRACKINIZER_SESSION_EMBEDDER: name a model")
    # A CPU slot is the empty-string device: CUDA_VISIBLE_DEVICES="" hides
    # every GPU from that process and the worker falls back to CPU inference.
    slots = gpus * flags.workers_per_gpu + [""] * flags.cpu_workers
    if not slots:
        parser.error("no worker slots: pass --gpus and/or --cpu-workers")
    # The GPU pipeline backfills ONE model per invocation (run it again per
    # model to bulk-backfill several); --follow above maintains all of them.
    resolved = registry.resolved_name(models[0], flags.dim)
    pending = asyncio.run(_pending_count(flags.dsn, model=resolved))
    if not pending:
        print(f"nothing pending for {resolved}", flush=True)
        return 0
    try:
        done = _backfill_while_advancing(
            flags,
            model=models[0],
            resolved=resolved,
            slots=slots,
            pending=pending,
        )
    except _WriterBusyError as error:
        print(error, flush=True)
        return 1
    return 0 if done else 1


class Flags(Protocol):
    """Parsed command-line flags."""

    dsn: str
    gpus: str
    workers_per_gpu: int
    cpu_workers: int
    page: int
    flush_units: int
    hold_units: int
    batch_size: int
    model: str
    dim: int | None
    gpu_vram_gb: float | None
    compile_cache: Path
    follow: bool
    interval_sec: int
    cpu_threads: int
    gpu_backfill: bool
    gpu_backfill_min_records: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _WorkerConfig:
    """The per-worker payload, one positional argument across the mp boundary.

    Bundled into one frozen dataclass because multiprocessing ``args`` are always
    positional (keyword-only params cannot cross), so the worker functions take
    one config rather than eight positionals.

    Attributes:
      dsn: Postgres DSN.
      gpu: CUDA device id to pin (empty string is a CPU worker).
      flush_units: Unit texts pooled per embed+write flush.
      hold_units: Unit texts the embed stage may hold across pools while their
        length buckets fill.
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
    hold_units: int
    batch_size: int
    model: str
    dim: int | None
    gpu_vram_gb: float | None
    compile_cache: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class _Progress:
    """Run-wide progress shared by every worker.

    Attributes:
      pending: Records pending for the run, counted once before any worker writes.
      done: Records written so far across all workers (a process-shared counter).

    """

    pending: int
    done: Synchronized[int]


class _NoLiveWorkersError(RuntimeError):
    """Every embed worker exited while the scanner still had pages to feed."""


class _WriterBusyError(RuntimeError):
    """Another process holds the model's writer lock, so the run wrote nothing."""


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
    # A bucket's forward runs at the card's rows: 11,026 for the 0.6B's shortest
    # bucket on a 5090, which ~19% of units route to, so ~57k units fill it.
    parser.add_argument(
        "--hold-units",
        type=int,
        default=65_536,
        help="Unit texts a GPU worker may hold across pools while buckets fill.",
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
        "pipeline backfills the FIRST; --follow maintains ALL. Empty takes the "
        "server's setting: TRACKINIZER_SESSION_EMBEDDERS, else "
        "TRACKINIZER_SESSION_EMBEDDER.",
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
        type=float,
        default=None,
        help="Override the per-card VRAM (GB) the bucket rows are derived for; "
        "omit to query the pinned device (containers may misreport memory).",
    )
    parser.add_argument(
        "--compile-cache",
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
        help="Keep maintaining the corpus: loop the library sweep per model, "
        "sleeping --interval-sec between rounds. Each round reads only the "
        "records changed since the last; the first round of a model with no "
        "cursor reads every pending record.",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=60,
        help="Seconds to sleep between --follow sweep rounds (default 60).",
    )
    # Measured 2026-10-31 on the production host (Zen 5, 64 cores, AVX-512 BF16), the
    # 0.6B model in bf16, compiled and frozen, batches of 8: 9.0 vec/s at 16 threads,
    # 14.1 at 32, 15.7 at 64. Past half the physical cores a thread buys little and
    # takes a core the box's other work needs.
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=max(1, (os.cpu_count() or 4) // 4),
        help="Torch threads --follow embeds with on CPU (default: a quarter of "
        "the logical CPUs, the measured knee).",
    )
    parser.add_argument(
        "--gpu-backfill",
        action="store_true",
        help="With --follow: before the CPU loop, embed each model's pending "
        "records on whichever --gpus no other process is using, when at least "
        "--gpu-backfill-min-records are pending (a new model, or a backlog).",
    )
    parser.add_argument(
        "--gpu-backfill-min-records",
        type=int,
        default=1_000,
        help="Pending records below which --gpu-backfill leaves the work to the "
        "CPU loop: loading and compiling the model on a GPU costs about what the "
        "CPU spends on this many.",
    )


# The lock is held from before the first page until every worker exits, so no sweep
# takes the pair while a worker still writes the last pages. The workers start before
# this runs (forked outside any event loop), so a refused run gives each its sentinel
# rather than leave it waiting for pages.
async def _scan_as_sole_writer(
    dsn: str,
    queue: Queue[PageTask | None],
    page: int,
    workers: Sequence[multiprocessing.Process],
    *,
    mapper_name: str,
    model: str,
    indexed_kinds: Sequence[str],
) -> None:
    """Scan ``model``'s pending pages as its one writer, until the workers exit."""

    def alive() -> bool:
        return any(worker.is_alive() for worker in workers)

    async with (
        PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine,
        engine.acquire() as conn,
        sole_writer(conn, mapper=mapper_name, model=model) as held,
    ):
        if not held:
            await _close_feed(queue, len(workers), alive=alive)
            raise _WriterBusyError(
                f"another process is embedding {model}; this run wrote nothing",
            )
        await _scan(
            dsn,
            queue,
            page,
            len(workers),
            mapper_name=mapper_name,
            model=model,
            indexed_kinds=indexed_kinds,
            alive=alive,
        )
        await asyncio.to_thread(_join, workers)


def _join(workers: Sequence[multiprocessing.Process]) -> None:
    """Wait for every worker to exit."""
    for worker in workers:
        worker.join()


# Each task is the page's first and last pending keys, both inclusive, so a worker's
# span read covers every pending row the page walked.
async def _scan(
    dsn: str,
    queue: Queue[PageTask | None],
    page: int,
    n_workers: int,
    *,
    mapper_name: str,
    model: str,
    indexed_kinds: Sequence[str],
    alive: Callable[[], bool],
    poll_sec: float = 1.0,
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
            rows = await conn.fetch(
                pending_page_sql(
                    join,
                    predicate,
                    first=cursor is None,
                    columns="r.created, r.session_id, r.part, r.idx",
                ),
                mapper_name,
                model,
                kinds,
                *(cursor or ()),
                page,
            )
            if not rows:
                break
            last = _key(rows[-1])
            await asyncio.to_thread(
                _put,
                queue,
                (_queued(_key(rows[0])), _queued(last)),
                alive=alive,
                poll_sec=poll_sec,
            )
            cursor = last
            fed += len(rows)
            elapsed = time.monotonic() - start
            print(
                f"scanner: fed={fed} pending ({fed / elapsed:.0f} rec/s)",
                flush=True,
            )
    await _close_feed(queue, n_workers, alive=alive, poll_sec=poll_sec)


async def _close_feed(
    queue: Queue[PageTask | None],
    n_workers: int,
    *,
    alive: Callable[[], bool],
    poll_sec: float = 1.0,
) -> None:
    """Put one sentinel per worker; each exits once it reaches its sentinel."""
    for _ in range(n_workers):
        await asyncio.to_thread(_put, queue, None, alive=alive, poll_sec=poll_sec)


def _put(
    queue: Queue[PageTask | None],
    item: PageTask | None,
    *,
    alive: Callable[[], bool],
    poll_sec: float,
) -> None:
    """Put ``item``, re-checking every ``poll_sec`` that some consumer is alive."""
    while True:
        try:
            queue.put(item, timeout=poll_sec)
        except Full:
            if not alive():
                raise _NoLiveWorkersError(
                    "every embed worker exited; aborting the scan",
                ) from None
        else:
            return


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


def _worker_entry(
    config: _WorkerConfig,
    queue: Queue[PageTask | None],
    progress: _Progress,
) -> None:
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
        cpus = os.cpu_count()
        _cap_cpu_threads(max(1, cpus // 8) if cpus is not None else 1)
    asyncio.run(_worker(config, "cuda:0" if config.gpu else "cpu", queue, progress))


# Torch sizes its intra-op pool from OMP_NUM_THREADS at its first import, and left
# alone it takes one thread per physical core: all 64 of the production host, which
# the live follow service held for an hour on a backlog (2026-10-31).
def _cap_cpu_threads(threads: int) -> None:
    """Cap this process's torch CPU threads; call before torch is imported."""
    os.environ["OMP_NUM_THREADS"] = str(threads)


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


# Each stage is its own coroutine on its own connection so the GPU never waits on the
# database: the reader prefetches and md5-filters the next pages while the embedder
# runs, and finished vectors are written behind the next forward pass. Bounded queues
# (2) cap memory.
async def _worker(
    config: _WorkerConfig,
    device: str,
    queue: Queue[PageTask | None],
    progress: _Progress,
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
                indexed_kinds=sorted(mapper.embedded_kinds),
                flush_units=config.flush_units,
            ),
        )
        group.create_task(
            _embed_stage(pools, writes, embedder, edges, rows, hold=config.hold_units),
        )
        group.create_task(
            _write_stage(
                write_conn,
                writes,
                embedder=embedder,
                mapper_name=mapper.name,
                name=name,
                start=start,
                progress=progress,
            ),
        )
    # Clean exit only (a crash never reaches here, leaving the prior cache intact):
    # persist this run's freshly warmed compile artifacts for the next process.
    _save_compile_cache(config.compile_cache)
    print(f"{name} DONE in {time.monotonic() - start:.0f}s", flush=True)


# The span read re-applies the scanner's own pending filter (``pending_where``), so a
# row another worker finished since the scan is skipped by the same freshness oracle
# that queued it.
# Audience: internal -- an operator tool run on the server; no caller sees it.
async def _read_stage(
    conn: Conn,
    queue: Queue[PageTask | None],
    pools: asyncio.Queue[Pool | None],
    *,
    mapper: SemanticMapper,
    embedder: Embedder,
    indexed_kinds: Sequence[str],
    flush_units: int,
) -> None:
    """Fetch each page's still-pending rows and emit embed-ready pools."""
    join, predicate = manifest_bound("r")
    sql = vetted_sql(
        "SELECT r.created, r.session_id, r.part, r.idx, r.kind, r.text "
        "FROM session_records r ",
        join,
        "WHERE (r.created, r.session_id, r.part, r.idx) >= ($4, $5::uuid, $6, $7) "
        "AND (r.created, r.session_id, r.part, r.idx) <= ($8, $9::uuid, $10, $11) "
        "AND ",
        pending_where(predicate),
        " ORDER BY r.created, r.session_id, r.part, r.idx",
    )
    kinds = list(indexed_kinds)
    pool: Pool = []
    pooled_units = 0
    while True:
        task = await asyncio.to_thread(queue.get)
        if task is None:
            break
        lo, hi = task
        rows = await conn.fetch(sql, mapper.name, embedder.name, kinds, *lo, *hi)
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

    Only ``QwenFamilyEmbedder`` implements ``bucket_queue`` (Jina's custom
    ``encode`` and ``StubEmbedder`` do not). This runner feature-detects it to
    route through the fixed-shape core; every other embedder uses ``embed_batch``.
    Defined here (its only user) rather than in ``session_embed`` so it is not a
    private protocol unused in its own module.
    """

    dim: int
    name: str

    async def bucket_queue(
        self,
        *,
        edges: Sequence[int],
        rows: MutableMapping[int, int],
    ) -> _TextQueue:
        """Return a queue that embeds what is added as its length buckets fill."""
        ...


class _TextQueue(Protocol):
    """The two calls the embed stage makes on a ``BucketQueue``."""

    def add(self, texts: Sequence[str]) -> Ticket:
        """Queue ``texts``; return the ticket their vectors fill."""
        ...

    async def run(self, *, hold: int) -> None:
        """Forward full buckets, then oldest-first until ``hold`` texts wait."""
        ...


# One bucket queue spans every pool, so a forward runs full instead of padded out to
# the card's rows for one pool's few texts (14.4x the 0.6B's rate and 5.0x the 4B's
# on a 5090, 2026-10-31). A pool is written once all its texts ran, oldest pool
# first; the queue runs the oldest text's bucket whenever more than ``hold`` texts
# wait, so the oldest pool always finishes. Any other embedder embeds each pool on
# its own, shortest first.
async def _embed_stage(
    pools: asyncio.Queue[Pool | None],
    writes: asyncio.Queue[tuple[Pool, list[list[float]]] | None],
    embedder: Embedder,
    edges: Sequence[int],
    rows: MutableMapping[int, int],
    *,
    hold: int,
) -> None:
    """Embed each pool's unit texts; hand each pool to the writer once embedded."""
    if not isinstance(embedder, _BucketEmbedder):
        while (pool := await pools.get()) is not None:
            await writes.put((pool, await embed_texts(embedder, _pool_texts(pool))))
        await writes.put(None)
        return
    queue = await embedder.bucket_queue(edges=edges, rows=rows)
    waiting: deque[tuple[Pool, Ticket]] = deque()
    while (pool := await pools.get()) is not None:
        waiting.append((pool, queue.add(_pool_texts(pool))))
        await queue.run(hold=hold)
        while waiting and waiting[0][1].done:
            done, ticket = waiting.popleft()
            await writes.put((done, ticket.vectors))
    await queue.run(hold=0)
    for done, ticket in waiting:
        await writes.put((done, ticket.vectors))
    await writes.put(None)


def _pool_texts(pool: Pool) -> list[str]:
    """Return the unit texts of ``pool``, record by record."""
    return [unit.text for _row, units, _md5 in pool for unit in units]


# The pending denominator: INDEXABLE records with no up-to-date freshness marker
# for this ``(mapper, model)`` -- the sweep predicate as a COUNT, filtered to what
# the GPU pipeline embeds (the mapper's ``embedded_kinds`` + non-empty text).
# WITHOUT the indexable filter this counted every never-indexed / empty-text row as
# pending forever (3.88M live, dominated by empty AssistantMessage/Thinking), which
# is the leak that made three runs write zero. Measured 2026-10-31 on the
# production host: 22 s over 9.6M records. ``md5(r.text)`` is computed in-DB, matching the worker's
# app-side md5.
# Audience: internal -- an operator tool run on the server; no caller sees it.
async def _pending_count(dsn: str, *, model: str) -> int:
    """Return the number of embeddable records needing a (re-)embed for ``model``."""
    mapper = FootprintMapper()
    join, predicate = manifest_bound("r")
    async with (
        PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine,
        engine.acquire() as conn,
    ):
        value = await conn.fetchval(
            vetted_sql(
                "SELECT count(*) FROM session_records r ",
                join,
                "WHERE ",
                pending_where(predicate),
            ),
            mapper.name,
            model,
            sorted(mapper.embedded_kinds),
        )
    assert isinstance(value, int)
    return value


# One run-wide denominator, counted before any worker can write, and one run-wide
# done counter: per-worker counts made every worker report the global pending as
# its own shard, inflating the ETA by the worker count. The scanner walks only rows
# pending for THIS resolved model+mapper, so it needs the stored identity workers
# write (resolved torch-free, not by building the embedder). The pipeline produces
# ONLY vectors, so it scans the mapper's EMBEDDED kinds; fts-only records carry no
# vector and are marked by the library sweep (--follow), not this bulk path.
def _backfill(flags: Flags, *, model: str, slots: Sequence[str], pending: int) -> int:
    """Run the GPU/CPU worker pipeline over ``model``'s pending records."""
    scan_mapper = FootprintMapper()
    progress = _Progress(
        pending=pending,
        done=multiprocessing.Value(ctypes.c_int64, 0),
    )
    queue: Queue[PageTask | None] = multiprocessing.Queue(maxsize=len(slots) * 2)
    workers = [
        multiprocessing.Process(
            target=_worker_entry,
            args=(
                _WorkerConfig(
                    dsn=flags.dsn,
                    gpu=gpu,
                    flush_units=flags.flush_units,
                    hold_units=flags.hold_units,
                    batch_size=flags.batch_size,
                    model=model,
                    dim=flags.dim,
                    gpu_vram_gb=flags.gpu_vram_gb,
                    compile_cache=flags.compile_cache,
                ),
                queue,
                progress,
            ),
            name=f"embed-worker-{i}-{'gpu' + gpu if gpu else 'cpu'}",
        )
        for i, gpu in enumerate(slots)
    ]
    for worker in workers:
        worker.start()
    aborted = False
    try:
        asyncio.run(
            _scan_as_sole_writer(
                flags.dsn,
                queue,
                flags.page,
                workers,
                mapper_name=scan_mapper.name,
                model=registry.resolved_name(model, flags.dim),
                indexed_kinds=sorted(scan_mapper.embedded_kinds),
            ),
        )
    except _NoLiveWorkersError as error:
        # Nobody will drain the queued pages; without this the queue's feeder
        # thread blocks interpreter exit on them.
        queue.cancel_join_thread()
        print(error, flush=True)
        aborted = True
    finally:
        _join(workers)
    failed = sum(worker.exitcode != 0 for worker in workers)
    print(f"{failed} workers FAILED" if failed or aborted else "ALL DONE", flush=True)
    return 1 if failed or aborted else 0


# The snapshot is taken BEFORE the pending count, so a record changed while the GPUs
# work is newer than the cursor saved after them and the CPU loop reads it. The
# pipeline embeds only the embedded kinds; an fts-only record from before the
# snapshot keeps no marker, which costs nothing: it carries no vector, and nothing
# but a full scan reads it again. A failed run runs again while each run cuts the
# backlog. A run still failed at the end, or a model another process is writing,
# saves no cursor, so the CPU loop picks up where the markers say.
def _backfill_on_idle_gpus(
    flags: Flags,
    *,
    models: Sequence[str],
    gpus: Sequence[str],
) -> None:
    """Embed each model's backlog on the idle GPUs among ``gpus``, if large enough."""
    idle = _idle_gpus(gpus)
    if not idle:
        print(
            f"follow: no idle GPU among {list(gpus)}; the CPU loop catches up",
            flush=True,
        )
        return
    mapper_name = FootprintMapper().name
    for model in models:
        resolved = registry.resolved_name(model, flags.dim)
        snapshot = asyncio.run(_current_snapshot(flags.dsn))
        pending = asyncio.run(_pending_count(flags.dsn, model=resolved))
        if pending < flags.gpu_backfill_min_records:
            print(
                f"follow: {pending} pending for {resolved}; left to the CPU loop",
                flush=True,
            )
            continue
        print(
            f"follow: {pending} pending for {resolved}; embedding on GPUs {idle}",
            flush=True,
        )
        slots = [gpu for gpu in idle for _ in range(flags.workers_per_gpu)]
        try:
            done = _backfill_while_advancing(
                flags,
                model=model,
                resolved=resolved,
                slots=slots,
                pending=pending,
            )
        except _WriterBusyError as error:
            print(f"follow: {error}", flush=True)
            continue
        if done:
            asyncio.run(
                _save_cursor(
                    flags.dsn,
                    mapper=mapper_name,
                    model=resolved,
                    snapshot=snapshot,
                ),
            )


# Each pool commits its vectors with its markers, so a failed run loses only what its
# workers held, and the pending count says where it stopped. Another run starts there
# while each run cuts the count. A run that cuts nothing (a record that fails every
# time, a card that fails to load) hands the rest on instead of looping.
def _backfill_while_advancing(
    flags: Flags,
    *,
    model: str,
    resolved: str,
    slots: Sequence[str],
    pending: int,
) -> bool:
    """Run the pipeline, and again after each failed run that cut the backlog."""
    while _backfill(flags, model=model, slots=slots, pending=pending) != 0:
        left = asyncio.run(_pending_count(flags.dsn, model=resolved))
        if not left:
            return True
        if left >= pending:
            print(
                f"{resolved}: a run embedded nothing; {left} still pending",
                flush=True,
            )
            return False
        print(
            f"{resolved}: a run failed with {left} still pending; running again",
            flush=True,
        )
        pending = left
    return True


# Idle means no compute process holds the card and at most 512 MB of its memory is
# in use: the second guard covers a process nvidia-smi cannot name (another PID
# namespace), so a service already on the card keeps it. A card whose memory reads
# ``[N/A]`` shares the CPU's and is never taken. No nvidia-smi, or a driver that
# fails to answer, is no GPU.
def _idle_gpus(candidates: Sequence[str]) -> list[str]:
    """Return the ``candidates`` (CUDA indices) no other process is using."""
    try:
        cards = _nvidia_smi("--query-gpu=index,uuid,memory.used")
        busy = {row[0] for row in _nvidia_smi("--query-compute-apps=gpu_uuid")}
    except (OSError, subprocess.CalledProcessError):
        return []
    free = {
        index
        for index, uuid, used_mb in cards
        if uuid not in busy and used_mb.isdigit() and int(used_mb) <= 512
    }
    return [gpu for gpu in candidates if gpu in free]


def _nvidia_smi(query: str) -> list[list[str]]:
    """Run one ``nvidia-smi`` CSV query; return its rows as stripped fields."""
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell.
        ["nvidia-smi", query, "--format=csv,noheader,nounits"],  # noqa: S607 -- PATH lookup is the point.
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        [field.strip() for field in line.split(",")]
        for line in result.stdout.splitlines()
        if line.strip()
    ]


async def _current_snapshot(dsn: str) -> str:
    """Return the database's current snapshot (see ``current_snapshot``)."""
    async with PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine:
        return await current_snapshot(engine)


async def _save_cursor(dsn: str, *, mapper: str, model: str, snapshot: str) -> None:
    """Save ``(mapper, model)``'s cursor (see ``save_cursor``)."""
    async with PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine:
        await save_cursor(engine, mapper=mapper, model=model, snapshot=snapshot)


# ``done``, ``pending`` and the rate are all run-wide (every worker's writes), so each
# worker's ETA line estimates the whole run.
async def _write_stage(
    conn: Conn,
    writes: asyncio.Queue[tuple[Pool, list[list[float]]] | None],
    *,
    embedder: Embedder,
    mapper_name: str,
    name: str,
    start: float,
    progress: _Progress,
) -> None:
    """Write each embedded pool; report run-wide progress per flush."""
    seen = progress.done.value
    rate = 0.0  # EMA of run-wide records/sec.
    last = start
    while (item := await writes.get()) is not None:
        pool, vectors = item
        await write_pool(
            conn,
            pool,
            vectors,
            embedder=embedder,
            mapper_name=mapper_name,
        )
        with progress.done.get_lock():
            progress.done.value += len(pool)
            done = progress.done.value
        now = time.monotonic()
        rate = _update_rate(rate, done - seen, now - last)
        seen, last = done, now
        remaining = max(0, progress.pending - done)
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
    weight = float(0.5 ** (interval_sec / halflife_sec))
    return weight * prev + (1 - weight) * instant


def _format_eta(remaining: int, rate: float) -> str:
    """Return ``H:MM`` for ``remaining`` records at ``rate`` rec/s, or ``?`` if idle."""
    if rate <= 0 or remaining <= 0:
        return "?"
    seconds = int(remaining / rate)
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}:{minutes:02d}"


# The steady-state ingest path. Each round runs :func:`sweep_session_embeddings` per
# model, which reads only the records changed since that model's cursor, so a quiet
# round costs one index probe, not a scan. Each model's partial HNSW index is ensured
# once at startup so a fresh model is searchable without a migration.
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
                if stats.busy:
                    print(
                        f"follow: model={embedder.name} skipped: another process "
                        "is embedding it",
                        flush=True,
                    )
                    continue
                print(
                    f"follow: model={embedder.name} "
                    f"embedded={stats.records_embedded} units={stats.units_written}"
                    + (" (full scan)" if stats.full_scan else ""),
                    flush=True,
                )
            await asyncio.sleep(interval_sec)


# A real model is built for throughput, not as the server builds it to embed one
# query: compiled and frozen, in short length-sorted batches. Measured 2026-10-31 on
# the production host, the 0.6B model on 32 threads: 12.0-14.1 vec/s frozen against
# 3.6-6.7 eager, after ~70-100 s of compile the first round pays; eager batches of 8
# beat 16 (8.8 vs 3.6-6.7 vec/s), since a shorter batch pads less. A stub has
# nothing to compile.
def _follow_embedder(name: str) -> Embedder:
    """Build the follow loop's CPU embedder; reject a disabled/unknown name."""
    if not registry.is_weightless(name):
        return registry.build_backfill_embedder(name, device="cpu", batch_size=8)
    embedder = registry.build_session_embedder(name)
    if embedder is None:
        raise SystemExit(f"--model {name!r} is empty/disabled; follow needs a model")
    return embedder


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
