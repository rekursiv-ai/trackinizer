"""Tests for the shared Postgres substrate."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import asyncio
import hashlib
import os
import threading
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


def test_install_lock_reclaimed_when_stale(tmp_path: Path) -> None:
    """A lock orphaned by a killed installer is reclaimed once it ages out."""
    lock = tmp_path / ".lock"
    lock.mkdir()
    stale = time.time() - substrate._INSTALL_LOCK_STALE_SECONDS - 60
    os.utime(lock, (stale, stale))

    assert substrate._try_acquire_install_lock(lock) is False  # reclaim pass
    assert substrate._try_acquire_install_lock(lock) is True  # now claimable


def test_install_lock_held_when_fresh(tmp_path: Path) -> None:
    """A freshly held lock is not reclaimed out from under a live installer."""
    lock = tmp_path / ".lock"
    lock.mkdir()

    assert substrate._try_acquire_install_lock(lock) is False
    assert lock.exists()


def test_boot_semaphore_caps_concurrent_holders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold-start gate admits at most ``_max_concurrent_boots`` at once.

    Claim the whole pool, then assert the next acquire blocks (polls) until a
    held slot is released -- the property that throttles simultaneous PGlite
    boots so their single-threaded Node children do not starve each other.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 2)
    # Signal the moment the waiter enters the poll loop (calls _real_sleep), so
    # the test proves "still blocked" without a fixed 0.5s wall-clock wait.
    polled = threading.Event()

    def _mark_polled(seconds: float) -> None:
        del seconds
        polled.set()

    monkeypatch.setattr(substrate, "_real_sleep", _mark_polled)

    held = [substrate._acquire_boot_slot(), substrate._acquire_boot_slot()]
    assert len({s.name for s in held}) == 2  # two distinct slots

    # Pool exhausted: a third acquire must poll (sleep) rather than return. Run
    # it in a thread so the test does not wedge on the (now patched) busy-wait.
    got: list[substrate._BootSlot] = []
    waiter = threading.Thread(target=lambda: got.append(substrate._acquire_boot_slot()))
    waiter.start()
    assert polled.wait(timeout=2.0)  # waiter reached the poll loop (blocked)
    assert waiter.is_alive()  # still blocked on a full pool

    substrate._release_boot_slot(held[0])  # free one slot
    waiter.join(timeout=2.0)
    assert not waiter.is_alive()  # unblocked
    assert got  # the waiter returned a slot
    assert got[0].name == held[0].name  # reused the freed slot


def test_boot_slot_reclaimed_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slot orphaned by a killed booter is reclaimed once it ages out.

    Without reclaim a single ``kill -9`` mid-boot would permanently shrink the
    semaphore, eventually wedging every future engine start.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)

    first = substrate._acquire_boot_slot()
    stale = time.time() - substrate._BOOT_SLOT_STALE_SECONDS - 60
    os.utime(first.path, (stale, stale))

    # The only slot is held but stale: the next acquire reclaims and re-claims it
    # rather than blocking forever.
    second = substrate._acquire_boot_slot()
    assert second.name == first.name


def test_release_does_not_delete_a_reclaimed_slots_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow holder's release must not delete the slot a reclaimer now owns.

    Slots are reclaimed by name when stale; without an ownership token, the
    original holder's later ``_release`` would ``rmdir`` the live slot the new
    holder mkdir'd under the same name, over-admitting the semaphore.
    """
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)

    first = substrate._acquire_boot_slot()
    stale = time.time() - substrate._BOOT_SLOT_STALE_SECONDS - 60
    os.utime(first.path, (stale, stale))

    second = substrate._acquire_boot_slot()  # reclaims + re-owns slot-0
    assert second.name == first.name
    assert second.token != first.token

    substrate._release_boot_slot(first)  # the slow original holder releases

    assert second.path.exists(), "stale holder deleted the reclaimer's live slot"
    assert substrate._boot_owner_path(second).exists()


def test_release_does_not_delete_recreated_slot_after_owner_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release/reclaim race must not delete a new owner of the same slot name."""
    monkeypatch.setattr(substrate, "cache_dir", _cache_dir_under(tmp_path))

    first = substrate._BootSlot(path=tmp_path / "slot-0", token="first")  # noqa: S106 -- test token, not a secret
    second = substrate._BootSlot(path=tmp_path / "slot-0", token="second")  # noqa: S106 -- test token, not a secret
    first.path.mkdir()
    substrate._boot_owner_path(first).touch()
    original_unlink = Path.unlink

    def reclaim_during_unlink(path: Path) -> None:
        original_unlink(path)
        first.path.rmdir()
        first.path.mkdir()
        substrate._boot_owner_path(second).touch()

    monkeypatch.setattr(Path, "unlink", reclaim_during_unlink)

    substrate._release_boot_slot(first)

    assert second.path.exists(), "release deleted a concurrently reclaimed slot"
    assert substrate._boot_owner_path(second).exists()


@pytest.mark.asyncio
async def test_node_modules_warmed_before_boot_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-time npm install runs OUTSIDE the held boot slot.

    The install (``_ensure_shared_node_modules``) can take far longer than
    ``_BOOT_SLOT_STALE_SECONDS``; if it ran while a slot was held, a sibling
    would reclaim the live holder and the semaphore would over-admit. Pin the
    ordering: ``_ensure_shared_node_modules`` must be called before any slot is
    acquired.
    """
    order: list[str] = []
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(side_effect=lambda: order.append("install")),
    )
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- test token, not a secret
    monkeypatch.setattr(
        substrate,
        "_acquire_boot_slot",
        MagicMock(side_effect=lambda: (order.append("slot"), slot)[1]),
    )
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())

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
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- test token
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())
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
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- test token
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())
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
