"""Shared Postgres substrate implementations.

The ``DatabaseEngine`` Protocol defines a small surface — async context manager
yielding a connection-pool ``acquire()`` + LISTEN/NOTIFY pub/sub. Two
concrete implementations satisfy it:

- ``PGliteEngine`` runs Postgres in-process via ``py-pglite`` (WASM).
  Default substrate; zero setup, single connection.
- ``PostgresEngine`` connects to an external Postgres via DSN with a
  proper asyncpg connection pool and native ``LISTEN/NOTIFY`` fan-out.

Both forward notifications through an in-process ``_LocalBus`` so
consumers can ``listen()`` uniformly (PGlite has no cross-connection
NOTIFY; PostgresEngine bridges the dedicated listener into the same bus).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from pathlib import Path
from typing import Final, Protocol, Self

import asyncio
import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time

from asyncpg import Connection, Record
from asyncpg.pool import PoolConnectionProxy
from filelock import FileLock, Timeout
from py_pglite import PGliteConfig, PGliteManager
from py_pglite.extensions import SUPPORTED_EXTENSIONS

import asyncpg
import asyncpg.pool

from trackinizer.lib.userdirs import cache_dir


type Conn = PoolConnectionProxy[Record] | Connection[Record]


"""An ``asyncpg`` connection -- pool-borrowed proxy or standalone."""


class BootSlotUnavailableError(RuntimeError):
    """No PGlite cold-start slot became free within the caller's deadline."""


class InstallLockUnavailableError(RuntimeError):
    """The shared PGlite npm-install lock stayed held past the deadline."""


class DatabaseEngine(Protocol):
    """Postgres-protocol substrate: connection-pool acquire + pub/sub bus.

    The only seam between PGlite (in-process) and real Postgres. Callers stay dialect-agnostic by depending only on this Protocol.
    """

    async def __aenter__(self) -> Self:
        """Start the substrate (spawn PGlite or open the asyncpg pool)."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Tear down the substrate and release all resources."""
        ...

    def acquire(self) -> AbstractAsyncContextManager[Conn]:
        """Borrow a connection for the duration of an ``async with`` block.

        Not reentrant: nesting ``acquire`` within an active ``acquire`` from
        the same task is unsupported. The single-connection PGlite substrate
        raises ``RuntimeError`` rather than deadlocking on a held lock.

        Returns:
          ctx: Async context manager yielding a ``Conn`` that is returned
            to the pool (or closed) on exit.

        """
        ...

    async def notify(self, channel: str, payload: str) -> None:
        """Publish ``payload`` on ``channel`` to all live listeners.

        Args:
          channel: Channel name; matches a string passed to ``listen``.
          payload: Message body; opaque to the bus (usually a row id).

        """
        ...

    def listen(self, channel: str) -> AsyncGenerator[str, None]:
        """Subscribe to ``channel`` and yield each published payload.

        Args:
          channel: Channel name; matches a string passed to ``notify``.

        Yields:
          payload: One message body per ``notify`` call, in publish order.

        """
        ...


PGLITE_DATA_DIRNAME: Final = "pglite-data"
"""Directory name (relative to ``workdir``) where persistent PGlite stores its
backing data when ``persist=True``. Surviving restarts simply means this
directory keeps its contents between Node process lifetimes."""


