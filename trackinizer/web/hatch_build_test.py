"""Tests for the build hook that builds the web app into the wheel."""

from __future__ import annotations

from typing import TYPE_CHECKING

import threading
import time

from trackinizer.web import hatch_build


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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

    def slow_npm(npm_web: Path, *args: str) -> None:
        calls.append(args[0])
        time.sleep(0.2)
        if args[:2] == ("run", "build"):
            (npm_web / "dist").mkdir(exist_ok=True)
            (npm_web / "dist" / "index.html").write_text("<html></html>\n")

    monkeypatch.setattr(hatch_build, "_npm", slow_npm)
    start = threading.Barrier(2)

    def build() -> None:
        start.wait()
        hatch_build.build_web_app(root=tmp_path, version="standard")

    threads = [threading.Thread(target=build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == ["ci", "run"]
    assert (web / "dist" / "index.html").is_file()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
