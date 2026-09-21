"""Export-root pytest plugin registration for trackinizer.

The shared PGlite fixtures (``pglite_engine`` and friends) live in
``trackinizer.lib.postgres.testing``. Fixtures in a plain module are invisible
to pytest unless the module is registered as a plugin, and ``pytest_plugins``
is honored only in the ROOTDIR conftest -- the package's own
``trackinizer/conftest.py`` is too deep. In the monorepo the repo-root conftest
does this; the public tree has no such root, so it is declared here.

``pytest_postgresql.plugin`` is registered here too, and only when psycopg can
load libpq. Its entry-point autoload is off (``-p no:pytest_postgresql`` in
``pyproject.toml``): the plugin imports psycopg at load time, so on a machine
without libpq it aborted collection before any conftest ran, taking the
PGlite-only tier down with it. Without libpq the real-Postgres tests skip at
``pg_dsn`` instead.
"""

from typing import Final

import importlib


def _postgres_plugins() -> tuple[str, ...]:
    """Name the pytest-postgresql plugin, if psycopg can load libpq here.

    Returns:
      plugins: ``("pytest_postgresql.plugin",)``, or ``()`` without libpq.

    """
    try:
        importlib.import_module("psycopg")
    except ImportError:
        return ()
    return ("pytest_postgresql.plugin",)


pytest_plugins: Final = ("trackinizer.lib.postgres.testing", *_postgres_plugins())