class PGliteEngine:
    """In-process Postgres via ``py-pglite``; in-process ``asyncio.Queue`` bus.

    When ``persist=True`` (the default) the engine writes its own
    ``pglite_manager.js`` with ``dataDir`` set to ``<workdir>/<PGLITE_DATA_DIRNAME>``
    so the database survives process restarts. ``persist=False`` falls back
    to py-pglite's default in-memory script (useful for tests).

    Concurrency caveat: PGlite does not lock its ``dataDir``. Running two
    ``PGliteEngine`` instances against the same ``workdir`` concurrently
    will corrupt the database. Single-user dev tool; callers are
    responsible for ensuring at most one engine per ``workdir``.

    Migration caveat: schema changes are applied by ``Store.bootstrap()``
    using ``CREATE ... IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` so
    upgrades pick up new columns automatically. There is no down-migration
    story; a future schema change that drops or narrows a column would
    need explicit handling.
    """

    def __init__(
        self,
        *,
        workdir: Path,
        extensions: Sequence[str] = ("pgvector",),
        persist: bool = True,
        use_tcp: bool = False,
        own_workdir: bool = False,
    ) -> None:
        self._workdir = workdir
        self._extensions = list(extensions)
        self._persist = persist
        # When set, the engine owns ``workdir`` as private scratch and removes the
        # whole directory on shutdown. Used by ephemeral servers given a unique
        # per-process workdir: cleanup must run inside the engine teardown (which
        # executes under uvicorn's SIGTERM-driven graceful shutdown), because
        # ``atexit`` does NOT fire when uvicorn exits on a signal. Defaults off so
        # every existing caller (which passes a workdir it manages itself, e.g. a
        # pytest ``tmp_path``) keeps its directory untouched.
        self._own_workdir = own_workdir
        # Transport for the PGlite listener. Default Unix domain socket: the
        # path is unique per engine, so there is no port to race (the old
        # EADDRINUSE collision is impossible). TCP is opt-in for callers that
        # need a network-reachable port -- a non-co-located client, a TCP
        # healthcheck, ad-hoc ``psql`` -- and inherits the ephemeral-port race
        # (mitigated by the boot retry in ``_start_with_retries``).
        self._use_tcp = use_tcp
        self._manager: PGliteManager | None = None
        self._conn: asyncpg.Connection[asyncpg.Record] | None = None
        self._lock = asyncio.Lock()
        self._bus = _LocalBus()

    async def __aenter__(self) -> Self:
        """Start PGlite and open one persistent asyncpg connection."""
        await self._start_with_retries()
        return self

    async def _start_with_retries(self) -> None:
        """Spawn Node + open the asyncpg connection, retrying transient boot faults.

        The PGlite listener is a per-engine Unix socket (unique path), so there
        is no port to race -- the old EADDRINUSE collision is gone. What remains
        is PGlite's WASM Postgres occasionally trapping during the boot DDL
        under heavy concurrent load (``RuntimeError: PGlite process died during
        startup``); each trap is independently recoverable, so retry the whole
        start a few times. The same retry applies when ``_live_conn`` rebuilds
        the substrate after a Node crash. A deterministic failure (bad
        extension, unwritable workdir) re-raises on the last attempt.
        """
        workdir = self._workdir
        await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
        # Warm the shared node_modules *before* taking a boot slot. The first
        # cold run does a one-time ``npm ci`` (up to ``_INSTALL_TIMEOUT_SECONDS``)
        # behind its own install lock; running it inside the slot would hold the
        # slot far past ``_BOOT_SLOT_STALE_SECONDS``, so a sibling would reclaim a
        # live holder and the semaphore would over-admit. Hoisting it out keeps
        # the slot wrapping only the bounded Node boot. Idempotent and
        # cross-process safe, so this is a fast no-op once the cache is warm.
        await asyncio.to_thread(_ensure_shared_node_modules)
        last_error: BaseException | None = None
        # A few attempts absorb a cluster of independent WASM traps without
        # masking a deterministic failure (which recurs on every attempt).
        for _ in range(5):
            # Hold a cold-start slot only across the boot itself: the heavy,
            # CPU-bound spawn+WASM-boot+first-connect is what starves siblings,
            # while the live engine afterwards is cheap. Re-claimed per attempt
            # and released in ``finally`` so a failed boot never leaks a slot.
            try:
                async with acquire_boot_slot_async():
                    await self._start_once(workdir)
                return
            except (OSError, asyncpg.PostgresError, RuntimeError) as err:
                # py-pglite signals a dead Node boot as a RuntimeError carrying
                # the Node stderr; a connect failure is OSError/PostgresError.
                # Both are transient under load -- retry. The final attempt's
                # error surfaces via the raise below. A slot timeout is NOT
                # transient (the pool is saturated or leaked), so it propagates.
                last_error = err
                await self._teardown_failed_start()
        raise RuntimeError("PGlite failed to start after 5 attempts") from last_error

    async def _start_once(self, workdir: Path) -> None:
        """One PGlite start attempt; raises on startup or connect failure.

        Concurrency: ``pglite_manager.js`` is rewritten in place under
        ``workdir``; each engine must own its own ``workdir`` (not guarded with
        a lock). In Unix-socket mode (default) the listener path is unique per
        engine (py-pglite bakes PID+UUID in), so two engines never contend. In
        TCP mode the port is picked free per attempt and the boot retry covers
        a lost ephemeral-port race.
        """
        # py-pglite's ``_setup_work_dir`` only writes ``pglite_manager.js`` when
        # missing, so a stale file from a prior run pins the old listener address
        # and makes the new run's readiness probe time out. Remove it each start.
        (workdir / "pglite_manager.js").unlink(missing_ok=True)
        if self._use_tcp:
            # TCP (opt-in): pick a free ephemeral port. Best-effort -- the kernel
            # may hand it to another process between our close and the Node's
            # rebind (TOCTOU), surfacing as EADDRINUSE; the boot retry re-picks.
            port = _pick_free_port()
            config = PGliteConfig(
                work_dir=workdir,
                use_tcp=True,
                tcp_port=port,
                extensions=self._extensions,
                log_level="ERROR",
                timeout=10,
            )
            if self._persist:
                _write_persistent_manager_js_tcp(
                    workdir, port=port, extensions=self._extensions
                )
        else:
            # Unix domain socket (default): the path is unique per instance, so
            # there is no port to race -- the EADDRINUSE collision class is gone
            # by construction.
            config = PGliteConfig(
                work_dir=workdir,
                use_tcp=False,
                extensions=self._extensions,
                log_level="ERROR",
                timeout=10,
            )
            if self._persist:
                # Generate our own script with ``dataDir`` baked in *before*
                # py-pglite checks; ``_setup_work_dir`` then leaves our file
                # alone. It listens on the unique socket path py-pglite expects.
                _write_persistent_manager_js_unix(
                    workdir,
                    socket_path=config.socket_path,
                    extensions=self._extensions,
                )
        # Symlink the shared node_modules so py-pglite skips its per-workdir
        # ``npm install`` (a ~20 MB install per fresh tmp dir otherwise).
        await asyncio.to_thread(_link_shared_node_modules, workdir)
        self._manager = PGliteManager(config)
        await asyncio.to_thread(self._manager.start)
        _drain_node_stdout(self._manager)
        self._conn = await self._open_conn()

    async def _teardown_failed_start(self) -> None:
        """Best-effort cleanup of partial state between retry attempts."""
        await self._shutdown()

    async def _shutdown(self) -> None:
        """Terminate the asyncpg connection then stop the Node subprocess.

        PGlite's Node does not acknowledge asyncpg's graceful close handshake,
        so waiting for ``close()`` burns the full timeout on every engine exit.
        Terminating the client socket is the correct boundary here: the Node
        process is stopped immediately afterwards and owns durable flushes.
        """
        if self._conn is not None:
            self._conn.terminate()
            self._conn = None
        if self._manager is not None:
            await asyncio.to_thread(self._manager.stop)
            self._manager = None

    async def __aexit__(self, *exc: object) -> None:
        """Close the connection, stop Node, and discard an owned scratch workdir."""
        del exc
        await self._shutdown()
        if self._own_workdir:
            # Engine-owned ephemeral scratch: remove the whole directory now,
            # inside the graceful (SIGTERM) shutdown, since ``atexit`` would not
            # run on a signal exit. Best effort -- a leaked dir is swept on the
            # next ephemeral boot by the server's stale-dir prune.
            await asyncio.to_thread(shutil.rmtree, self._workdir, ignore_errors=True)

    def acquire(self) -> _ConnGuard:
        """Yield the single connection under a lock; mimics ``Pool.acquire()``.

        The lock is non-reentrant: a nested ``acquire`` from the same task
        would deadlock, so the guard raises instead.
        """
        assert self._manager is not None, "engine not entered"
        return _ConnGuard(self._live_conn, self._lock)

    async def _open_conn(
        self,
        *,
        attempts: int = 12,
        backoff_seconds: float = 0.25,
    ) -> asyncpg.Connection[asyncpg.Record]:
        """Open one configured asyncpg connection to the running PGlite.

        ``pglite-socket`` 0.2's ``server.start()`` resolves before the WASM
        Postgres is actually accepting wire traffic, so the first connect after a
        fresh boot can land in that window and be dropped mid-handshake
        (``ConnectionDoesNotExistError`` / ``ConnectionRefusedError``). The
        server IS up -- only the readiness signal is eager -- so a short bounded
        connect-retry against the already-running Node closes the gap. This is
        distinct from the cold-start ``_start_with_retries`` loop, which re-spawns
        Node; here the process is healthy and only the connection needs a beat.

        Args:
          attempts: asyncpg connect attempts against the healthy Node. The
            window widens under heavy CPU contention (``pytest -n`` booting many
            Node children), so ``attempts`` and ``backoff_seconds`` together
            budget ~20s -- generous against saturation, yet fast to give up on a
            server that never accepts (each failed connect returns promptly).
          backoff_seconds: Linear backoff base between attempts (attempt ``k``
            waits ``k * backoff_seconds``), so the total wait grows to a few
            seconds.

        """
        assert self._manager is not None, "engine not entered"
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                conn = await asyncpg.connect(
                    dsn=self._manager.get_asyncpg_uri(),
                    ssl=False,
                    server_settings={},
                    statement_cache_size=0,
                    timeout=10,
                )
            except (asyncpg.PostgresError, OSError) as err:
                last_error = err
                await asyncio.sleep(backoff_seconds * (attempt + 1))
                continue
            await _init_connection(conn)
            return conn
        assert last_error is not None
        raise last_error

    async def _live_conn(self) -> asyncpg.Connection[asyncpg.Record]:
        """Return the persistent connection; restart Node + reconnect on death.

        The asyncpg socket and the PGlite Node child fail independently:
        Node can crash while ``_conn.is_closed()`` still reports False
        (the kernel hasn't surfaced the RST yet), or both can die
        together. If the manager is dead we rebuild the whole substrate
        before reopening the connection -- otherwise ``_open_conn``'s
        ``get_asyncpg_uri()`` raises ``RuntimeError`` and every caller
        gets a 500 until the process restarts.
        """
        if self._manager is None or not self._manager.is_running():
            await self._shutdown()
            await self._start_with_retries()
            assert self._conn is not None
            return self._conn
        if self._conn is None or self._conn.is_closed():
            self._conn = await self._open_conn()
        return self._conn

    async def notify(self, channel: str, payload: str) -> None:
        """Publish via the in-process bus (PGlite has no cross-conn NOTIFY)."""
        self._bus.publish(channel, payload)

    def listen(self, channel: str) -> AsyncGenerator[str, None]:
        """Yield messages published to ``channel``."""
        return self._bus.subscribe(channel)


