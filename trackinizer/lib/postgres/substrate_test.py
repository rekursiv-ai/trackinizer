"""Tests for the shared Postgres substrate."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast, override
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import asyncio
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time

from py_pglite import PGliteConfig, PGliteManager

import asyncpg
import pytest

from trackinizer.lib.postgres import (
    PGLITE_DATA_DIRNAME,
    PGliteEngine,
    PostgresEngine,
    substrate,
)
from trackinizer.lib.userdirs import cache_dir


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import CodeType


_REENTRANT: Final = (
    r"^Reentrant acquire on the single PGlite connection would deadlock: "
    r"the current task already holds the connection lock\.$"
)

_FAILED_START: Final = r"^PGlite failed to start after 5 attempts$"


def _cache_under(root: Path) -> Path:
    """Return a PGlite cache root under ``root``, laid out as the default one is."""
    return root / "caches" / "rekursiv-ai" / "pglite"


def _no_sleep(seconds: float) -> None:
    """Skip a production poll delay while preserving the retry boundary."""
    del seconds


def test_pglite_default_caches_use_xdg_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "test-host")

    host_key = hashlib.sha256(b"test-host").hexdigest()[:16]
    assert (
        PGliteEngine(workdir=tmp_path)._cache == cache_dir() / "rekursiv-ai" / "pglite"
    )
    assert substrate._boot_slots_root(tmp_path) == tmp_path / "boot-slots" / host_key


def test_an_engine_keeps_the_cache_it_was_made_with(tmp_path: Path) -> None:
    assert PGliteEngine(workdir=tmp_path, cache=tmp_path / "kept")._cache == (
        tmp_path / "kept"
    )


@pytest.mark.db_pglite
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


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_pglite_defaults_to_a_live_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = AsyncMock(wraps=asyncpg.connect)
    monkeypatch.setattr(asyncpg, "connect", connect)

    async with (
        PGliteEngine(workdir=tmp_path / "pg", extensions=(), persist=False) as engine,
        engine.acquire() as conn,
    ):
        assert await conn.fetchval("SELECT 7") == 7
        assert connect.await_args is not None
        dsn = urlsplit(cast(str, connect.await_args.kwargs["dsn"]))
        assert dsn.hostname is None
        assert Path(parse_qs(dsn.query)["host"][0]).is_absolute()


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_pglite_creates_missing_workdir_ancestors(tmp_path: Path) -> None:
    workdir = tmp_path / "missing-parent" / "pg"
    assert not workdir.parent.exists()

    async with (
        PGliteEngine(workdir=workdir, extensions=(), persist=False) as engine,
        engine.acquire() as conn,
    ):
        assert await conn.fetchval("SELECT 1") == 1
        assert workdir.is_dir()


@pytest.mark.asyncio
async def test_pglite_listen_notify_round_trips_payload(tmp_path: Path) -> None:
    """PGlite notifications need no database or Node process."""
    engine = PGliteEngine(workdir=tmp_path / "unused", extensions=())
    channel = "events"
    notifications = engine.listen(channel)
    next_payload = asyncio.create_task(_anext(notifications))
    try:
        await asyncio.sleep(0)
        await engine.notify(channel, "payload-1")
        assert await next_payload == "payload-1"
    finally:
        if not next_payload.done():
            next_payload.cancel()
            with suppress(asyncio.CancelledError):
                await next_payload
        await notifications.aclose()
    assert not engine._bus._subscribers[channel]


@pytest.fixture
def synthetic_node_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep subprocess call recordings independent of credentials in the caller."""
    monkeypatch.setattr(os, "environ", {"PATH": "/synthetic/bin", "KEEP": "test"})


@pytest.mark.asyncio
async def test_pglite_acquire_reopens_dropped_connection(tmp_path: Path) -> None:
    """Reopen a PGlite connection when Node side-closes the prior connection."""
    engine = PGliteEngine(workdir=tmp_path / "pg", extensions=())
    manager = MagicMock(get_asyncpg_uri=MagicMock(return_value="postgresql://x"))
    engine._manager = cast(PGliteManager, manager)
    dead = _make_conn()
    dead.is_closed = MagicMock(return_value=True)
    fresh = _make_conn()
    # Quoted: `asyncpg.Connection` is generic to the checkers but NOT
    # subscriptable at runtime, so the bare form raises TypeError.
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
    stop = MagicMock()
    dead_manager = MagicMock(
        is_running=MagicMock(return_value=False),
        get_asyncpg_uri=MagicMock(
            side_effect=RuntimeError(
                "PGlite server is not running. Call start() first.",
            ),
        ),
        stop=stop,
    )
    engine._manager = cast(PGliteManager, dead_manager)
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", _make_conn())
    fresh_manager = MagicMock(
        is_running=MagicMock(return_value=True),
        get_asyncpg_uri=MagicMock(return_value="postgresql://x"),
    )
    fresh_conn = _make_conn()

    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(
            "trackinizer.lib.postgres.substrate.PGliteManager",
            MagicMock(return_value=fresh_manager),
        )
        monkeypatch.setattr(
            "trackinizer.lib.postgres.substrate._start_node",
            MagicMock(),
        )
        monkeypatch.setattr(substrate, "_stop_node", stop)
        monkeypatch.setattr(
            "trackinizer.lib.postgres.substrate._ensure_shared_node_modules",
            MagicMock(),
        )
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=fresh_conn))
        async with engine.acquire() as conn:
            assert conn is fresh_conn

    assert engine._manager is fresh_manager
    assert engine._conn is fresh_conn
    stop.assert_called_once_with(dead_manager)


@pytest.mark.asyncio
async def test_pglite_acquire_releases_lock_on_reconnect_failure(
    tmp_path: Path,
) -> None:
    """Release the PGlite acquire lock when reconnecting raises."""
    engine = PGliteEngine(workdir=tmp_path / "pg", extensions=())
    manager = MagicMock(get_asyncpg_uri=MagicMock(return_value="postgresql://x"))
    engine._manager = cast(PGliteManager, manager)

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
    manager = MagicMock(get_asyncpg_uri=MagicMock(return_value="postgresql://x"))
    engine._manager = cast(PGliteManager, manager)
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", _make_conn())

    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=_make_conn()))
        async with engine.acquire():
            with pytest.raises(RuntimeError, match=_REENTRANT):
                async with engine.acquire():
                    pass

    assert not engine._lock.locked()


@pytest.mark.asyncio
async def test_a_second_task_waits_for_the_borrowed_pglite_connection(
    tmp_path: Path,
) -> None:
    engine = PGliteEngine(workdir=tmp_path, extensions=())
    engine._manager = cast(
        PGliteManager,
        MagicMock(is_running=MagicMock(return_value=True)),
    )
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", _make_conn())
    attempted = asyncio.Event()

    async def borrow() -> object:
        attempted.set()
        async with engine.acquire() as connection:
            return connection

    async with asyncio.TaskGroup() as tasks, engine.acquire() as first:
        borrower = tasks.create_task(borrow())
        await attempted.wait()
        assert not borrower.done()

    assert borrower.result() is first


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
    pool_close = AsyncMock()
    pool = AsyncMock(close=pool_close)
    listener_close = AsyncMock()
    listener = AsyncMock(
        add_listener=AsyncMock(side_effect=asyncpg.InterfaceError("boom")),
        close=listener_close,
    )
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=listener))

    engine = PostgresEngine("postgresql:///unused", listen_channel="ch")
    with pytest.raises(asyncpg.InterfaceError):
        await engine.__aenter__()

    pool_close.assert_awaited()
    listener_close.assert_awaited()


