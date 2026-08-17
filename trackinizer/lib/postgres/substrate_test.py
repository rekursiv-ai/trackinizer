"""Tests for the shared Postgres substrate."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import asyncio
import hashlib
import subprocess
import sys
import time

import asyncpg
import pytest

from trackinizer.lib.postgres import PGliteEngine, PostgresEngine, substrate
from trackinizer.lib.userdirs import cache_dir


def _cache_dir_under(root: Path) -> Callable[[], Path]:
    """Typed ``cache_dir`` stub rooting the substrate's XDG cache at ``root``."""

    def _cache_dir() -> Path:
        return root / "caches"

    return _cache_dir


def test_pglite_default_caches_use_xdg_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "test-host")

    host_key = hashlib.sha256(b"test-host").hexdigest()[:16]
    assert substrate._boot_slots_root() == (
        cache_dir() / "rekursiv-ai" / "pglite" / "boot-slots" / host_key
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pglite_persistent_restart_retains_rows(tmp_path: Path) -> None:
    """PGlite rows survive engine restart when persistence is enabled."""
    workdir = tmp_path / "pg"
    async with (
        PGliteEngine(workdir=workdir, extensions=()) as engine,
        engine.acquire() as conn,
    ):
        await conn.execute("CREATE TABLE durable_items (id text PRIMARY KEY)")
        await conn.execute("INSERT INTO durable_items (id) VALUES ($1)", "kept")

    async with (
        PGliteEngine(workdir=workdir, extensions=()) as restarted,
        restarted.acquire() as conn,
    ):
        assert await conn.fetchval("SELECT id FROM durable_items") == "kept"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pglite_listen_notify_round_trips_payload(tmp_path: Path) -> None:
    """PGlite exposes the same listen/notify surface as external Postgres."""
    async with PGliteEngine(workdir=tmp_path / "pg", extensions=()) as engine:
        notifications = engine.listen("events")
        next_payload = asyncio.create_task(_anext(notifications))
        await asyncio.sleep(0)
        await engine.notify("events", "payload-1")
        assert await next_payload == "payload-1"
        await notifications.aclose()


@pytest.mark.asyncio
async def test_pglite_acquire_reopens_dropped_connection(tmp_path: Path) -> None:
    """Reopen a PGlite connection when Node side-closes the prior connection."""
    engine = PGliteEngine(workdir=tmp_path / "pg", extensions=())
    manager = MagicMock()
    manager.get_asyncpg_uri.return_value = "postgresql://x"
    engine._manager = manager
    dead = _make_conn()
    dead.is_closed = MagicMock(return_value=True)
    fresh = _make_conn()
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", dead)

    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=fresh))
        async with engine.acquire() as conn:
            assert conn is fresh

    assert engine._conn is fresh


@pytest.mark.asyncio
async def test_pglite_acquire_restarts_manager_when_node_died(tmp_path: Path) -> None:
    """Restart the PGlite Node manager when it crashed under a live engine.

    The asyncpg pool socket and the PGlite Node child are two separate
    resources: when Node crashes, ``manager.is_running()`` flips to False
    while ``self._conn.is_closed()`` may still report False until the next
    read. ``_live_conn`` must notice the dead manager and rebuild the
    substrate -- otherwise ``_open_conn`` raises
    ``RuntimeError: PGlite server is not running`` from
    ``manager.get_asyncpg_uri()`` and every API call 500s until the
    process restarts.
    """
    engine = PGliteEngine(workdir=tmp_path / "pg", extensions=())
    dead_manager = MagicMock()
    dead_manager.is_running.return_value = False
    dead_manager.get_asyncpg_uri.side_effect = RuntimeError(
        "PGlite server is not running. Call start() first."
    )
    engine._manager = dead_manager
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", _make_conn())
    fresh_manager = MagicMock()
    fresh_manager.is_running.return_value = True
    fresh_manager.get_asyncpg_uri.return_value = "postgresql://x"
    fresh_conn = _make_conn()

    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
        monkeypatch.setattr(
            "trackinizer.lib.postgres.substrate.PGliteManager",
            MagicMock(return_value=fresh_manager),
        )
        monkeypatch.setattr(
            "trackinizer.lib.postgres.substrate._drain_node_stdout", MagicMock()
        )
        monkeypatch.setattr(
            "trackinizer.lib.postgres.substrate._ensure_shared_node_modules",
            MagicMock(),
        )
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=fresh_conn))
        async with engine.acquire() as conn:
            assert conn is fresh_conn

    assert engine._manager is fresh_manager
    assert engine._conn is fresh_conn
    dead_manager.stop.assert_called()


