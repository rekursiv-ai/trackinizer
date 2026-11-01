"""The web app type-checks and passes its own tests, where the package publishes.

The monorepo runs these in its ``trackinizer-ui`` CI job; the public repository
has no such job, so its CI runs them here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import subprocess

import pytest

from trackinizer.lib.testing.wheel import project_root
from trackinizer.web.hatch_build import web_lock


_CWD: Final = Path(__file__).resolve().parent

_WEB: Final = _CWD

pytestmark = [
    pytest.mark.cli_node,
    pytest.mark.skipif(
        project_root(anchor=_WEB / "app_test.py") is None,
        reason="the monorepo runs the web app's checks in its own CI job",
    ),
]


# One test, under the build hook's lock: two cases each ran ``npm ci`` here, and on
# two workers one wiped ``node_modules`` while the other used it.
def test_the_web_app_type_checks_and_passes_its_tests() -> None:
    with web_lock(_WEB):
        for args in (
            ("ci", "--no-audit", "--no-fund"),
            ("run", "typecheck"),
            ("test",),
        ):
            subprocess.run(  # noqa: S603 -- Fixed argv; the project's own npm launcher.
                [str(_WEB / "npm"), *args],
                cwd=_WEB,
                check=True,
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