def test_shared_node_modules_installs_once_then_reuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared PGlite install runs ``npm ci`` once, then is reused.

    Per-test ``npm install`` into a fresh ``tmp_path`` was the cause of the
    60s integration-test timeout; the shared cache must collapse it to one.
    """
    cache = _cache_under(tmp_path)
    calls = MagicMock()
    monkeypatch.setattr("trackinizer.lib.postgres.substrate.subprocess.run", calls)

    first = substrate._ensure_shared_node_modules(cache)
    second = substrate._ensure_shared_node_modules(cache)

    assert first == second
    assert (first.parent / ".ready").exists()
    calls.assert_called_once()
    assert cast(list[str], calls.call_args.args[0])[:2] == ["npm", "ci"]


def test_warm_cache_preserves_superseded_keys_that_may_still_be_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready cache must not delete a tree another live process may use.

    A dependency bump mints a new cache key while already-running PGlite Node
    children can still resolve modules through the old tree. Cache cleanup has
    no lease information, so automatic pruning would race those readers.
    """
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(subprocess, "run", MagicMock())
    current = substrate._ensure_shared_node_modules(cache)  # Warms current key.
    stale = current.parent.parent / "deadbeef00000000"
    stale.mkdir()
    (stale / ".ready").touch()

    substrate._ensure_shared_node_modules(cache)  # Warm return path.

    assert stale.exists()
    assert current.parent.exists()


def test_pglite_version_tracks_vendored_lockfile_content() -> None:
    """The cache key is derived from the vendored manifest + lockfile bytes."""
    key = substrate.pglite_version()

    assert len(key) == 16
    assert (substrate._CWD / "pglite-package.json").exists()
    assert (substrate._CWD / "pglite-package-lock.json").exists()


def test_install_lock_stale_window_exceeds_install_timeout() -> None:
    assert substrate._INSTALL_LOCK_STALE_SECONDS > substrate._INSTALL_TIMEOUT_SECONDS


class _InvertedSubstrate(SourceFileLoader):
    """Load a copy of substrate.py compiled under the real file's name.

    The name lets coverage credit the guard line. The loader's own path is the
    tmp copy, so the inverted bytecode is never cached in substrate's
    ``__pycache__``, where it broke every later import of the package.
    """

    @override
    def get_code(self, fullname: str) -> CodeType:
        del fullname
        return compile(Path(self.path).read_bytes(), substrate.__file__, "exec")


def test_install_lock_invariant_rejects_inverted_constants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = "_INSTALL_LOCK_STALE_SECONDS: Final = 900.0"
    source = Path(substrate.__file__).read_text()
    assert stale in source
    copy = tmp_path / "substrate_inverted.py"
    copy.write_text(source.replace(stale, "_INSTALL_LOCK_STALE_SECONDS: Final = 300.0"))
    loader = _InvertedSubstrate("substrate_inverted", str(copy))
    spec = util.spec_from_loader("substrate_inverted", loader)
    assert spec is not None
    module = util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)

    with pytest.raises(
        RuntimeError,
        match=r"^PGlite install lock stale window must exceed the install timeout$",
    ):
        loader.exec_module(module)


def test_install_lock_reclaimed_when_stale(tmp_path: Path) -> None:
    """A lock orphaned by a killed installer is reclaimed once it ages out."""
    lock = tmp_path / ".lock"
    lock.mkdir()
    stale = time.time() - substrate._INSTALL_LOCK_STALE_SECONDS - 60
    os.utime(lock, (stale, stale))

    assert substrate._try_acquire_install_lock(lock) is False  # Reclaim pass.
    assert substrate._try_acquire_install_lock(lock) is True  # Now claimable.


def test_install_lock_held_when_fresh(tmp_path: Path) -> None:
    """A freshly held lock is not reclaimed out from under a live installer."""
    lock = tmp_path / ".lock"
    lock.mkdir()

    assert substrate._try_acquire_install_lock(lock) is False
    assert lock.exists()


def test_boot_semaphore_caps_concurrent_holders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold-start gate admits at most ``_max_concurrent_boots`` at once.

    Claim the whole pool, then assert the next acquire blocks (polls) until a
    held slot is released -- the property that throttles simultaneous PGlite
    boots so their single-threaded Node children do not starve each other.
    """
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 2)
    # Signal the moment the waiter enters the poll loop (calls _real_sleep), so
    # the test proves "still blocked" without a fixed 0.5s wall-clock wait.
    polled = threading.Event()

    def _mark_polled(seconds: float) -> None:
        del seconds
        polled.set()

    monkeypatch.setattr(substrate, "_real_sleep", _mark_polled)

    held = [substrate._acquire_boot_slot(cache), substrate._acquire_boot_slot(cache)]
    assert len({s.name for s in held}) == 2  # Two distinct slots.

    # Pool exhausted: a third acquire must poll (sleep) rather than return. Run
    # it in a thread so the test does not wedge on the (now patched) busy-wait.
    got: list[substrate._BootSlot] = []
    waiter = threading.Thread(
        target=lambda: got.append(substrate._acquire_boot_slot(cache)),
    )
    waiter.start()
    assert polled.wait(timeout=2.0)  # `waiter` reached the poll loop (blocked)
    assert waiter.is_alive()  # Still blocked on a full pool.

    substrate._release_boot_slot(held[0])  # Free one slot.
    waiter.join(timeout=2.0)
    assert not waiter.is_alive()  # Unblocked.
    assert got  # The waiter returned a slot.
    assert got[0].name == held[0].name  # Reused the freed slot.


def test_boot_slot_reclaimed_when_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot orphaned by a killed booter is reclaimed once it ages out.

    Without reclaim a single ``kill -9`` mid-boot would permanently shrink the
    semaphore, eventually wedging every future engine start.
    """
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)
    monkeypatch.setattr(substrate, "_real_sleep", _no_sleep)

    first = substrate._acquire_boot_slot(cache)
    stale = time.time() - substrate._BOOT_SLOT_STALE_SECONDS - 60
    os.utime(first.path, (stale, stale))

    # The only slot is held but stale: the next acquire reclaims and re-claims it
    # rather than blocking forever.
    second = substrate._acquire_boot_slot(cache)
    assert second.name == first.name