@pytest.mark.asyncio
async def test_pglite_acquire_releases_lock_on_reconnect_failure(
    tmp_path: Path,
) -> None:
    """Release the PGlite acquire lock when reconnecting raises."""
    engine = PGliteEngine(workdir=tmp_path / "pg", extensions=())
    manager = MagicMock()
    manager.get_asyncpg_uri.return_value = "postgresql://x"
    engine._manager = manager

    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(
            asyncpg,
            "connect",
            AsyncMock(side_effect=asyncpg.InterfaceError("manager dead")),
        )
        with pytest.raises(asyncpg.InterfaceError):
            async with engine.acquire():
                pass

    assert not engine._lock.locked()


@pytest.mark.asyncio
async def test_pglite_reentrant_acquire_raises_not_deadlocks(tmp_path: Path) -> None:
    """INF-006: a nested same-task ``acquire`` must raise, not deadlock.

    The single-connection guard uses a non-reentrant ``asyncio.Lock``; a
    second ``acquire`` from the same task would block forever waiting on a
    lock the task itself holds. The guard must detect this and raise.
    """
    engine = PGliteEngine(workdir=tmp_path / "pg", extensions=())
    manager = MagicMock()
    manager.get_asyncpg_uri.return_value = "postgresql://x"
    engine._manager = manager
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", _make_conn())

    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=_make_conn()))
        async with engine.acquire():
            with pytest.raises(RuntimeError, match=r"[Rr]eentrant"):
                async with engine.acquire():
                    pass

    assert not engine._lock.locked()


def test_postgres_engine_listens_on_configured_channel() -> None:
    """PostgresEngine keeps the native LISTEN channel domain-configurable."""
    engine = PostgresEngine("postgresql:///unused", listen_channel="demo_channel")

    assert engine.listen_channel == "demo_channel"


