"""Tests for the per-adapter session-file discovery scan probe."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import sys

from trackinizer.docs.probes import scan_cost


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from trackinizer.runs.adapters.custom_types import Adapter


class _JsonlAdapter:
    """Offers fixed roots and parses ``*.jsonl`` files under them."""

    def __init__(self, name: str, *dirs: Path) -> None:
        self.name = name
        self._dirs = dirs

    def session_dirs(self) -> tuple[Path, ...]:
        return self._dirs

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl"


def _tree(root: Path) -> tuple[Path, Path]:
    """Make a root holding two logs (one nested), a non-log, and a directory."""
    present = root / "present"
    (present / "nested" / "deep.jsonl").parent.mkdir(parents=True)
    (present / "nested" / "deep.jsonl").write_text("{}\n")
    (present / "top.jsonl").write_text("{}\n")
    (present / "notes.txt").write_text("x")
    (present / "dir.jsonl").mkdir()
    return present, root / "absent"


def test_a_scan_counts_offered_dirs_and_the_logs_under_existing_ones(
    tmp_path: Path,
) -> None:
    present, absent = _tree(tmp_path)

    counts = scan_cost.scan_once(cast("Adapter", _JsonlAdapter("x", present, absent)))

    assert counts == (2, 2)


def test_report_prints_one_row_per_adapter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    present, _ = _tree(tmp_path)
    _install_adapters(monkeypatch, present)

    scan_cost.report(tick_sec=0.2, repeats=2)

    header, *rows = capsys.readouterr().out.splitlines()
    assert header.split() == ["adapter", "dirs", "matched", "median", "occupancy"]
    assert [row.split()[:3] for row in rows] == [
        ["claude", "1", "2"],
        ["codex", "1", "2"],
        ["gemini", "0", "0"],
    ]
    assert all(row.split()[3].endswith("ms") for row in rows)
    assert all(row.split()[4].endswith("%") for row in rows)


def test_main_reads_its_flags_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    present, _ = _tree(tmp_path)
    _install_adapters(monkeypatch, present)
    monkeypatch.setattr(sys, "argv", ["scan_cost", "--tick-sec", "1", "--repeats", "1"])

    assert scan_cost.main() == 0

    assert len(capsys.readouterr().out.splitlines()) == 4


def _install_adapters(monkeypatch: pytest.MonkeyPatch, present: Path) -> None:
    """Stand fake adapters in for the three CLIs the probe scans."""
    monkeypatch.setattr(
        scan_cost,
        "ClaudeAdapter",
        lambda: _JsonlAdapter("claude", present),
    )
    monkeypatch.setattr(
        scan_cost,
        "CodexAdapter",
        lambda: _JsonlAdapter("codex", present),
    )
    monkeypatch.setattr(scan_cost, "GeminiAdapter", lambda: _JsonlAdapter("gemini"))


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
