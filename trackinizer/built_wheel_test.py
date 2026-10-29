"""The trackinizer wheel, with its built web app, run outside the checkout."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

import subprocess

import pytest

from trackinizer.lib.testing.wheel import built_wheel, project_root


_THIS: Final = Path(__file__).resolve()
_CWD: Final = _THIS.parent

if TYPE_CHECKING:
    from trackinizer.lib.testing.wheel import Wheel


pytestmark = pytest.mark.cli_uv

# Every package of the wheel's ``server`` extra, read from the wheel's own
# metadata, so a package added to the extra is checked with no edit here.
_NO_SERVER_PACKAGE = (
    "import importlib.metadata as m, importlib.util as u, re; "
    "import trackinizer.trax.cli, trackinizer.client.client; "
    "extra = [re.match(r'[A-Za-z0-9_.-]+', r)[0].replace('-', '_') "
    "for r in m.requires('trackinizer') if 'server' in r.partition(';')[2]]; "
    "assert extra, 'the wheel has no server extra'; "
    "leaked = [name for name in extra if u.find_spec(name)]; "
    "assert not leaked, leaked"
)


def test_the_web_app_type_checks_and_passes_its_tests() -> None:
    web = _web()
    for script in ("typecheck", "test"):
        _npm(web, "run", script)


def test_the_wheel_ships_the_built_web_app() -> None:
    assert "trackinizer/web/dist/index.html" in _wheel().names()


def test_the_client_runs_without_the_server_extra() -> None:
    """``pip install trackinizer`` is the client: ``trax`` and nothing of the server."""
    _wheel().run(_NO_SERVER_PACKAGE)


def test_the_server_extra_serves() -> None:
    _wheel().run("import trackinizer.server.server", extras=("server",))


@cache
def _wheel() -> Wheel:
    """Build the web app the wheel ships, then the wheel."""
    _npm(_web(), "run", "build")
    return built_wheel("trackinizer", anchor=__file__)


@cache
def _web() -> Path:
    """Return the web app's directory, installed; skip where no project builds it."""
    project = project_root("trackinizer", anchor=_THIS)
    if project is None:
        pytest.skip("trackinizer builds from its own project, not this checkout")
    web = project / "trackinizer" / "web"
    _npm(web, "ci", "--no-audit", "--no-fund")
    return web


def _npm(web: Path, *args: str) -> None:
    """Run npm, on the Node the test group installs, in ``web``."""
    subprocess.run(  # noqa: S603 -- Fixed argv; the project's own npm launcher.
        [str(web / "npm"), *args],
        cwd=web,
        check=True,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