@pytest.mark.asyncio
async def test_postgres_engine_aenter_closes_pool_when_listener_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INF-005: ``__aenter__`` must close pool+listener if setup raises.

    ``__aexit__`` does not run when ``__aenter__`` raises (PEP 343), so the
    pool and listener connection leak unless ``__aenter__`` cleans up itself.
    """
    pool = AsyncMock()
    listener = AsyncMock()
    listener.add_listener = AsyncMock(side_effect=asyncpg.InterfaceError("boom"))
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=listener))

    engine = PostgresEngine("postgresql:///unused", listen_channel="ch")
    with pytest.raises(asyncpg.InterfaceError):
        await engine.__aenter__()

    pool.close.assert_awaited()
    listener.close.assert_awaited()


def test_shared_node_modules_installs_once_then_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared PGlite install runs ``npm ci`` once, then is reused.

    Per-test ``npm install`` into a fresh ``tmp_path`` was the cause of the
    60s integration-test timeout; the shared cache must collapse it to one.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    calls = MagicMock()
    monkeypatch.setattr("trackinizer.lib.postgres.substrate.subprocess.run", calls)

    first = substrate._ensure_shared_node_modules()
    second = substrate._ensure_shared_node_modules()

    assert first == second
    assert (first.parent / ".ready").exists()
    calls.assert_called_once()
    assert calls.call_args.args[0][:2] == ["npm", "ci"]


def test_warm_cache_preserves_superseded_keys_that_may_still_be_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ready cache must not delete a tree another live process may use.

    A dependency bump mints a new cache key while already-running PGlite Node
    children can still resolve modules through the old tree. Cache cleanup has
    no lease information, so automatic pruning would race those readers.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(
        "trackinizer.lib.postgres.substrate.subprocess.run", MagicMock()
    )
    current = substrate._ensure_shared_node_modules()  # warms current key
    stale = current.parent.parent / "deadbeef00000000"
    stale.mkdir()
    (stale / ".ready").touch()

    substrate._ensure_shared_node_modules()  # warm return path

    assert stale.exists()
    assert current.parent.exists()


def test_cache_key_tracks_vendored_lockfile_content() -> None:
    """The cache key is derived from the vendored manifest + lockfile bytes."""
    key = substrate._cache_key()

    assert len(key) == 16
    assert substrate._PGLITE_PACKAGE_JSON.exists()
    assert substrate._PGLITE_PACKAGE_LOCK.exists()


def _boot_slot_is_available(timeout_sec: float) -> bool:
    """Return whether a boot slot could be claimed within ``timeout_sec``.

    Collapses the claim into a plain boolean so a test can assert the pool is
    full without nesting one context manager inside another.
    """
    try:
        with substrate.acquire_boot_slot(timeout_sec=timeout_sec):
            return True
    except substrate.BootSlotUnavailableError:
        return False


def _install_lock_is_available(lock_path: Path, timeout_sec: float) -> bool:
    """Return whether the install lock could be claimed within ``timeout_sec``."""
    try:
        with substrate._install_lock(lock_path, timeout_sec=timeout_sec):
            return True
    except substrate.InstallLockUnavailableError:
        return False


def _spawn_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    """Start a child that takes ``lock_path`` and blocks until killed.

    Returns once the child reports the lock is held, so the caller never races
    a not-yet-acquired lock.

    Args:
      lock_path: Lock file for the child to hold.

    Returns:
      holder: The live child process; the caller must kill and reap it.

    """
    holder = subprocess.Popen(  # noqa: S603 -- fixed argv, this interpreter, no shell
        [
            sys.executable,
            "-c",
            (
                "import sys,time;"
                "from filelock import FileLock;"
                "lock=FileLock(sys.argv[1]);"
                "lock.acquire();"
                "print('held',flush=True);"
                "time.sleep(300)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    holder.stdout.readline()
    return holder


def test_install_lock_freed_immediately_when_holder_is_killed(
    tmp_path: Path,
) -> None:
    """A killed installer's lock is claimable at once, with no stale window.

    The install lock had the same timestamp-inference defect as the boot slots,
    with a 900s window: a ``kill -9`` mid-install blocked every other process
    for fifteen minutes. Kernel-released locks make the window unnecessary.
    """
    lock_path = tmp_path / ".lock"
    holder = _spawn_lock_holder(lock_path)
    try:
        assert not _install_lock_is_available(lock_path, 0.2)
    finally:
        holder.kill()
        holder.wait()

    started = time.monotonic()
    with substrate._install_lock(lock_path, timeout_sec=5.0):
        pass
    assert time.monotonic() - started < 1.0


def test_install_lock_held_when_fresh(tmp_path: Path) -> None:
    """A live holder's lock is not stolen by a concurrent installer."""
    lock_path = tmp_path / ".lock"

    with substrate._install_lock(lock_path, timeout_sec=5.0):
        assert not _install_lock_is_available(lock_path, 0.2)


def test_boot_semaphore_caps_concurrent_holders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold-start gate admits at most ``_max_concurrent_boots`` at once.

    Claim the whole pool, then assert the next acquire is refused until a held
    slot is released -- the property that throttles simultaneous PGlite boots so
    their single-threaded Node children do not starve each other.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 2)

    with substrate.acquire_boot_slot(timeout_sec=5.0) as first:
        with substrate.acquire_boot_slot(timeout_sec=5.0) as second:
            assert first != second  # two distinct slots
            assert not _boot_slot_is_available(0.2)

        # One slot released: a waiter now gets it back.
        with substrate.acquire_boot_slot(timeout_sec=5.0) as reused:
            assert reused == second


