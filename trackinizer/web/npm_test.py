"""Tests for ``web/npm``: the Node shim it publishes is usable by the whole group.

The script runs from a copy in a temporary tree, with ``uv`` and ``node`` faked on
PATH, so nothing here touches the checkout's own ``.node-bin`` or the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import os
import shutil
import subprocess

import pytest


_CWD: Final = Path(__file__).resolve().parent


def test_the_shim_takes_the_umask_not_mktemps_owner_only_mode(tmp_path: Path) -> None:
    """Under the ops umask the shim is group-usable, as ``mkdir`` would make it.

    ``mktemp -d`` makes 0700 whatever the umask. In a web worktree a deploy group
    shares, that locked every operator but the shim's creator out of npm, and a
    colleague's redeploy failed with "npm: Permission denied" (2026-09-29).
    """
    web = tmp_path / "root" / "loop" / "trackinizer" / "web"

    result, _ = _run_shim(tmp_path, web)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("npm-cli.js --version")
    (shim,) = (path for path in (web / ".node-bin").iterdir() if path.is_symlink())
    assert oct(shim.resolve().stat().st_mode & 0o777) == "0o770"


@pytest.mark.parametrize(
    "layout",
    [
        "trackinizer/web",
    ],
)
def test_uv_finds_the_project_from_the_web_directory(
    tmp_path: Path,
    layout: str,
) -> None:
    """The monorepo and the public package hold web/ at different depths."""
    web = tmp_path / "root" / layout

    result, uv_args = _run_shim(tmp_path, web)

    assert result.returncode == 0, result.stderr
    assert Path(uv_args[uv_args.index("--project") + 1]).resolve() == web.resolve()


def _run_shim(
    tmp_path: Path,
    web: Path,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run a copy of the shim at ``web`` under the ops umask; return uv's arguments."""
    web.mkdir(parents=True)
    _ = shutil.copy2(_CWD / "npm", web / "npm")
    node_bin = tmp_path / "node" / "bin"
    node_bin.mkdir(parents=True)
    _executable(node_bin / "node", 'echo "node $*"')
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    uv_log = tmp_path / "uv.args"
    _executable(fakes / "uv", f'printf "%s\\n" "$@" > {uv_log}\necho {node_bin}')
    result = subprocess.run(  # noqa: S603 -- runs the repository's own script.
        ["/bin/sh", "-c", 'umask 007 && exec "$0" --version', str(web / "npm")],
        env={**os.environ, "PATH": f"{fakes}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, uv_log.read_text().splitlines()


def _executable(path: Path, body: str) -> None:
    _ = path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