class PostgresEngine:
    """External Postgres via DSN; native ``LISTEN/NOTIFY``."""

    def __init__(
        self,
        dsn: str,
        *,
        listen_channel: str,
        max_size: int = 8,
    ) -> None:
        self._dsn = dsn
        self._listen_channel = listen_channel
        self._max_size = max_size
        self._listener: asyncpg.Connection[asyncpg.Record] | None = None
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None
        self._bus = _LocalBus()

    @property
    def listen_channel(self) -> str:
        """Return the native Postgres channel bridged into the local bus."""
        return self._listen_channel

    async def __aenter__(self) -> Self:
        """Open the pool and a dedicated listener connection.

        ``__aexit__`` does not run when ``__aenter__`` raises (PEP 343), so
        partially-acquired resources must be released here on failure to avoid
        leaking the pool and listener connection.
        """
        pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=1,
            max_size=self._max_size,
            init=_init_connection,
        )
        listener: asyncpg.Connection[asyncpg.Record] | None = None
        try:
            listener = await asyncpg.connect(dsn=self._dsn)
            await _init_connection(listener)
            await listener.add_listener(self._listen_channel, self._on_notify)
        except BaseException:
            if listener is not None:
                await listener.close()
            await pool.close()
            raise
        self._pool = pool
        self._listener = listener
        return self

    def _on_notify(
        self,
        _c: asyncpg.Connection[asyncpg.Record]
        | asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
        _p: int,
        _ch: str,
        payload: object,
    ) -> None:
        """Forward a native NOTIFY payload onto the in-process bus."""
        self._bus.publish(_ch, str(payload))

    async def __aexit__(self, *exc: object) -> None:
        """Close listener and pool."""
        del exc
        if self._listener is not None:
            await self._listener.close()
            self._listener = None
        assert self._pool is not None
        await self._pool.close()

    def acquire(self) -> asyncpg.pool.PoolAcquireContext[asyncpg.Record]:
        """Acquire a connection from the pool."""
        assert self._pool is not None, "engine not entered"
        return self._pool.acquire()

    async def notify(self, channel: str, payload: str) -> None:
        """Send a NOTIFY via ``pg_notify`` (parameterised; ``NOTIFY`` syntax cannot)."""
        async with self.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", channel, payload)

    def listen(self, channel: str) -> AsyncGenerator[str, None]:
        """Yield messages forwarded from the dedicated listener connection."""
        return self._bus.subscribe(channel)