def test_boot_slot_freed_immediately_when_holder_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGKILL'd holder's slot is free at once -- no stale window, no wait.

    Liveness is the kernel's answer, not a timestamp heuristic: the holder's
    file descriptor closes on process death and the lock drops with it. The
    previous scheme inferred death from an mtime, so it had to pick a window
    that was simultaneously too short for a slow boot (live slots were stolen)
    and too long for a killed one (a dead holder wedged every waiter for the
    full window). This test pins the property that removes both failure modes.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)

    holder = _spawn_lock_holder(substrate._boot_slot_lock_path(0))
    try:
        assert not _boot_slot_is_available(0.2)
    finally:
        holder.kill()
        holder.wait()

    started = time.monotonic()
    with substrate.acquire_boot_slot(timeout_sec=5.0):
        elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"killed holder's slot took {elapsed:.2f}s to free"


def test_boot_slot_acquire_raises_rather_than_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full pool raises after the deadline instead of blocking forever.

    The previous acquire was ``while True`` with no deadline and no failure
    path, so one leaked slot hung every future caller indefinitely -- surfacing
    only as unrelated tests timing out. A bounded wait turns that into an
    immediate, attributable error.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)

    started = time.monotonic()
    with substrate.acquire_boot_slot(timeout_sec=5.0):
        assert not _boot_slot_is_available(0.2)
    assert time.monotonic() - started < 3.0


def test_boot_semaphore_admits_concurrent_holders_in_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap binds within a single process, not just across processes.

    Every engine in a ``pytest -n`` worker boots from ONE process, so a
    per-process lock (POSIX ``fcntl.lockf``) would admit all of them and the
    semaphore would gate nothing. Each claim owns a distinct lock handle, so
    the cap holds regardless of how many claims share a process.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 2)

    with substrate.acquire_boot_slot(timeout_sec=5.0) as first:
        with substrate.acquire_boot_slot(timeout_sec=5.0) as second:
            assert first != second  # distinct slots, same process
            assert not _boot_slot_is_available(0.2)
        # Releasing one admits the next waiter.
        with substrate.acquire_boot_slot(timeout_sec=5.0) as reused:
            assert reused == second


@pytest.mark.asyncio
async def test_node_modules_warmed_before_boot_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-time npm install runs OUTSIDE the held boot slot.

    ``_ensure_shared_node_modules`` can run for minutes on a cold cache. Holding
    a cold-start slot across it would idle one of the very few slots on work
    that is not a boot, throttling every sibling for the whole install. Pin the
    ordering: the install completes before any slot is acquired.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    order: list[str] = []
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(side_effect=lambda: order.append("install")),
    )
    original_acquire = substrate.acquire_boot_slot

    @contextmanager
    def _recording_acquire(**kwargs: float) -> Generator[int]:
        order.append("slot")
        with original_acquire(**kwargs) as index:
            yield index

    monkeypatch.setattr(substrate, "acquire_boot_slot", _recording_acquire)

    engine = substrate.PGliteEngine(workdir=tmp_path / "wd", extensions=())

    async def fake_start_once(_workdir: Path) -> None:
        order.append("start")

    monkeypatch.setattr(engine, "_start_once", fake_start_once)

    await engine._start_with_retries()

    assert order[0] == "install"
    assert order.index("install") < order.index("slot")
    assert order.index("slot") < order.index("start")


