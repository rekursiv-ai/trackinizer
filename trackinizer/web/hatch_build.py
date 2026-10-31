"""Build the web app before hatch packs the wheel, so ``uv build`` alone ships it.

The wheel carries ``trackinizer/web/dist`` as an artifact. Hatch packs an
artifact only if it exists, and builds without one silently, so every caller of
``uv build`` once had to run ``npm run build`` first. This hook runs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import override

import fcntl
import os
import subprocess

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebAppHook(BuildHookInterface[BuilderConfig]):
    """Build the web app for a wheel or sdist that does not carry it yet."""

    PLUGIN_NAME = "custom"

    @override
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del build_data
        build_web_app(root=Path(self.root), version=version)


def build_web_app(*, root: Path, version: str) -> None:
    """Build ``root``'s web app unless the build is editable or the app is built.

    Args:
      root: The project root.
      version: The hatch build version; ``editable`` for ``uv sync``, which
        must not need Node.

    """
    if version == "editable":
        return
    web = root / "trackinizer" / "web"
    # Concurrent builds share ``web/``: unserialized, one's ``npm ci`` and vite
    # rewrote ``node_modules`` and ``dist`` while the other's hatch packed them.
    # The lock is on the directory itself, so no lock file lands in the sdist.
    descriptor = os.open(web, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if (web / "dist" / "index.html").is_file():
            return
        _npm(web, "ci", "--no-audit", "--no-fund")
        _npm(web, "run", "build")
    finally:
        os.close(descriptor)


def _npm(web: Path, *args: str) -> None:
    """Run npm, on the Node the test group installs, in ``web``."""
    subprocess.run(  # noqa: S603 -- Fixed argv; the project's own npm launcher.
        [str(web / "npm"), *args],
        cwd=web,
        check=True,
    )