class _LocalBus:
    """In-process fan-out from a single publisher to N subscriber queues.

    Each subscriber queue is bounded; a stuck consumer cannot grow it
    without limit. On overflow we drop the oldest queued payload rather
    than the newest -- notification streams favour freshness over history.

    Not thread-safe: every ``publish``/``subscribe`` call must run on the one
    event loop that owns the engine. The ``asyncio.Queue`` objects are bound
    to that loop, so cross-thread access is unsupported.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    def publish(self, channel: str, payload: str) -> None:
        """Push ``payload`` onto every subscriber queue; drop oldest on overflow."""
        for q in self._subscribers.get(channel, ()):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                q.put_nowait(payload)

    async def subscribe(
        self, channel: str, *, queue_maxsize: int = 1024
    ) -> AsyncGenerator[str, None]:
        """Subscribe to ``channel`` and yield messages until the consumer exits.

        Args:
          channel: Channel name to subscribe to.
          queue_maxsize: Bound on this subscriber's queue; oldest payloads drop
            past this depth.

        """
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_maxsize)
        self._subscribers.setdefault(channel, set()).add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers[channel].discard(q)


class _ConnGuard:
    """Async context manager yielding a single persistent connection under a lock.

    Mimics the surface of ``Pool.acquire()`` for the PGlite case where
    a real pool's release-time reset would hang the WASM Postgres
    implementation.
    """

    def __init__(
        self,
        get_conn: Callable[[], Awaitable[asyncpg.Connection[asyncpg.Record]]],
        lock: asyncio.Lock,
    ) -> None:
        self._get_conn = get_conn
        self._lock = lock

    async def __aenter__(self) -> asyncpg.Connection[asyncpg.Record]:
        if self._lock.locked() and _lock_owner(self._lock) is asyncio.current_task():
            raise RuntimeError(
                "Reentrant acquire on the single PGlite connection would "
                "deadlock: the current task already holds the connection lock."
            )
        await self._lock.acquire()
        _set_lock_owner(self._lock, asyncio.current_task())
        try:
            return await self._get_conn()
        except BaseException:
            _set_lock_owner(self._lock, None)
            self._lock.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        del exc
        _set_lock_owner(self._lock, None)
        self._lock.release()


_LOCK_OWNER_ATTR: Final = "_loop_conn_guard_owner"


def _lock_owner(lock: asyncio.Lock) -> asyncio.Task[object] | None:
    """Return the task that currently holds ``lock`` via a guard, if any."""
    owner = getattr(lock, _LOCK_OWNER_ATTR, None)
    assert owner is None or isinstance(owner, asyncio.Task)
    return owner


def _set_lock_owner(lock: asyncio.Lock, task: asyncio.Task[object] | None) -> None:
    """Record (or clear) the task holding ``lock`` for reentrancy detection."""
    setattr(lock, _LOCK_OWNER_ATTR, task)


async def _init_connection(conn: Connection[Record]) -> None:
    """Per-connection setup: JSONB and NUMERIC codecs.

    Without the NUMERIC codec, asyncpg returns ``decimal.Decimal`` for
    ``NUMERIC`` columns -- mixing ``Decimal`` with the ``float`` typed
    cost fields on ``Change``/``Trackinoid`` would break downstream
    arithmetic. Cost precision is sub-cent and the agreed type is
    ``float`` (per project decision); the codec maps end-to-end.

    The NUMERIC decoder is ``float``, so values with more than ~15
    significant digits lose precision. This is acceptable only because the
    schema uses ``NUMERIC`` for sub-cent cost fields that fit a double; do
    not reuse this codec for high-precision columns.

    Vectors pass through as ``::vector`` text casts (no codec needed).
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "numeric",
        encoder=str,
        decoder=float,
        schema="pg_catalog",
        format="text",
    )


