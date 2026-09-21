"""Download and warm the configured session-search embedder.

Maintenance entry point (run-hook): downloads the ``TRACKINIZER_SESSION_EMBEDDER``
model's weights into the provisioned HF cache (``HF_HOME`` via ops/env; no
``cache_dir`` override), loads it, embeds one probe, and prints its identity,
dimension, and cache footprint. Idempotent: a second run reuses the cache and
only re-loads. Run it once per host BEFORE serving so the request path never
triggers an 8 GB in-band download.

    python -m trackinizer.server.prep_models

With no session embedder configured (the default) it prints that fact and
exits 0 -- nothing to prepare, semantic search stays full-text-only.
"""

from __future__ import annotations

# ruff: noqa: T201 -- a maintenance run-hook; its prints are the CLI output.
from pathlib import Path

import asyncio
import os
import shutil
import subprocess

from trackinizer.server.config import Config
from trackinizer.server.embedders.registry import build_session_embedder


def _prepare() -> int:
    """Build the configured session embedder, warm it, and report; return exit code."""
    config = Config.from_env()
    embedder = build_session_embedder(config.session_embedder)
    if embedder is None:
        print(
            "No session embedder configured (TRACKINIZER_SESSION_EMBEDDER is "
            "unset); nothing to prepare. Semantic session search stays "
            "full-text-only until one is set.",
        )
        return 0
    # The first embed triggers the lazy load, which downloads the weights into
    # the HF cache when absent (the ONE place a download is allowed) and loads
    # them; a second run finds the cache and only re-loads.
    probe = asyncio.run(embedder.embed_query("probe"))
    print(f"model: {embedder.name}")
    print(f"dim: {embedder.dim} (probe vector length {len(probe)})")
    cache_home = os.environ.get("HF_HOME", "~/.cache/huggingface")
    print(f"cache: {cache_home}")
    print(f"cache size: {_directory_size(cache_home)}")
    return 0


def _directory_size(path: str) -> str:
    """Return ``du -sh`` for ``path``, or a fallback string when unavailable."""
    resolved = Path(path).expanduser()
    if not resolved.is_dir():
        return "(cache directory not present)"
    du = shutil.which("du")
    if du is None:
        return "(du unavailable)"
    result = subprocess.run(  # noqa: S603 -- fixed argv, resolved du path.
        [du, "-sh", str(resolved)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.split("\t", 1)[0].strip() or "(unknown)"


if __name__ == "__main__":
    raise SystemExit(_prepare())