def test_release_does_not_delete_a_reclaimed_slots_new_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow holder's release must not delete the slot a reclaimer now owns.

    Slots are reclaimed by name when stale; without an ownership token, the
    original holder's later ``_release`` would ``rmdir`` the live slot the new
    holder mkdir'd under the same name, over-admitting the semaphore.
    """
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)
    monkeypatch.setattr(substrate, "_real_sleep", _no_sleep)

    first = substrate._acquire_boot_slot(cache)
    stale = time.time() - substrate._BOOT_SLOT_STALE_SECONDS - 60
    os.utime(first.path, (stale, stale))

    second = substrate._acquire_boot_slot(cache)  # Reclaims + re-owns slot-0.
    assert second.name == first.name
    assert second.token != first.token

    substrate._release_boot_slot(first)  # The slow original holder releases.

    assert second.path.exists(), "stale holder deleted the reclaimer's live slot"
    assert substrate._boot_owner_path(second).exists()


def test_release_does_not_delete_recreated_slot_after_owner_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release/reclaim race must not delete a new owner of the same slot name."""
    first = substrate._BootSlot(path=tmp_path / "slot-0", token="first")  # noqa: S106 -- Test credentials are fixed fixtures and never real secrets.
    second = substrate._BootSlot(path=tmp_path / "slot-0", token="second")  # noqa: S106 -- Test credentials are fixed fixtures and never real secrets.
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-time npm install runs OUTSIDE the held boot slot.

    The install (``_ensure_shared_node_modules``) can take far longer than
    ``_BOOT_SLOT_STALE_SECONDS``; if it ran while a slot was held, a sibling
    would reclaim the live holder and the semaphore would over-admit. Pin the
    ordering: ``_ensure_shared_node_modules`` must be called before any slot is
    acquired.
    """
    order: list[str] = []
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- Test credentials are fixed fixtures and never real secrets.

    def install(cache: Path) -> Path:
        order.append("install")
        return cache

    def acquire(cache: Path) -> substrate._BootSlot:
        del cache
        order.append("slot")
        return slot

    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", install)
    monkeypatch.setattr(substrate, "_acquire_boot_slot", acquire)
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())

    engine = substrate.PGliteEngine(workdir=tmp_path / "wd", extensions=())

    async def fake_start_once(workdir: Path) -> None:
        del workdir
        order.append("start")

    monkeypatch.setattr(engine, "_start_once", fake_start_once)

    await engine._start_with_retries()

    assert order[0] == "install"
    assert order.index("install") < order.index("slot")
    assert order.index("slot") < order.index("start")


@pytest.mark.asyncio
async def test_start_retries_past_transient_boot_runtimeerror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient boot ``RuntimeError`` is retried, not surfaced as a hard fail.

    py-pglite reports a dead Node boot as a plain ``RuntimeError`` carrying the
    Node stderr (e.g. a WASM trap under concurrent load), NOT an ``OSError`` --
    so an ``except (OSError, PostgresError)`` clause would miss it. The boot
    retry must include ``RuntimeError`` so a transient trap self-heals on the
    next attempt. First attempt raises, second succeeds.
    """
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- Test credentials are fixed fixtures and never real secrets.
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())
    engine = substrate.PGliteEngine(workdir=tmp_path / "wd", extensions=())
    monkeypatch.setattr(engine, "_teardown_failed_start", AsyncMock())

    attempts = 0

    async def flaky_start_once(workdir: Path) -> None:
        del workdir
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("PGlite process died during startup. Output: WASM trap")

    monkeypatch.setattr(engine, "_start_once", flaky_start_once)

    await engine._start_with_retries()

    assert attempts == 2, "should retry once past the transient boot fault"


@pytest.mark.asyncio
async def test_start_surfaces_deterministic_boot_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boot failure that recurs on every attempt surfaces after the retries.

    A deterministic fault (bad extension, unwritable workdir) raises on each of
    the 5 attempts; the loop exhausts and re-raises rather than hanging or
    swallowing the error.
    """
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- Test credentials are fixed fixtures and never real secrets.
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())
    engine = substrate.PGliteEngine(workdir=tmp_path / "wd", extensions=())
    monkeypatch.setattr(engine, "_teardown_failed_start", AsyncMock())

    attempts = 0

    async def always_fail(workdir: Path) -> None:
        del workdir
        nonlocal attempts
        attempts += 1
        raise RuntimeError("PGlite process died during startup. Output: bad ext")

    monkeypatch.setattr(engine, "_start_once", always_fail)

    with pytest.raises(RuntimeError, match=_FAILED_START):
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
async def test_pglite_exit_terminates_client_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PGlite shutdown does not wait on asyncpg's broken close handshake."""
    engine = PGliteEngine(workdir=tmp_path / "scratch", extensions=())
    close = AsyncMock()
    terminate = MagicMock()
    conn = MagicMock(close=close, terminate=terminate)
    engine._conn = cast("asyncpg.Connection[asyncpg.Record]", conn)
    stop = MagicMock()
    manager = MagicMock()
    engine._manager = cast(PGliteManager, manager)
    socket_path = tmp_path / ".s.PGSQL.5432"
    socket_path.touch()
    engine._socket_path = socket_path
    monkeypatch.setattr(substrate, "_stop_node", stop)

    await engine.__aexit__()

    terminate.assert_called_once_with()
    close.assert_not_awaited()
    stop.assert_called_once_with(manager)
    assert (engine._conn, engine._socket_path) == (None, None)
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_link_shared_node_modules_symlinks_to_shared_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh workdir symlinks ``node_modules`` at the shared install."""
    shared = tmp_path / "shared-node-modules"
    shared.mkdir()
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=shared),
    )
    workdir = tmp_path / "wd"
    workdir.mkdir()

    substrate._link_shared_node_modules(workdir, tmp_path)

    link = workdir / "node_modules"
    assert link.is_symlink()
    assert link.resolve() == shared.resolve()


def test_link_shared_node_modules_leaves_existing_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing ``node_modules`` is never replaced by the shared link."""
    ensure = MagicMock()
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", ensure)
    workdir = tmp_path / "wd"
    (workdir / "node_modules").mkdir(parents=True)

    substrate._link_shared_node_modules(workdir, tmp_path)

    assert not (workdir / "node_modules").is_symlink()
    ensure.assert_not_called()


async def _anext[T](items: AsyncGenerator[T, None]) -> T:
    """Return the next item from an async generator."""
    return await anext(items)


def _make_conn() -> AsyncMock:
    """Build an asyncpg-shaped connection mock."""
    conn = AsyncMock()
    conn.set_type_codec = AsyncMock()
    conn.is_closed = MagicMock(return_value=False)
    return conn


@pytest.mark.asyncio
async def test_pglite_enter_starts_and_returns_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    start = AsyncMock()
    monkeypatch.setattr(engine, "_start_with_retries", start)

    assert await engine.__aenter__() is engine
    start.assert_awaited_once_with()


def test_lock_owner_rejects_invalid_owner() -> None:
    lock = asyncio.Lock()
    setattr(lock, substrate._LOCK_OWNER_ATTR, object())

    with pytest.raises(
        ValueError,
        match=r"^Expected owner is None or isinstance\(owner, asyncio\.Task\)\.$",
    ):
        substrate._lock_owner(lock)


@pytest.mark.asyncio
async def test_pglite_start_once_tcp_configures_port_and_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=tmp_path, extensions=("pgvector",), use_tcp=True)
    start = MagicMock()
    manager = MagicMock(start=start)
    configs: list[PGliteConfig] = []

    def make_manager(config: PGliteConfig) -> MagicMock:
        configs.append(config)
        return manager

    monkeypatch.setattr(substrate, "_pick_free_port", lambda: 54_321)
    monkeypatch.setattr(substrate, "PGliteManager", make_manager)
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    monkeypatch.setattr(substrate, "_start_node", start)
    monkeypatch.setattr(engine, "_open_conn", AsyncMock(return_value=_make_conn()))

    await engine._start_once(tmp_path)

    (config,) = configs
    assert (config.work_dir, config.use_tcp, config.tcp_port) == (
        tmp_path,
        True,
        54_321,
    )
    assert (config.extensions, config.log_level, config.timeout) == (
        ["pgvector"],
        "ERROR",
        10,
    )
    assert engine._manager is manager
    assert engine._socket_path is None
    start.assert_called_once_with(
        manager,
        config=config,
        workdir=tmp_path,
        cache=engine._cache,
    )
    assert (
        'const LISTEN = {"host": "127.0.0.1", "port": 54321};'
        in (tmp_path / "pglite_manager.js").read_text().splitlines()
    )


@pytest.mark.asyncio
async def test_pglite_teardown_failed_start_delegates_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    shutdown = AsyncMock()
    monkeypatch.setattr(engine, "_shutdown", shutdown)

    await engine._teardown_failed_start()

    shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_pglite_acquire_requires_entered_engine() -> None:
    with pytest.raises(ValueError, match=r"^engine not entered$"):
        PGliteEngine(workdir=Path("unused"), extensions=()).acquire()