def _drain_node_stdout(manager: PGliteManager) -> None:
    """Drain Node's stdout pipe in a daemon thread.

    ``py-pglite`` spawns Node with ``stdout=PIPE`` and never reads it; under
    sustained traffic the ~64KB OS pipe buffer fills, Node blocks on
    ``write()``, and the postgres protocol hangs. We start a daemon thread
    that reads and discards until the process exits.

    The thread is single-shot per Node lifetime: it exits when ``readline``
    hits EOF on process death, so a fresh thread per restart is bounded by the
    number of restarts, not a leak.
    """
    proc = manager.process
    if proc is None or proc.stdout is None:
        return
    stdout = proc.stdout

    def _consume() -> None:
        try:
            for _ in iter(stdout.readline, ""):
                pass
        except (OSError, ValueError):
            pass

    threading.Thread(target=_consume, daemon=True).start()


# Vendored ``package.json`` + ``package-lock.json`` pin the exact dependency
# tree py-pglite's Node script ``require``s. We install from the lockfile with
# ``npm ci`` so every machine and run resolves byte-identical versions -- core
# infra (web app, trackinizer server) must boot deterministically, not whatever
# the registry served the day the cache first warmed. All extensions (pgvector,
# pg_trgm, ...) ship as subpaths of the base pglite package, so this one tree
# serves every ``extensions`` combination. Bumping a version = edit both files;
# the lockfile hash changes, minting a fresh cache key (see ``_cache_key``).
_PGLITE_PACKAGE_JSON = Path(__file__).parent / "pglite-package.json"
_PGLITE_PACKAGE_LOCK = Path(__file__).parent / "pglite-package-lock.json"

_INSTALL_TIMEOUT_SECONDS: Final = 300.0
"""Wall-clock ceiling on a single ``npm ci``."""

_INSTALL_LOCK_TIMEOUT_SECONDS: Final = 600.0
"""How long to wait for another process's ``npm ci`` before failing.

Exceeds ``_INSTALL_TIMEOUT_SECONDS`` so a waiter outlasts one full install by
the holder rather than failing while legitimate work is in flight. No stale
window is needed alongside it: the lock is released by the kernel when its
holder exits, so a killed installer frees it immediately instead of blocking
every other process for a fixed penalty."""


def _cache_key() -> str:
    """Content-address the cache by the vendored lockfile + package manifest.

    Keying on file *content* (not a hand-maintained version string) means any
    edit to either vendored file automatically mints a new cache directory, so
    a dependency bump can never be served a stale tree from an old key.
    """
    digest = hashlib.sha256()
    digest.update(_PGLITE_PACKAGE_JSON.read_bytes())
    digest.update(_PGLITE_PACKAGE_LOCK.read_bytes())
    return digest.hexdigest()[:16]


def _ensure_shared_node_modules() -> Path:
    """Install the PGlite npm deps once into a shared cache; return that dir.

    py-pglite runs ``npm install`` into *every* ``work_dir`` whose
    ``node_modules`` is missing. With a fresh ``tmp_path`` per test that is a
    full ~20 MB install per test, and N concurrent installs under ``pytest -n``
    thrash disk and the npm registry hard enough to blow the per-test timeout.

    Installing once into a content-addressed cache and symlinking each
    ``work_dir/node_modules`` at it (see :meth:`PGliteEngine._start_once`)
    collapses that to a single install shared across all engines and processes.

    Cross-process safe: a sibling ``.lock`` directory (atomic ``mkdir``) and a
    ``.ready`` marker serialise the install so concurrent pytest workers don't
    race a half-written tree. The marker is only written after ``npm ci``
    succeeds, so a crashed install never looks complete; a lock left behind by
    a killed installer is reclaimed once it ages past
    ``_INSTALL_LOCK_STALE_SECONDS``.

    Superseded keys are intentionally not pruned here. Existing Node children
    can still resolve modules through an older tree, and this cache has no
    reader leases that would make automatic deletion safe.
    """
    root = cache_dir() / "rekursiv-ai" / "pglite" / "node-modules" / _cache_key()
    node_modules = root / "node_modules"
    ready = root / ".ready"
    if ready.exists():
        return node_modules
    root.mkdir(parents=True, exist_ok=True)
    with _install_lock(root / ".lock"):
        # Re-check under the lock: the process we queued behind was very likely
        # doing this exact install, and ``npm ci`` wipes node_modules on entry.
        if not ready.exists():
            _run_npm_ci(root)
            ready.touch()
    return node_modules


