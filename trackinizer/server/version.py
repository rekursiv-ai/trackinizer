"""Resolve the running server's build SHA, for stale-deploy detection.

A client (or operator) compares this against the SHA it expects deployed.
When the live binary predates a push, the divergence is the direct signal
-- rather than inferring staleness from wrong responses (e.g. a dropped
``seq_range`` returning out-of-window rows).

Resolution order, first hit wins, evaluated once and cached:

  1. ``$TRACKINIZER_SHA`` -- set by the deploy at launch; authoritative
     and cheap, the only source that works when ``.git`` is absent (e.g.
     a build artifact or container without the repo).
  2. ``git rev-parse HEAD`` in the package's checkout -- the dev/local
     fallback when no env var is injected.
  3. ``"unknown"`` -- neither available; the endpoint still answers so a
     caller can distinguish "old server without /version" (404) from
     "server that cannot name its build" (200 with ``"unknown"``).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import os
import subprocess


@cache
def build_sha() -> str:
    """Return the running build's git SHA, or ``"unknown"``.

    Cached: the SHA is fixed for a process lifetime, so the env read and
    the at-most-one ``git`` subprocess happen on the first call only.
    """
    # Strip before the truthiness test: a whitespace-only value is truthy
    # but strips to "", which would otherwise be returned as a blank SHA
    # instead of falling through to the git probe / "unknown".
    if env := os.environ.get("TRACKINIZER_SHA", "").strip():
        return env
    try:
        # ``git`` by name resolves via PATH on purpose: the deploy may run
        # from any checkout and no fixed absolute path is portable. Args are
        # a fixed tuple (no shell, no user input), so PATH search is the only
        # variability and it is benign here.
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),  # noqa: S607 -- PATH-resolved git; fixed args, no shell.
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"