@pytest.mark.asyncio
async def test_pglite_open_conn_requires_entered_engine() -> None:
    with pytest.raises(ValueError, match=r"^engine not entered$"):
        await PGliteEngine(workdir=Path("unused"), extensions=())._open_conn()


@pytest.mark.asyncio
async def test_pglite_open_conn_retries_and_surfaces_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    manager = MagicMock(get_asyncpg_uri=MagicMock(return_value="postgresql://x"))
    monkeypatch.setattr(engine, "_manager", manager)
    first = asyncpg.ConnectionDoesNotExistError("first")
    second = asyncpg.ConnectionDoesNotExistError("second")
    fresh = _make_conn()
    connect = AsyncMock(side_effect=[first, second, fresh])
    sleeps = AsyncMock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncio, "sleep", sleeps)

    result = await engine._open_conn(attempts=3, backoff_seconds=0.1)

    assert result is fresh
    assert connect.await_count == 3
    assert [call.args[0] for call in sleeps.await_args_list] == [0.1, 0.2]


@pytest.mark.asyncio
async def test_pglite_open_conn_zero_attempts_has_clear_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    monkeypatch.setattr(engine, "_manager", MagicMock())

    with pytest.raises(ValueError, match=r"^Expected last_error is not None\.$"):
        await engine._open_conn(attempts=0)


@pytest.mark.asyncio
async def test_pglite_open_conn_raises_last_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    monkeypatch.setattr(engine, "_manager", MagicMock())
    error = OSError("connection refused")
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=error))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(OSError, match="connection refused"):
        await engine._open_conn(attempts=1)


@pytest.mark.asyncio
async def test_pglite_live_conn_requires_connection_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    manager = MagicMock(is_running=MagicMock(return_value=False))
    monkeypatch.setattr(engine, "_manager", manager)
    monkeypatch.setattr(engine, "_shutdown", AsyncMock())
    monkeypatch.setattr(engine, "_start_with_retries", AsyncMock())

    with pytest.raises(ValueError, match=r"^Expected self\._conn is not None\.$"):
        await engine._live_conn()


@pytest.mark.asyncio
async def test_pglite_notify_and_listen_deliver_payload() -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    stream = engine.listen("channel")
    next_payload = asyncio.create_task(_anext(stream))
    await asyncio.sleep(0)

    await engine.notify("channel", "payload")

    assert await next_payload == "payload"
    await stream.aclose()


@pytest.mark.asyncio
async def test_postgres_enter_configures_pool_listener_and_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_close = AsyncMock()
    pool = AsyncMock(close=pool_close)
    add_listener = AsyncMock()
    listener_close = AsyncMock()
    listener = AsyncMock(add_listener=add_listener, close=listener_close)
    create_pool = AsyncMock(return_value=pool)
    connect = AsyncMock(return_value=listener)
    initialized: list[object] = []

    async def init(conn: object) -> None:
        initialized.append(conn)

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(substrate, "_init_connection", init)
    engine = PostgresEngine("postgresql:///unused", listen_channel="updates")

    assert await engine.__aenter__() is engine
    assert engine._pool is pool
    assert engine._listener is listener
    create_pool.assert_awaited_once_with(
        dsn="postgresql:///unused",
        min_size=1,
        max_size=8,
        init=init,
    )
    connect.assert_awaited_once_with(dsn="postgresql:///unused")
    assert initialized == [listener]
    add_listener.assert_awaited_once_with("updates", engine._on_notify)
    await engine.__aexit__()
    listener_close.assert_awaited_once_with()
    pool_close.assert_awaited_once_with()
    assert engine._listener is None


def test_postgres_on_notify_publishes_string_payload() -> None:
    engine = PostgresEngine("postgresql:///unused", listen_channel="updates")
    connection = asyncpg.Connection.__new__(asyncpg.Connection)  # pyrefly: ignore[bad-specialization] -- pyrefly reads a bare generic class as ``Connection[Unknown]``, outside its own bound.

    stream = engine.listen("updates")

    async def check() -> None:
        next_payload = asyncio.create_task(_anext(stream))
        await asyncio.sleep(0)
        engine._on_notify(connection, 7, "updates", 123)
        assert await next_payload == "123"
        await stream.aclose()

    asyncio.run(check())


def test_postgres_acquire_requires_entered_engine() -> None:
    with pytest.raises(ValueError, match=r"^engine not entered$"):
        PostgresEngine("postgresql:///unused", listen_channel="updates").acquire()


@pytest.mark.asyncio
async def test_postgres_exit_requires_pool() -> None:
    with pytest.raises(ValueError, match=r"^Expected self\._pool is not None\.$"):
        await PostgresEngine(
            "postgresql:///unused",
            listen_channel="updates",
        ).__aexit__()


@pytest.mark.asyncio
async def test_postgres_notify_executes_parameterized_pg_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PostgresEngine("postgresql:///unused", listen_channel="updates")
    execute = AsyncMock()
    conn = AsyncMock(execute=execute)

    class AcquireContext:
        async def __aenter__(self) -> AsyncMock:
            return conn

        async def __aexit__(self, *exc: object) -> None:
            del exc

    class Pool:
        def acquire(self) -> AcquireContext:
            return AcquireContext()

    monkeypatch.setattr(engine, "_pool", Pool())

    await engine.notify("updates", "payload")

    execute.assert_awaited_once_with(
        "SELECT pg_notify($1, $2)",
        "updates",
        "payload",
    )


@pytest.mark.asyncio
async def test_local_bus_drops_oldest_payload_on_overflow() -> None:
    bus = substrate._LocalBus()
    stream = bus.subscribe("events", queue_maxsize=1)
    task = asyncio.create_task(_anext(stream))
    await asyncio.sleep(0)
    bus.publish("events", "old")
    bus.publish("events", "new")

    assert await task == "new"
    await stream.aclose()


def test_local_bus_publishes_to_a_channel_no_one_hears_as_nothing() -> None:
    bus = substrate._LocalBus()

    bus.publish("unheard", "payload")

    assert bus._subscribers == {}


@pytest.mark.asyncio
async def test_local_bus_removes_subscriber_when_closed() -> None:
    bus = substrate._LocalBus()
    stream = bus.subscribe("events")
    task = asyncio.create_task(_anext(stream))
    await asyncio.sleep(0)
    assert bus._subscribers["events"]
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await stream.aclose()

    assert not bus._subscribers["events"]


@pytest.mark.parametrize("read_error", [OSError("closed"), ValueError("closed")])
def test_drain_node_stdout_reports_read_failures_and_drains_to_eof(
    monkeypatch: pytest.MonkeyPatch,
    read_error: OSError | ValueError,
) -> None:
    class Thread:
        def __init__(self) -> None:
            self.started = False

        def start(self) -> None:
            self.started = True

    targets: list[Callable[[], None]] = []
    thread = Thread()

    def make_thread(*, target: Callable[[], None], daemon: bool) -> Thread:
        assert daemon
        targets.append(target)
        return thread

    monkeypatch.setattr(threading, "Thread", make_thread)
    readline = MagicMock(side_effect=read_error)
    stdout = MagicMock(readline=readline)
    failed = substrate._drain_node_stdout(stdout)
    assert thread.started
    targets.pop()()
    readline.assert_called_once_with()
    assert failed.changed.is_set()
    assert not failed.ready
    assert failed.tail == "closed"

    complete_readline = MagicMock(side_effect=["line", ""])
    complete = MagicMock(readline=complete_readline)
    finished = substrate._drain_node_stdout(complete)
    targets.pop()()
    assert complete_readline.call_count == 2
    assert finished.changed.is_set()
    assert not finished.ready
    assert finished.tail == "line"