@contextmanager
def _install_lock(
    lock_path: Path,
    *,
    timeout_sec: float = _INSTALL_LOCK_TIMEOUT_SECONDS,
) -> Generator[None]:
    """Hold the shared npm-install lock for the duration of the block.

    Args:
      lock_path: Lock file serialising ``npm ci`` across processes.
      timeout_sec: Seconds to wait before giving up.

    Raises:
      InstallLockUnavailableError: If the lock is not obtained in time.

    """
    lock = FileLock(lock_path)
    try:
        lock.acquire(timeout=timeout_sec)
    except Timeout as err:
        raise InstallLockUnavailableError(
            f"PGlite npm install lock at {lock_path} was held for more than "
            f"{timeout_sec}s",
        ) from err
    try:
        yield
    finally:
        lock.release()


def _run_npm_ci(root: Path) -> None:
    """Copy the vendored manifest + lockfile into ``root`` and ``npm ci``.

    ``npm ci`` installs strictly from the lockfile and refuses to proceed if
    the manifest and lockfile disagree, so the resolved tree is reproducible
    rather than whatever ``npm install`` would resolve caret ranges to today.
    """
    (root / "package.json").write_bytes(_PGLITE_PACKAGE_JSON.read_bytes())
    (root / "package-lock.json").write_bytes(_PGLITE_PACKAGE_LOCK.read_bytes())
    subprocess.run(
        ["npm", "ci", "--no-audit", "--no-fund"],  # noqa: S607 -- fixed args, no shell
        cwd=root,
        capture_output=True,
        check=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
    )


def _link_shared_node_modules(work_dir: Path) -> None:
    """Point ``work_dir/node_modules`` at the shared install if it isn't present.

    A pre-existing ``node_modules`` (e.g. a persisted ``work_dir`` from a prior
    run) is left untouched. Otherwise we symlink the shared tree, which both
    satisfies py-pglite's ``node_modules.exists()`` install-skip check and lets
    its ``find_pglite_modules`` parent-walk resolve ``NODE_PATH``.
    """
    target = work_dir / "node_modules"
    if target.exists() or target.is_symlink():
        return
    shared = _ensure_shared_node_modules()
    # On any link failure (lost race, or a filesystem without symlinks) leave
    # ``target`` absent: py-pglite then runs its own per-workdir ``npm install``
    # -- slower, but a correct fallback rather than a failed engine start.
    with suppress(OSError):
        target.symlink_to(shared, target_is_directory=True)


def _real_sleep(seconds: float) -> None:
    """Block the calling thread; isolated so tests can patch the install wait."""
    time.sleep(seconds)


# -- Cold-start concurrency gate ------------------------------------------------
#
# Each PGlite engine is a single-threaded WASM Node child. Its ~2.5s cold start
# (spawn Node, boot WASM Postgres, accept the first asyncpg connection) is
# CPU-bound, and ``pytest -n`` boots many engines at once -- one per xdist
# worker, plus several per worker for the multi-engine suites. With nothing
# bounding that fan-out the Node children starve each other off the CPU during
# the readiness window, so a boot's asyncpg connect races a not-yet-listening
# (``ConnectionRefusedError``) or already-overwhelmed (``ConnectionDoesNotExistError``,
# "did not start in time") socket. The install lock below serialises ``npm ci``
# only and is a no-op once the cache is warm, so it does NOT cover this.
#
# A cross-process counting semaphore caps simultaneous cold starts so each Node
# gets enough CPU to finish booting before the next begins. Each of the
# ``slot-<i>`` files is an OS advisory lock (``filelock``): a booter holds one
# across its boot and the kernel drops it when the process exits, however it
# exits.
#
# Liveness is therefore observed, never inferred. An earlier version claimed
# slots with ``mkdir`` and guessed whether a holder was alive from the
# directory's mtime, which cannot be right in both directions: too short a
# window steals slots from a slow-but-live boot (measured at 48 concurrent cold
# starts on a 128-cpu host: holds reached ~126s against a 60s window and 16 of
# 51 live slots were reclaimed, over-admitting the semaphore until boots failed
# outright), while too long a window leaves a ``kill -9``'d holder squatting its
# slot for the remainder of the window and wedging every waiter. A heartbeat
# only moves the tradeoff. A kernel-released lock removes it: a killed holder's
# slot is measurably free within a millisecond.

_BOOT_SLOT_TIMEOUT_SECONDS: Final = 300.0
"""How long a booter waits for a free cold-start slot before failing.

Bounded on purpose. An unbounded wait converts one leaked slot into a permanent
hang for every future caller, which surfaces far from its cause -- as unrelated
tests timing out rather than as a lock error. Generous enough to cover a full
pool of legitimately slow boots, short enough that a genuine deadlock is
reported rather than waited on forever."""


