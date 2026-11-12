"""Tests for the run-hook that downloads and warms the session embedder."""

from __future__ import annotations

from subprocess import CompletedProcess
from typing import TYPE_CHECKING

import shutil
import subprocess

from trackinizer.server import prep_models


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_no_configured_embedder_has_nothing_to_prepare(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TRACKINIZER_SESSION_EMBEDDER", raising=False)

    assert prep_models._prepare() == 0

    assert "No session embedder configured" in capsys.readouterr().out


def test_a_configured_embedder_is_warmed_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRACKINIZER_SESSION_EMBEDDER", "stub-1024")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    (tmp_path / "weights.bin").write_bytes(b"\0" * 4096)

    assert prep_models._prepare() == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "model: stub-1024",
        "dim: 1024 (probe vector length 1024)",
        f"cache: {tmp_path}",
    ]
    assert lines[3].startswith("cache size: ")
    assert lines[3] != "cache size: (unknown)"


def test_an_absent_cache_directory_is_said_so(tmp_path: Path) -> None:
    assert (
        prep_models._directory_size(str(tmp_path / "missing"))
        == "(cache directory not present)"
    )


def test_a_host_without_du_says_so(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def nowhere(name: str) -> None:
        del name

    monkeypatch.setattr(shutil, "which", nowhere)

    assert prep_models._directory_size(str(tmp_path)) == "(du unavailable)"


def test_du_printing_nothing_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def silent(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        del kwargs
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", silent)

    assert prep_models._directory_size(str(tmp_path)) == "(unknown)"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
