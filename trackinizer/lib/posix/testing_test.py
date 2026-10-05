"""Tests for the FSEvents stand-in that follower tests run against."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import asyncio
import os
import platform

import pytest

from trackinizer.lib.posix.follow import follow_dir
from trackinizer.lib.posix.testing import _changed, _stamps, poll_fsevents


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the macOS branch on every host; the stand-in only polls, so it runs."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")


def test_without_writes_a_write_is_not_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only files appearing, vanishing, or moving are named.

    The sentinel is created AFTER the append, so any poll that saw the append
    reported it no later than the sentinel's wake.
    """
    held = tmp_path / "held"
    _ = held.write_text("history\n")
    sentinel = tmp_path / "sentinel"
    poll_fsevents(monkeypatch, writes=False)

    async def run() -> list[set[Path]]:
        async with follow_dir(tmp_path) as changed:
            with held.open("a") as writer:
                _ = writer.write("appended\n")
            _ = sentinel.write_text("")
            return await asyncio.wait_for(_through(changed, sentinel), 5.0)

    wakes = asyncio.run(run())
    assert all(held not in wake for wake in wakes), f"a write was reported: {wakes}"


def test_with_writes_a_write_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = tmp_path / "held"
    _ = held.write_text("history\n")
    poll_fsevents(monkeypatch, writes=True)

    async def run() -> set[Path]:
        async with follow_dir(tmp_path) as changed:
            with held.open("a") as writer:
                _ = writer.write("appended\n")
            return await asyncio.wait_for(anext(changed), 5.0)

    assert asyncio.run(run()) == {held}


def test_with_writes_a_same_size_rewrite_in_one_tick_is_reported(
    tmp_path: Path,
) -> None:
    """FSEvents reports a rewrite that leaves the file's stat as it was.

    A filesystem stamping whole seconds gives a same-size rewrite within one
    tick the inode, size, and mtime it overwrote, so those alone miss it.
    Called directly: a poll landing between the write and the restored mtime
    would see the change through the mtime and hide the miss.
    """
    held = tmp_path / "held"
    _ = held.write_text("history\n")
    mtime_ns = held.stat().st_mtime_ns
    before = _stamps(tmp_path, contents=True)
    _ = held.write_text("HISTORY\n")
    os.utime(held, ns=(mtime_ns, mtime_ns))

    assert _changed(before, _stamps(tmp_path, contents=True), writes=True) == {held}


def test_stamps_skips_a_file_deleted_during_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "vanishing"
    _ = file_path.write_text("gone")

    def missing(path: Path) -> object:
        del path
        raise FileNotFoundError(file_path)

    monkeypatch.setattr(Path, "stat", missing)
    assert _stamps(tmp_path, contents=False) == {}


def test_a_symlinked_root_keeps_the_callers_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paths arrive resolved, as FSEvents sends them, and map back to the alias.

    The alias sits deeper than its target, so an unresolved report would be
    cut at the wrong depth by the root map and dropped.
    """
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "deeper" / "alias"
    alias.parent.mkdir()
    alias.symlink_to(physical, target_is_directory=True)
    poll_fsevents(monkeypatch, writes=False)

    async def run() -> set[Path]:
        async with follow_dir(alias) as changed:
            _ = (alias / "new").write_text("")
            return await asyncio.wait_for(anext(changed), 5.0)

    assert asyncio.run(run()) == {alias / "new"}


async def _through(changed: AsyncIterator[set[Path]], last: Path) -> list[set[Path]]:
    """Collect wakes up to and including the one naming ``last``."""
    wakes: list[set[Path]] = []
    async for wake in changed:
        wakes.append(wake)
        if last in wake:
            break
    return wakes


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
