#!/usr/bin/env python
"""Measure the per-tick session-file discovery scan cost, per adapter.

Reproduces the table in ``design_agent_session_logging.md``'s addendum.
Numbers are machine- and history-dependent: the claude figure scales with
how many project directories that CLI has ever created on THIS host, so a
fresh machine shows a small number and a long-lived one a large one.

Run:  uv --quiet run --frozen python trackinizer/docs/probes/scan_cost.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import argparse
import statistics
import time

from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter


if TYPE_CHECKING:
    from trackinizer.trax.run.adapters.base import Adapter


def scan_once(adapter: Adapter) -> tuple[int, int]:
    """One full discovery sweep, mirroring ``_scan_and_read``'s walk.

    Returns:
      dirs: How many session directories the adapter offered.
      matched: How many files passed ``matches_session_file``.

    """
    dirs = list(adapter.session_dirs())
    matched = 0
    for session_dir in dirs:
        if not session_dir.is_dir():
            continue
        for path in session_dir.rglob("*"):
            if path.is_file() and adapter.matches_session_file(path):
                matched += 1
    return len(dirs), matched


def report(*, tick_sec: float, repeats: int) -> None:
    """Print one row per adapter: fan-out, match count, and tick occupancy.

    ``tick_sec`` mirrors the drain loop's poll interval in ``run/session.py``.
    Occupancy is the share of each tick the drain thread spends walking
    directories -- wall-clock time in that thread, largely ``stat()`` syscalls,
    NOT a CPU-utilization figure.
    """
    print(f"{'adapter':8} {'dirs':>6} {'matched':>8} {'median':>10} {'occupancy':>10}")
    for adapter in (ClaudeAdapter(), CodexAdapter(), GeminiAdapter()):
        times: list[float] = []
        dirs = matched = 0
        for _ in range(repeats):
            start = time.perf_counter()
            dirs, matched = scan_once(adapter)
            times.append(time.perf_counter() - start)
        median = statistics.median(times)
        print(
            f"{adapter.name:8} {dirs:6} {matched:8} "
            f"{median * 1000:9.1f}ms {median / tick_sec:9.0%}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="scan_cost", description=__doc__)
    parser.add_argument(
        "--tick-sec",
        type=float,
        default=0.2,
        help="drain poll interval to score occupancy against",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="scans per adapter; median is reported"
    )
    args = parser.parse_args()
    report(tick_sec=args.tick_sec, repeats=args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
