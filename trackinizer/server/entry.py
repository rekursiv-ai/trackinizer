"""Console-script entry for ``trackinizer``: names the extra a bare install lacks.

A bare ``pip install trackinizer`` carries the client side only. ``server.server``
imports FastAPI and asyncpg at module scope, so on that install the import fails
before its ``main`` can run, and the user reads a traceback that names a package
instead of the extra that supplies it. This module imports nothing server-only,
so it can say what to install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import sys


if TYPE_CHECKING:
    from trackinizer.server.server import main as serve
else:
    from wrapt import lazy_import

    serve = lazy_import("trackinizer.server.server", "main")

SERVER_ONLY: Final = frozenset(
    {
        "asyncpg",
        "configgle",
        "fastapi",
        "huggingface_hub",
        "itsdangerous",
        "peft",
        "py_pglite",
        "starlette",
        "torch",
        "transformers",
        "uvicorn",
        "zstandard",
    },
)
"""The import name of each package in the ``server`` extra of ``pyproject.toml``."""


def main() -> int:
    """Run the server, or name the extra to install when a package of it is missing.

    Returns:
      code: The server's exit code, or 2 when a package of the ``server`` extra
        is not installed.

    """
    try:
        return serve()
    except ModuleNotFoundError as err:
        if err.name is None or err.name.partition(".")[0] not in SERVER_ONLY:
            raise
        sys.stderr.write("the server needs: pip install 'trackinizer[server]'\n")
        return 2