def test_install_wait_retries_until_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    cache = _cache_under(tmp_path)

    def no_lock(lock: Path) -> bool:
        del lock
        return False

    monkeypatch.setattr(substrate, "_try_acquire_install_lock", no_lock)
    monkeypatch.setattr(substrate, "pglite_version", lambda: "key")
    root = tmp_path / "caches" / "rekursiv-ai" / "pglite" / "node-modules" / "key"
    ready = root / ".ready"

    def make_ready(seconds: float) -> None:
        sleeps.append(seconds)
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.touch()

    monkeypatch.setattr(substrate, "_real_sleep", make_ready)
    assert substrate._ensure_shared_node_modules(cache) == ready.parent / "node_modules"
    assert sleeps == [0.2]


def test_real_sleep_delegates_to_time(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = MagicMock()
    monkeypatch.setattr(time, "sleep", sleep)

    substrate._real_sleep(0.3)

    sleep.assert_called_once_with(0.3)


def test_pick_free_port_is_bindable() -> None:
    port = substrate._pick_free_port()

    assert 0 < port < 65_536


def test_manager_js_extension_parts_validate_and_render() -> None:
    requires, config = substrate._manager_js_extension_parts(("pgvector",))

    assert "require('@electric-sql/pglite-pgvector')" in requires
    assert config == "{ pgvector: vector }"
    assert substrate._manager_js_extension_parts(()) == ("", "{}")
    with pytest.raises(ValueError, match="unknown pglite extension"):
        substrate._manager_js_extension_parts(("missing",))


@pytest.mark.asyncio
@pytest.mark.usefixtures("synthetic_node_environment")
@pytest.mark.parametrize("use_tcp", [False, True])
@pytest.mark.parametrize("persist", [False, True])
async def test_pglite_start_uses_owned_child_readiness_without_vendor_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    use_tcp: bool,
    persist: bool,
) -> None:
    output = io.StringIO("loading wasm\nServer started on socket test\n")
    process = MagicMock(stdout=output, poll=MagicMock(return_value=None))
    spawn = MagicMock(return_value=process)
    vendor_start = MagicMock(side_effect=AssertionError("vendor startup must not run"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(PGliteManager, "start", vendor_start)
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    monkeypatch.setattr(substrate, "_pick_free_port", lambda: 54_321)
    connection = _make_conn()
    handshake = AsyncMock(return_value=connection)
    engine = PGliteEngine(
        workdir=tmp_path,
        extensions=("pgvector",),
        persist=persist,
        use_tcp=use_tcp,
    )
    monkeypatch.setattr(engine, "_open_conn", handshake)
    cwd = Path.cwd()
    (tmp_path / "package.json").write_text('{"type":"module"}\n')

    await engine._start_once(tmp_path)

    assert (tmp_path / "package.json").read_text() == '{"type":"commonjs"}\n'
    vendor_start.assert_not_called()
    assert Path.cwd() == cwd
    assert engine._conn is connection
    handshake.assert_awaited_once_with()
    assert spawn.call_count == 1
    assert spawn.call_args.args == (["node", "pglite_manager.js"],)
    assert spawn.call_args.kwargs["text"] is True
    assert spawn.call_args.kwargs["cwd"] == tmp_path
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert spawn.call_args.kwargs["stdout"] == subprocess.PIPE
    assert spawn.call_args.kwargs["stderr"] == subprocess.STDOUT
    script = (tmp_path / "pglite_manager.js").read_text().splitlines()
    data_dir = f'            dataDir: "{tmp_path / PGLITE_DATA_DIRNAME}",'
    assert script[script.index("            extensions: { pgvector: vector }") - 1] == (
        data_dir if persist else "            "
    )
    assert "const { vector } = require('@electric-sql/pglite-pgvector');" in script
    assert ('const LISTEN = {"host": "127.0.0.1", "port": 54321};' in script) is use_tcp
    output.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("synthetic_node_environment")
@pytest.mark.parametrize(
    ("persist", "made", "seeded"),
    [(False, False, True), (True, False, True), (True, True, False)],
    ids=["in-memory", "new-data-directory", "existing-data-directory"],
)
async def test_only_a_new_database_opens_from_this_hosts_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    persist: bool,
    made: bool,
    seeded: bool,
) -> None:
    """A database PGlite would initialize loads the template for its extensions."""
    process = MagicMock(
        stdout=io.StringIO("Server started on socket test\n"),
        poll=MagicMock(return_value=None),
    )
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=process))
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    monkeypatch.setattr(substrate, "pglite_version", lambda: "v1")
    workdir = tmp_path / "pg"
    if made:
        (workdir / PGLITE_DATA_DIRNAME).mkdir(parents=True)
    workdir.mkdir(exist_ok=True)
    engine = PGliteEngine(
        workdir=workdir,
        extensions=("pgvector",),
        persist=persist,
    )
    monkeypatch.setattr(engine, "_open_conn", AsyncMock(return_value=_make_conn()))

    await engine._start_once(workdir)

    template = cache_dir() / "rekursiv-ai" / "pglite" / "templates" / "v1-pgvector.tar"
    script = (workdir / "pglite_manager.js").read_text().splitlines()
    assert (
        "const SEED = " + (json.dumps(str(template)) if seeded else "null") + ";"
    ) in script


def test_a_template_is_named_for_its_sorted_extensions_or_bare(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    version = substrate.pglite_version()

    assert [
        substrate._template(tmp_path, names) for names in ((), ("zeta", "alpha"))
    ] == [
        templates / f"{version}-bare.tar",
        templates / f"{version}-alpha-zeta.tar",
    ]


# Each initialization draws a new system identifier, so a copy keeps its original's.
_SYSTEM: Final = "SELECT system_identifier FROM pg_control_system()"


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_a_template_is_the_database_as_first_made(tmp_path: Path) -> None:
    """The first boot records its new database; later ones start from it, untouched."""
    async with (
        PGliteEngine(workdir=tmp_path / "first", extensions=(), persist=False) as first,
        first.acquire() as conn,
    ):
        made = await conn.fetchval(_SYSTEM)
        await conn.execute("CREATE TABLE made_after (id int)")
    assert substrate._template(cache_dir() / "rekursiv-ai" / "pglite", ()).is_file()

    durable = tmp_path / "durable"
    async with (
        PGliteEngine(workdir=durable, extensions=()) as loaded,
        loaded.acquire() as conn,
    ):
        loaded_from = await conn.fetchval(_SYSTEM)
        tables = await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE tablename = 'made_after'",
        )
        await conn.execute("CREATE TABLE kept (id int)")
        await conn.execute("INSERT INTO kept VALUES (7)")
    async with (
        PGliteEngine(workdir=durable, extensions=()) as reopened,
        reopened.acquire() as conn,
    ):
        kept = await conn.fetchval("SELECT id FROM kept")

    assert (loaded_from, tables, kept) == (made, 0, 7)


