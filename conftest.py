"""Export-root pytest plugin registration for trackinizer.

The shared PGlite fixtures (``pglite_engine`` and friends) live in
``trackinizer.lib.postgres.testing``. Fixtures in a plain module are invisible
to pytest unless the module is registered as a plugin, and ``pytest_plugins``
is honored only in the ROOTDIR conftest -- the package's own
``trackinizer/conftest.py`` is too deep. In the monorepo the repo-root conftest
does this; the public tree has no such root, so it is declared here.

``pytest_postgresql`` loads through its own entry point: the test group pins
``psycopg[binary]``, whose bundled libpq lets that plugin import on any machine.

The math-thread caps run here, at the root, because this conftest loads before
any test or package conftest imports NumPy or torch. In the monorepo the
repo-root conftest applies them; the public tree has no such root.
"""

from typing import Final

from trackinizer.lib.testing.resources import cap_math_threads


cap_math_threads()


pytest_plugins: Final = ("trackinizer.lib.postgres.testing",)
