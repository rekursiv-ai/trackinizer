"""The web app type-checks and passes its own tests, where the package publishes.

The monorepo runs these in a CI job of its own; the public repository has no
such job, so its CI runs them here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import subprocess

import pytest

from trackinizer.lib.testing.wheel import project_root


_CWD: Final = Path(__file__).resolve().parent

_WEB: Final = _CWD

pytestmark = [
    pytest.mark.cli_node,
    pytest.mark.xdist_group("web-app"),
    pytest.mark.skipif(
        project_root(anchor=_WEB / "app_test.py") is None,
        reason="the monorepo runs the web app's checks in its own CI job",
    ),
]


@pytest.mark.parametrize("script", ["typecheck", "test"])
def test_the_web_app_passes(script: str) -> None:
    subprocess.run(  # noqa: S603 -- Fixed argv; the project's own npm launcher.
        [str(_WEB / "npm"), "ci", "--no-audit", "--no-fund"],
        cwd=_WEB,
        check=True,
    )
    subprocess.run(  # noqa: S603 -- Fixed argv; the project's own npm launcher.
        [str(_WEB / "npm"), "run", script],
        cwd=_WEB,
        check=True,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
