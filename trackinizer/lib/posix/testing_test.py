"""Tests for the FSEvents stand-in that follower tests run against."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import platform

import pytest

from trackinizer.lib.posix.follow import follow_dir
from trackinizer.lib.posix.testing import poll_fsevents


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


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