@pytest.mark.asyncio
@pytest.mark.usefixtures("synthetic_node_environment")
@pytest.mark.parametrize(
    ("output_text", "returncode", "message"),
    [
        ("WASM boot failed\n", 13, "WASM boot failed"),
        ("WASM boot failed\n", None, "WASM boot failed"),
        ("Server started on socket test\n", 13, "process died during startup"),
    ],
)
async def test_pglite_start_rejects_eof_or_death_without_live_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_text: str,
    returncode: int | None,
    message: str,
) -> None:
    output = io.StringIO(output_text)
    process = MagicMock(stdout=output, poll=MagicMock(return_value=returncode))
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=process))
    monkeypatch.setattr(
        PGliteManager,
        "start",
        MagicMock(side_effect=AssertionError("vendor startup must not run")),
    )
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    engine = PGliteEngine(workdir=tmp_path, extensions=())
    handshake = AsyncMock()
    monkeypatch.setattr(engine, "_open_conn", handshake)

    with pytest.raises(RuntimeError, match=message):
        await engine._start_once(tmp_path)

    handshake.assert_not_awaited()
    output.close()


class _BlockedNodeOutput:
    def __init__(self) -> None:
        self.release = threading.Event()

    def readline(self) -> str:
        self.release.wait(timeout=2)
        return ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("synthetic_node_environment")
async def test_pglite_start_bounds_a_silent_child_without_opening_a_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _BlockedNodeOutput()
    config = PGliteConfig(work_dir=tmp_path, timeout=1)
    config.timeout = 0
    process = MagicMock(stdout=output, poll=MagicMock(return_value=None))
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=process))
    monkeypatch.setattr(substrate, "PGliteConfig", MagicMock(return_value=config))
    monkeypatch.setattr(
        PGliteManager,
        "start",
        MagicMock(side_effect=AssertionError("vendor startup must not run")),
    )
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    engine = PGliteEngine(workdir=tmp_path, extensions=())
    handshake = AsyncMock()
    monkeypatch.setattr(engine, "_open_conn", handshake)

    try:
        with pytest.raises(RuntimeError, match="startup timeout"):
            await engine._start_once(tmp_path)
    finally:
        output.release.set()

    handshake.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("force_kill", [False, True])
async def test_pglite_shutdown_reaps_only_its_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    force_kill: bool,
) -> None:
    output = io.StringIO()
    wait = MagicMock(
        side_effect=[subprocess.TimeoutExpired("node", 5), 0] if force_kill else [0],
    )
    process = MagicMock(pid=12_345, stdout=output, wait=wait)
    manager = PGliteManager()
    monkeypatch.setattr(manager, "process", process)
    vendor_stop = MagicMock(side_effect=AssertionError("vendor cleanup must not run"))
    monkeypatch.setattr(manager, "stop", vendor_stop)
    killpg = MagicMock()
    monkeypatch.setattr(os, "killpg", killpg)
    engine = PGliteEngine(workdir=tmp_path, extensions=())
    engine._manager = manager

    await engine._shutdown()

    expected_signals = (
        [signal.SIGTERM, signal.SIGKILL] if force_kill else [signal.SIGTERM]
    )
    assert [call.args for call in killpg.call_args_list] == [
        (12_345, each) for each in expected_signals
    ]
    assert wait.call_count == (2 if force_kill else 1)
    assert engine._manager is None
    assert manager.process is None
    assert output.closed
    vendor_stop.assert_not_called()


def test_node_stdout_readiness_keeps_diagnostics_bounded_and_drains_to_eof() -> None:
    output = substrate._NodeOutput()
    stdout = io.StringIO(
        "Server started on socket test\n" + "x" * 8000 + "\nlast line\n",
    )

    substrate._consume_node_stdout(stdout, output=output)

    assert output.ready
    assert output.changed.is_set()
    assert len(output.tail) == 4000
    assert output.tail.endswith("\nlast line\n")
    assert stdout.read() == ""
    stdout.close()


@pytest.mark.asyncio
async def test_pglite_cancellation_waits_for_spawn_then_reaps_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    output = io.StringIO()
    wait = MagicMock(return_value=0)
    process = MagicMock(pid=12_345, stdout=output, wait=wait)

    def start(
        manager: PGliteManager,
        *,
        config: PGliteConfig,
        workdir: Path,
        cache: Path,
    ) -> None:
        del config, workdir, cache
        monkeypatch.setattr(manager, "process", process)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=2)

    monkeypatch.setattr(substrate, "_start_node", start)
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    monkeypatch.setattr(os, "killpg", MagicMock())
    engine = PGliteEngine(workdir=tmp_path, extensions=())
    handshake = AsyncMock()
    monkeypatch.setattr(engine, "_open_conn", handshake)
    task = asyncio.create_task(engine._start_once(tmp_path))
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "spawn must finish before cancellation cleanup"
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert engine._manager is None
    assert output.closed
    wait.assert_called_once_with(timeout=5)
    handshake.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("synthetic_node_environment")
async def test_failed_pglite_start_reaps_each_child_and_releases_every_boot_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = [io.StringIO("WASM trap\n") for _ in range(5)]
    waits = [MagicMock(return_value=13) for _ in range(5)]
    processes = [
        MagicMock(pid=12_345 + i, stdout=output, wait=wait, poll=lambda: 13)
        for i, (output, wait) in enumerate(zip(outputs, waits, strict=True))
    ]
    monkeypatch.setattr(subprocess, "Popen", MagicMock(side_effect=processes))
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="test-token")  # noqa: S106 -- Fixed ownership token in an isolated fixture.
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    released = MagicMock()
    monkeypatch.setattr(substrate, "_release_boot_slot", released)
    monkeypatch.setattr(os, "killpg", MagicMock())
    engine = PGliteEngine(workdir=tmp_path, extensions=())

    with pytest.raises(RuntimeError, match=_FAILED_START):
        await engine._start_with_retries()

    assert released.call_count == 5
    assert all(call.args == (slot,) for call in released.call_args_list)
    for wait, output in zip(waits, outputs, strict=True):
        wait.assert_called_once_with(timeout=5)
        assert output.closed
    assert engine._manager is None


@pytest.mark.usefixtures("synthetic_node_environment")
@pytest.mark.parametrize("node_options", [None, "--max-old-space-size=512"])
def test_node_launch_preserves_environment_and_applies_configured_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node_options: str | None,
) -> None:
    output = io.StringIO("Server started on socket test\n")
    process = MagicMock(stdout=output, poll=MagicMock(return_value=None))
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    config = PGliteConfig(work_dir=tmp_path, node_options=node_options)
    manager = PGliteManager(config)
    monkeypatch.setattr(
        os,
        "environ",
        {"PATH": "/synthetic/bin", "KEEP": "test", "NODE_OPTIONS": "--trace-warnings"},
    )

    substrate._start_node(manager, config=config, workdir=tmp_path, cache=tmp_path)

    expected_env = {
        "PATH": "/synthetic/bin",
        "KEEP": "test",
        "NODE_OPTIONS": "--trace-warnings",
        "NODE_PATH": str(tmp_path),
    }
    configured_options = node_options or ""
    if configured_options:
        expected_env["NODE_OPTIONS"] = configured_options
    assert spawn.call_args.kwargs["env"] == expected_env
    assert manager.process is process
    output.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("synthetic_node_environment")
async def test_pglite_missing_node_releases_slots_without_signalling_other_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn = MagicMock(side_effect=FileNotFoundError("node missing"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=tmp_path),
    )
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="test-token")  # noqa: S106 -- Fixed ownership token in an isolated fixture.
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    released = MagicMock()
    monkeypatch.setattr(substrate, "_release_boot_slot", released)
    killpg = MagicMock()
    monkeypatch.setattr(os, "killpg", killpg)
    engine = PGliteEngine(workdir=tmp_path, extensions=())

    with pytest.raises(
        RuntimeError,
        match=_FAILED_START,
    ) as failed:
        await engine._start_with_retries()

    assert isinstance(failed.value.__cause__, FileNotFoundError)
    assert spawn.call_count == 5
    assert released.call_count == 5
    assert engine._manager is None
    killpg.assert_not_called()


