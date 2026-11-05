"""Tests for the build hook that builds the web app into the wheel."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING

import fcntl
import os
import threading

import pytest

from trackinizer.web import hatch_build


if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _web(root: Path, *, built: bool) -> Path:
    web = root / "trackinizer" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text("{}\n", encoding="utf-8")
    if built:
        (web / "dist").mkdir()
        (web / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    return web


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_npm(web: Path, *args: str) -> None:
        calls.append([str(web), *args])

    monkeypatch.setattr(hatch_build, "_npm", fake_npm)
    return calls


def test_a_wheel_build_builds_the_web_app_it_ships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``uv build`` alone makes a wheel with the app, which used to need a step first."""
    calls = _recorded(monkeypatch)
    web = _web(tmp_path, built=False)

    hatch_build.build_web_app(root=tmp_path, version="standard")

    assert calls == [
        [str(web), "ci", "--no-audit", "--no-fund"],
        [str(web), "run", "build"],
    ]


def test_an_editable_install_builds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``uv sync`` installs editable; it must not need Node."""
    calls = _recorded(monkeypatch)
    _web(tmp_path, built=False)

    hatch_build.build_web_app(root=tmp_path, version="editable")

    assert calls == []


def test_a_built_app_is_not_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wheel built from the sdist finds the app the sdist carries."""
    calls = _recorded(monkeypatch)
    _web(tmp_path, built=True)

    hatch_build.build_web_app(root=tmp_path, version="standard")

    assert calls == []


def test_concurrent_wheel_builds_build_the_app_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``uv build``s share ``web/``; one's vite rewrote ``dist`` as the other packed it."""
    web = _web(tmp_path, built=False)
    calls: list[str] = []

    def locked_npm(npm_web: Path, *args: str) -> None:
        _assert_locked(npm_web)
        calls.append(args[0])
        if args[:2] == ("run", "build"):
            (npm_web / "dist").mkdir(exist_ok=True)
            (npm_web / "dist" / "index.html").write_text("<html></html>\n")

    monkeypatch.setattr(hatch_build, "_npm", locked_npm)
    start = threading.Barrier(2)
    web_lock = hatch_build.web_lock

    @contextmanager
    def contended_lock(path: Path) -> Generator[None]:
        start.wait(timeout=2)
        with web_lock(path):
            yield

    monkeypatch.setattr(hatch_build, "web_lock", contended_lock)

    def build() -> None:
        hatch_build.build_web_app(root=tmp_path, version="standard")

    with ThreadPoolExecutor(max_workers=2) as pool:
        builds = [pool.submit(build) for _ in range(2)]
        for result in builds:
            result.result()

    assert calls == ["ci", "run"]
    assert (web / "dist" / "index.html").is_file()


def test_web_lock_lets_one_holder_in_at_a_time(tmp_path: Path) -> None:
    """The app's own tests take this lock too, so their ``npm ci`` cannot overlap.

    Two parametrized cases each ran ``npm ci`` in ``web/``; on two workers one
    wiped ``node_modules`` while the other used it.
    """
    web = _web(tmp_path, built=False)
    inside: list[int] = []
    overlaps: list[int] = []
    start = threading.Barrier(2)

    def hold() -> None:
        start.wait()
        with hatch_build.web_lock(web):
            inside.append(1)
            overlaps.append(len(inside))
            _assert_locked(web)
            inside.pop()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holders = [pool.submit(hold) for _ in range(2)]
        for result in holders:
            result.result()

    assert overlaps == [1, 1]
    descriptor = os.open(web, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def _assert_locked(web: Path) -> None:
    """Probe a separate open description while the real holder owns the lock."""
    descriptor = os.open(web, os.O_RDONLY)
    try:
        for mode in (fcntl.LOCK_EX, fcntl.LOCK_SH):
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