def _max_concurrent_boots() -> int:
    """Cap on simultaneous PGlite cold starts across all processes.

    Scaled off the host CPU count but held low: starvation appears well before
    the cores are saturated (each Node is single-threaded and the boot is a
    burst of contention), so a small ceiling keeps boots fast without
    re-introducing the race. Always at least 2 so a single multi-engine test
    still overlaps. Tests pin it by patching this function.
    """
    return max(2, min(8, (os.cpu_count() or 4) // 8))


def _boot_slots_root() -> Path:
    """Directory holding the cold-start semaphore's slot dirs.

    Shares the XDG cache base with the node-modules install, but scopes slots
    by host. The cache may be network-mounted across nodes, while the CPU
    contention this semaphore controls is host-local.
    """
    host_key = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]
    return cache_dir() / "rekursiv-ai" / "pglite" / "boot-slots" / host_key


def _boot_slot_lock_path(index: int) -> Path:
    """Return the lock file backing cold-start slot ``index``."""
    return _boot_slots_root() / f"slot-{index}.lock"


@contextmanager
def acquire_boot_slot(
    *,
    timeout_sec: float = _BOOT_SLOT_TIMEOUT_SECONDS,
) -> Generator[int]:
    """Hold one cold-start slot for the duration of the block.

    Sweeps the pool for a slot whose lock is free, then waits for whichever
    frees first. Each attempt builds a FRESH ``FileLock``: an instance is
    reentrant by design (``lock_counter``), so reusing one would let a single
    caller re-enter a lock it already holds and silently exceed the cap.

    Args:
      timeout_sec: Total seconds to wait for a slot before giving up.

    Yields:
      index: The claimed slot's index.

    Raises:
      BootSlotUnavailableError: If no slot frees within ``timeout_sec``.

    """
    root = _boot_slots_root()
    root.mkdir(parents=True, exist_ok=True)
    limit = _max_concurrent_boots()
    deadline = time.monotonic() + timeout_sec
    while True:
        for index in range(limit):
            lock = FileLock(_boot_slot_lock_path(index))
            try:
                lock.acquire(blocking=False)
            except Timeout:
                continue
            try:
                yield index
            finally:
                lock.release()
            return
        if time.monotonic() >= deadline:
            raise BootSlotUnavailableError(
                f"no PGlite cold-start slot became free within {timeout_sec}s "
                f"({limit} slots under {root})",
            )
        _real_sleep(0.05)


@asynccontextmanager
async def acquire_boot_slot_async(
    *,
    timeout_sec: float = _BOOT_SLOT_TIMEOUT_SECONDS,
) -> AsyncGenerator[int]:
    """Async wrapper over :func:`acquire_boot_slot` that never blocks the loop.

    The claim and release are pushed to worker threads, and the whole wait runs
    on ONE thread so acquire and release share it -- ``filelock`` is
    thread-local by default, so a release from a different thread than the
    acquire would not drop the lock.

    Args:
      timeout_sec: Total seconds to wait for a slot before giving up.

    Yields:
      index: The claimed slot's index.

    """
    released = threading.Event()
    acquired: queue.Queue[int | BaseException] = queue.Queue(maxsize=1)

    def hold() -> None:
        try:
            with acquire_boot_slot(timeout_sec=timeout_sec) as index:
                acquired.put(index)
                released.wait()
        except BaseException as err:  # noqa: BLE001 -- relayed to the caller
            acquired.put(err)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    claimed = await asyncio.to_thread(acquired.get)
    if isinstance(claimed, BaseException):
        raise claimed
    try:
        yield claimed
    finally:
        released.set()
        await asyncio.to_thread(thread.join)


_EXTENSION_JS: Final = {
    "pgvector": ("vector", "@electric-sql/pglite-pgvector"),
}
"""Mapping ``pglite-extension-name -> (JS-symbol, npm-module)``.

The single source of truth for how the vendored Node env loads each PGlite
extension. As of ``pglite`` 0.5 pgvector ships as the standalone
``@electric-sql/pglite-pgvector`` package (it left the core ``pglite``
``/vector`` subpath). :func:`_align_pyglite_extension_modules` pushes this map
into ``py_pglite.extensions.SUPPORTED_EXTENSIONS`` at import so the
non-persistent boot path (which delegates its ``pglite_manager.js`` to
py-pglite) and the persistent path (which writes its own JS from this map) load
the extension from the same, correct module.
"""


def _align_pyglite_extension_modules() -> None:
    """Override py-pglite's extension module paths with :data:`_EXTENSION_JS`.

    py-pglite 0.5.3 still maps pgvector to the pre-0.5 core subpath
    ``@electric-sql/pglite/vector``, which the bumped ``pglite`` no longer
    exports. The non-persistent manager generates its ``pglite_manager.js`` from
    ``py_pglite.extensions.SUPPORTED_EXTENSIONS``, so left unpatched it would
    ``require`` a missing module and the Node boot fails with
    ``ERR_PACKAGE_PATH_NOT_EXPORTED``. Reconcile the two tables here, keeping
    :data:`_EXTENSION_JS` the single authority.
    """
    for name, (symbol, module) in _EXTENSION_JS.items():
        SUPPORTED_EXTENSIONS[name] = {"name": symbol, "module": module}


_align_pyglite_extension_modules()


def _pick_free_port() -> int:
    """Return an ephemeral TCP port the OS just assigned us (TCP mode only).

    Best-effort: the socket is closed before PGlite's Node rebinds the port, so
    a concurrent booter can steal it in the gap (a TOCTOU window). Used only
    when ``PGliteEngine(use_tcp=True)``; the boot retry in
    ``_start_with_retries`` re-picks on the resulting EADDRINUSE.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert isinstance(port, int)
        return port


def _persist_js_extension_parts(extensions: Sequence[str]) -> tuple[str, str]:
    """Return ``(require_lines, extensions_object)`` JS fragments for a manager.

    Shared by the Unix- and TCP-socket persist templates: each validates the
    extension names against :data:`_EXTENSION_JS` and emits the ``require()``
    lines plus the ``{name: symbol, ...}`` object literal PGlite expects.
    """
    ext_requires: list[str] = []
    ext_configs: list[str] = []
    for name in extensions:
        if name not in _EXTENSION_JS:
            raise ValueError(
                f"unknown pglite extension {name!r}; add it to _EXTENSION_JS"
            )
        symbol, module = _EXTENSION_JS[name]
        ext_requires.append(f"const {{ {symbol} }} = require('{module}');")
        ext_configs.append(f"{name}: {symbol}")
    ext_requires_str = "\n".join(ext_requires)
    ext_configs_str = ", ".join(ext_configs) or ""
    extensions_obj = f"{{ {ext_configs_str} }}" if ext_configs_str else "{}"
    return ext_requires_str, extensions_obj


def _write_persistent_manager_js_unix(
    workdir: Path,
    *,
    socket_path: str,
    extensions: Sequence[str],
) -> None:
    """Write a Unix-socket ``pglite_manager.js`` opening a persistent ``dataDir``.

    Matches py-pglite's Unix-socket template -- ``path:``-mode
    ``PGLiteSocketServer``, stale-socket cleanup, SIGINT/SIGTERM handlers -- with
    ``new PGlite({dataDir, extensions})`` added. The data dir is absolute (under
    ``workdir``) so it survives Node cwd changes. Listens on the unique
    ``socket_path`` py-pglite minted, so there is no port to race. Not
    idempotent: the caller unlinks any stale file first.
    """
    data_dir = workdir / PGLITE_DATA_DIRNAME
    ext_requires_str, extensions_obj = _persist_js_extension_parts(extensions)
    script = f"""\
const {{ PGlite }} = require('@electric-sql/pglite');
const {{ PGLiteSocketServer }} = require('@electric-sql/pglite-socket');
const {{ existsSync }} = require('fs');
const {{ unlink }} = require('fs/promises');
{ext_requires_str}

const SOCKET_PATH = {json.dumps(socket_path)};

async function cleanup() {{
    if (existsSync(SOCKET_PATH)) {{
        try {{ await unlink(SOCKET_PATH); }} catch (err) {{}}
    }}
}}

async function startServer() {{
    try {{
        const db = new PGlite({{
            dataDir: {json.dumps(str(data_dir))},
            extensions: {extensions_obj}
        }});
        await cleanup();
        const server = new PGLiteSocketServer({{
            db,
            path: SOCKET_PATH,
        }});
        await server.start();
        console.log(`Server started on socket ${{SOCKET_PATH}}`);

        const shutdown = async () => {{
            try {{ await server.stop(); }} catch (err) {{}}
            try {{ await db.close(); }} catch (err) {{}}
            process.exit(0);
        }};
        process.on('SIGINT', shutdown);
        process.on('SIGTERM', shutdown);
    }} catch (err) {{
        console.error('Failed to start PGlite server:', err);
        process.exit(1);
    }}
}}

startServer();
"""
    (workdir / "pglite_manager.js").write_text(script)


def _write_persistent_manager_js_tcp(
    workdir: Path,
    *,
    port: int,
    extensions: Sequence[str],
) -> None:
    """Write a TCP ``pglite_manager.js`` opening a persistent ``dataDir``.

    The TCP counterpart of :func:`_write_persistent_manager_js_unix`: a
    ``host``/``port``-mode ``PGLiteSocketServer`` on ``127.0.0.1:{port}``. Only
    used when the engine is opened with ``use_tcp=True``; ``port`` is baked into
    the script, so the caller unlinks any stale file before each start.
    """
    data_dir = workdir / PGLITE_DATA_DIRNAME
    ext_requires_str, extensions_obj = _persist_js_extension_parts(extensions)
    script = f"""\
const {{ PGlite }} = require('@electric-sql/pglite');
const {{ PGLiteSocketServer }} = require('@electric-sql/pglite-socket');
{ext_requires_str}

async function startServer() {{
    try {{
        const db = new PGlite({{
            dataDir: {json.dumps(str(data_dir))},
            extensions: {extensions_obj}
        }});
        const server = new PGLiteSocketServer({{
            db,
            host: '127.0.0.1',
            port: {port},
        }});
        await server.start();
        console.log(`Server started on TCP 127.0.0.1:{port}`);

        const shutdown = async () => {{
            try {{ await server.stop(); }} catch (err) {{}}
            try {{ await db.close(); }} catch (err) {{}}
            process.exit(0);
        }};
        process.on('SIGINT', shutdown);
        process.on('SIGTERM', shutdown);
    }} catch (err) {{
        console.error('Failed to start PGlite server:', err);
        process.exit(1);
    }}
}}

startServer();
"""
    (workdir / "pglite_manager.js").write_text(script)