@pytest.mark.parametrize("force_kill", [False, True])
def test_owned_node_cleanup_without_process_group_support(
    monkeypatch: pytest.MonkeyPatch,
    *,
    force_kill: bool,
) -> None:
    wait = MagicMock(
        side_effect=[subprocess.TimeoutExpired("node", 5), 0] if force_kill else [0],
    )
    terminate = MagicMock()
    kill = MagicMock()
    process = MagicMock(wait=wait, terminate=terminate, kill=kill)
    manager = PGliteManager()
    monkeypatch.setattr(manager, "process", process)
    monkeypatch.delattr(os, "killpg", raising=False)

    substrate._stop_node(manager)

    terminate.assert_called_once_with()
    assert kill.call_count == int(force_kill)
    assert [call.kwargs for call in wait.call_args_list] == [
        {"timeout": 5},
        *([{"timeout": 2}] if force_kill else []),
    ]
    assert manager.process is None


def test_owned_node_cleanup_reaps_an_already_exited_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait = MagicMock(return_value=13)
    process = MagicMock(pid=12_345, wait=wait)
    manager = PGliteManager()
    monkeypatch.setattr(manager, "process", process)
    monkeypatch.setattr(os, "killpg", MagicMock(side_effect=ProcessLookupError))

    substrate._stop_node(manager)

    wait.assert_called_once_with(timeout=5)
    assert manager.process is None


@pytest.mark.asyncio
async def test_a_connection_reads_jsonb_as_json_and_numeric_as_float() -> None:
    """The codecs asyncpg is given: JSON both ways, and ``NUMERIC`` as a float."""
    conn = _CodecConnection()

    await substrate._init_connection(conn)

    jsonb, numeric = conn.codecs
    assert (jsonb.typename, jsonb.schema, jsonb.format) == (
        "jsonb",
        "pg_catalog",
        "text",
    )
    assert (numeric.typename, numeric.schema, numeric.format) == (
        "numeric",
        "pg_catalog",
        "text",
    )
    assert jsonb.encoder({"a": [1]}) == '{"a": [1]}'
    assert jsonb.decoder('{"a": [1]}') == {"a": [1]}
    assert numeric.encoder(1.25) == "1.25"
    assert numeric.decoder("1.250") == 1.25


@dataclass(frozen=True, slots=True, kw_only=True)
class _Codec:
    typename: str
    encoder: Callable[..., object]
    decoder: Callable[..., object]
    schema: str
    format: str


@dataclass(slots=True, kw_only=True)
class _CodecConnection:
    """The part of an asyncpg connection ``_init_connection`` talks to."""

    codecs: list[_Codec] = field(default_factory=list[_Codec])

    async def set_type_codec(
        self,
        typename: str,
        *,
        schema: str = "public",
        encoder: Callable[..., object],
        decoder: Callable[..., object],
        format: str = "text",
    ) -> None:
        self.codecs.append(
            _Codec(
                typename=typename,
                encoder=encoder,
                decoder=decoder,
                schema=schema,
                format=format,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [False, True])
async def test_a_start_over_a_unix_socket_listens_where_its_config_says(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    persist: bool,
) -> None:
    engine = PGliteEngine(workdir=tmp_path, extensions=(), persist=persist)
    configs: list[PGliteConfig] = []

    def make_manager(config: PGliteConfig) -> MagicMock:
        configs.append(config)
        return MagicMock()

    monkeypatch.setattr(substrate, "PGliteManager", make_manager)
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    monkeypatch.setattr(substrate, "_start_node", MagicMock())
    monkeypatch.setattr(engine, "_open_conn", AsyncMock(return_value=_make_conn()))

    await engine._start_once(tmp_path)

    (config,) = configs
    assert (config.work_dir, config.use_tcp, config.extensions) == (tmp_path, False, [])
    assert (config.log_level, config.timeout) == ("ERROR", 10)
    assert engine._socket_path == Path(config.socket_path)
    script = (tmp_path / "pglite_manager.js").read_text().splitlines()
    listen = '{"path": ' + json.dumps(config.socket_path) + "}"
    assert f"const LISTEN = {listen};" in script


@pytest.mark.asyncio
async def test_an_engine_persists_over_a_unix_socket_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=tmp_path)
    configs: list[PGliteConfig] = []

    def make_manager(config: PGliteConfig) -> MagicMock:
        configs.append(config)
        return MagicMock()

    monkeypatch.setattr(substrate, "PGliteManager", make_manager)
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    monkeypatch.setattr(substrate, "_start_node", MagicMock())
    monkeypatch.setattr(engine, "_open_conn", AsyncMock(return_value=_make_conn()))

    await engine._start_once(tmp_path)

    (config,) = configs
    assert (config.use_tcp, config.extensions) == (False, ["pgvector"])
    data_dir = json.dumps(str(tmp_path / PGLITE_DATA_DIRNAME))
    assert f"            dataDir: {data_dir}," in (
        (tmp_path / "pglite_manager.js").read_text().splitlines()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("spawn"), RuntimeError("boot")])
async def test_a_start_cancelled_while_its_child_fails_still_reaps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """The cancel is what the caller sees; the failed spawn is cleaned up under it."""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def start(
        manager: PGliteManager,
        *,
        config: PGliteConfig,
        workdir: Path,
        cache: Path,
    ) -> None:
        del manager, config, workdir, cache
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=2)
        raise error

    monkeypatch.setattr(substrate, "_start_node", start)
    monkeypatch.setattr(substrate, "_link_shared_node_modules", MagicMock())
    engine = PGliteEngine(workdir=tmp_path, extensions=())
    shutdown = AsyncMock()
    monkeypatch.setattr(engine, "_shutdown", shutdown)
    task = asyncio.create_task(engine._start_once(tmp_path))
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    shutdown.assert_awaited_once_with()


def test_npm_ci_installs_the_vendored_lockfile_whole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)

    substrate._run_npm_ci(tmp_path)

    assert (tmp_path / "package.json").read_bytes() == (
        substrate._CWD / "pglite-package.json"
    ).read_bytes()
    assert (tmp_path / "package-lock.json").read_bytes() == (
        substrate._CWD / "pglite-package-lock.json"
    ).read_bytes()
    run.assert_called_once_with(
        ["npm", "ci", "--no-audit", "--no-fund"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        timeout=substrate._INSTALL_TIMEOUT_SECONDS,
    )


@pytest.mark.asyncio
async def test_a_connection_is_tried_twelve_times_backing_off_a_quarter_second_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    manager = MagicMock(get_asyncpg_uri=MagicMock(return_value="postgresql://x"))
    monkeypatch.setattr(engine, "_manager", manager)
    connect = AsyncMock(side_effect=OSError("refused"))
    sleeps = AsyncMock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncio, "sleep", sleeps)

    with pytest.raises(OSError, match=r"^refused$"):
        await engine._open_conn()

    assert [call.args[0] for call in sleeps.await_args_list] == [
        0.25 * attempt for attempt in range(1, 13)
    ]
    assert connect.await_args is not None
    assert connect.await_args.kwargs == {
        "dsn": "postgresql://x",
        "ssl": False,
        "server_settings": {},
        "statement_cache_size": 0,
        "timeout": 10,
    }