@pytest.mark.asyncio
async def test_start_retries_past_transient_boot_runtimeerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient boot ``RuntimeError`` is retried, not surfaced as a hard fail.

    py-pglite reports a dead Node boot as a plain ``RuntimeError`` carrying the
    Node stderr (e.g. a WASM trap under concurrent load), NOT an ``OSError`` --
    so an ``except (OSError, PostgresError)`` clause would miss it. The boot
    retry must include ``RuntimeError`` so a transient trap self-heals on the
    next attempt. First attempt raises, second succeeds.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    engine = substrate.PGliteEngine(workdir=tmp_path / "wd", extensions=())
    monkeypatch.setattr(engine, "_teardown_failed_start", AsyncMock())

    attempts = 0

    async def flaky_start_once(_workdir: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("PGlite process died during startup. Output: WASM trap")

    monkeypatch.setattr(engine, "_start_once", flaky_start_once)

    await engine._start_with_retries()

    assert attempts == 2, "should retry once past the transient boot fault"


@pytest.mark.asyncio
async def test_start_surfaces_deterministic_boot_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot failure that recurs on every attempt surfaces after the retries.

    A deterministic fault (bad extension, unwritable workdir) raises on each of
    the 5 attempts; the loop exhausts and re-raises rather than hanging or
    swallowing the error.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    engine = substrate.PGliteEngine(workdir=tmp_path / "wd", extensions=())
    monkeypatch.setattr(engine, "_teardown_failed_start", AsyncMock())

    attempts = 0

    async def always_fail(_workdir: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("PGlite process died during startup. Output: bad ext")

    monkeypatch.setattr(engine, "_start_once", always_fail)

    with pytest.raises(RuntimeError, match="failed to start after 5 attempts"):
        await engine._start_with_retries()
    assert attempts == 5, "exhausts all attempts before surfacing"


@pytest.mark.asyncio
async def test_own_workdir_removed_on_exit(tmp_path: Path) -> None:
    """An ``own_workdir`` engine deletes its whole workdir on ``__aexit__``.

    Ephemeral servers get a unique, engine-owned scratch dir; cleanup must run
    in the engine teardown (under uvicorn's SIGTERM shutdown) because ``atexit``
    does not fire on a signal exit.
    """
    workdir = tmp_path / "scratch"
    workdir.mkdir()
    (workdir / "marker").touch()
    engine = PGliteEngine(workdir=workdir, extensions=(), own_workdir=True)

    await engine.__aexit__()

    assert not workdir.exists()


@pytest.mark.asyncio
async def test_unowned_workdir_kept_on_exit(tmp_path: Path) -> None:
    """A non-owning engine leaves its workdir intact (caller manages it)."""
    workdir = tmp_path / "scratch"
    workdir.mkdir()
    engine = PGliteEngine(workdir=workdir, extensions=())

    await engine.__aexit__()

    assert workdir.exists()


@pytest.mark.asyncio
async def test_pglite_exit_terminates_client_socket(tmp_path: Path) -> None:
    """PGlite shutdown does not wait on asyncpg's broken close handshake."""
    engine = PGliteEngine(workdir=tmp_path / "scratch", extensions=())
    conn = MagicMock()
    conn.close = AsyncMock()
    conn.terminate = MagicMock()
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", conn)
    manager = MagicMock()
    engine._manager = manager

    await engine.__aexit__()

    conn.terminate.assert_called_once_with()
    conn.close.assert_not_awaited()
    manager.stop.assert_called_once_with()


@pytest.mark.asyncio
async def test_link_shared_node_modules_symlinks_to_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh workdir symlinks ``node_modules`` at the shared install."""
    shared = tmp_path / "shared-node-modules"
    shared.mkdir()
    monkeypatch.setattr(
        substrate, "_ensure_shared_node_modules", MagicMock(return_value=shared)
    )
    workdir = tmp_path / "wd"
    workdir.mkdir()

    substrate._link_shared_node_modules(workdir)

    link = workdir / "node_modules"
    assert link.is_symlink()
    assert link.resolve() == shared.resolve()


def test_link_shared_node_modules_leaves_existing_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing ``node_modules`` is never replaced by the shared link."""
    ensure = MagicMock()
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", ensure)
    workdir = tmp_path / "wd"
    (workdir / "node_modules").mkdir(parents=True)

    substrate._link_shared_node_modules(workdir)

    assert not (workdir / "node_modules").is_symlink()
    ensure.assert_not_called()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)


async def _anext[T](items: AsyncGenerator[T, None]) -> T:
    """Return the next item from an async generator."""
    return await anext(items)


def _make_conn() -> AsyncMock:
    """Build an asyncpg-shaped connection mock."""
    conn = AsyncMock()
    conn.set_type_codec = AsyncMock()
    conn.is_closed = MagicMock(return_value=False)
    return conn