@pytest.mark.asyncio
async def test_postgres_engine_closes_its_pool_when_the_listener_cannot_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_close = AsyncMock()
    monkeypatch.setattr(
        asyncpg,
        "create_pool",
        AsyncMock(return_value=AsyncMock(close=pool_close)),
    )
    monkeypatch.setattr(
        asyncpg,
        "connect",
        AsyncMock(side_effect=OSError("listener refused")),
    )
    engine = PostgresEngine("postgresql:///unused", listen_channel="ch")

    with pytest.raises(OSError, match=r"^listener refused$"):
        await engine.__aenter__()

    pool_close.assert_awaited_once_with()


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> float:
    """Stop the wall clock the stale checks read, at a whole second."""
    now = float(int(time.time()))
    monkeypatch.setattr(time, "time", lambda: now)
    return now


def test_a_boot_slot_exactly_as_old_as_the_stale_limit_is_still_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_now: float,
) -> None:
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 2)
    first = substrate._acquire_boot_slot(cache)
    edge = frozen_now - substrate._BOOT_SLOT_STALE_SECONDS
    os.utime(first.path, (edge, edge))

    second = substrate._acquire_boot_slot(cache)

    assert (first.name, second.name) == ("slot-0", "slot-1")
    assert substrate._boot_owner_path(first).exists()


def test_a_boot_slot_released_while_it_is_looked_at_is_taken_next_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 1)
    sleeps: list[float] = []
    monkeypatch.setattr(substrate, "_real_sleep", sleeps.append)
    original = Path.mkdir
    raced: list[Path] = []

    # The slot exists when claimed, and is gone by the time its age is read.
    def mkdir(path: Path, *args: object, **kwargs: bool) -> None:
        if path.name == "slot-0" and not raced:
            raced.append(path)
            raise FileExistsError(path)
        original(path, *cast(tuple[int], args), **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)

    slot = substrate._acquire_boot_slot(cache)

    assert slot.name == "slot-0"
    assert sleeps == [0.1]


def test_a_stale_boot_slot_that_cannot_be_removed_is_passed_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "_max_concurrent_boots", lambda: 2)
    stuck = substrate._acquire_boot_slot(cache)
    (stuck.path / "held").mkdir()
    (stuck.path / "held" / "file").touch()
    stale = time.time() - substrate._BOOT_SLOT_STALE_SECONDS - 60
    os.utime(stuck.path, (stale, stale))
    (stuck.path / "held").chmod(0o500)
    try:
        taken = substrate._acquire_boot_slot(cache)
    finally:
        (stuck.path / "held").chmod(0o700)

    assert taken.name == "slot-1"


@pytest.mark.parametrize(
    ("cpus", "boots"),
    [(None, 2), (8, 2), (20, 2), (40, 5), (80, 8)],
)
def test_cold_starts_are_capped_at_an_eighth_of_the_cpus_between_two_and_eight(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int | None,
    boots: int,
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)

    assert substrate._max_concurrent_boots() == boots


def test_an_install_a_crash_left_unfinished_is_installed_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache_under(tmp_path)
    monkeypatch.setattr(substrate, "pglite_version", lambda: "key")
    root = tmp_path / "caches" / "rekursiv-ai" / "pglite" / "node-modules" / "key"
    root.mkdir(parents=True)
    claimed: list[Path] = []
    original = substrate._try_acquire_install_lock

    def claim(lock: Path) -> bool:
        claimed.append(lock)
        return original(lock)

    monkeypatch.setattr(substrate, "_try_acquire_install_lock", claim)

    # A sibling reclaims the lock as stale while this install runs.
    def install(root: Path) -> None:
        (root / ".lock").rmdir()

    monkeypatch.setattr(substrate, "_run_npm_ci", install)

    assert substrate._ensure_shared_node_modules(cache) == root / "node_modules"
    assert claimed == [root / ".lock"]
    assert (root / ".ready").exists()


def test_a_workdir_that_cannot_hold_a_link_finds_the_install_by_node_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    monkeypatch.setattr(
        substrate,
        "_ensure_shared_node_modules",
        MagicMock(return_value=shared),
    )
    monkeypatch.setattr(Path, "symlink_to", MagicMock(side_effect=OSError("no links")))

    substrate._link_shared_node_modules(tmp_path, tmp_path)

    assert not (tmp_path / "node_modules").exists()


def test_an_install_lock_exactly_as_old_as_the_stale_limit_is_still_held(
    tmp_path: Path,
    frozen_now: float,
) -> None:
    lock = tmp_path / ".lock"
    lock.mkdir()
    edge = frozen_now - substrate._INSTALL_LOCK_STALE_SECONDS
    os.utime(lock, (edge, edge))

    assert substrate._try_acquire_install_lock(lock) is False
    assert lock.exists()


def test_an_install_lock_released_while_it_is_looked_at_is_tried_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = tmp_path / ".lock"

    def mkdir(path: Path) -> None:
        raise FileExistsError(path)

    monkeypatch.setattr(Path, "mkdir", mkdir)

    assert substrate._try_acquire_install_lock(lock) is False


@pytest.mark.asyncio
async def test_a_subscriber_holds_its_last_1024_payloads_by_default() -> None:
    bus = substrate._LocalBus()
    stream = bus.subscribe("events")
    first = asyncio.create_task(_anext(stream))
    await asyncio.sleep(0)
    bus.publish("events", "taken")
    assert await first == "taken"

    for each in range(1_025):
        bus.publish("events", str(each))

    assert await _anext(stream) == "1"
    await stream.aclose()


@pytest.mark.asyncio
async def test_a_start_makes_its_workdirs_missing_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(substrate, "_ensure_shared_node_modules", MagicMock())
    slot = substrate._BootSlot(path=tmp_path / "slot", token="t")  # noqa: S106 -- Test credentials are fixed fixtures and never real secrets.
    monkeypatch.setattr(substrate, "_acquire_boot_slot", MagicMock(return_value=slot))
    monkeypatch.setattr(substrate, "_release_boot_slot", MagicMock())
    workdir = tmp_path / "missing" / "wd"
    engine = PGliteEngine(workdir=workdir, extensions=())
    monkeypatch.setattr(engine, "_start_once", AsyncMock())

    await engine._start_with_retries()

    assert workdir.is_dir()


@pytest.mark.asyncio
async def test_an_owned_workdir_already_gone_ends_the_engine_quietly(
    tmp_path: Path,
) -> None:
    engine = PGliteEngine(workdir=tmp_path / "gone", extensions=(), own_workdir=True)

    await engine.__aexit__()

    assert not (tmp_path / "gone").exists()


def test_a_failed_read_of_node_output_keeps_the_last_4000_characters() -> None:
    class Failing:
        def __init__(self) -> None:
            self.lines = ["x" * 5_000]

        def readline(self) -> str:
            if self.lines:
                return self.lines.pop()
            raise OSError("pipe closed")

    output = substrate._NodeOutput()

    substrate._consume_node_stdout(Failing(), output=output)

    assert len(output.tail) == 4_000
    assert output.tail.endswith("xpipe closed")


def test_a_lock_no_guard_holds_has_no_owner() -> None:
    assert substrate._lock_owner(asyncio.Lock()) is None


def test_manager_js_lists_each_extension_on_its_own_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        substrate,
        "_EXTENSION_JS",
        {"a": ("alpha", "mod-a"), "b": ("beta", "mod-b")},
    )

    assert substrate._manager_js_extension_parts(("a", "b")) == (
        "const { alpha } = require('mod-a');\nconst { beta } = require('mod-b');",
        "{ a: alpha, b: beta }",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
