"""Export-root pytest plugin registration for trackinizer.

The shared PGlite fixtures (``pglite_engine`` and friends) live in
``trackinizer.lib.postgres.testing``. Fixtures in a plain module are invisible
to pytest unless the module is registered as a plugin, and ``pytest_plugins``
is honored only in the ROOTDIR conftest -- the package's own
``trackinizer/conftest.py`` is too deep. In the monorepo the repo-root conftest
does this; the public tree has no such root, so it is declared here.
"""

from typing import Final


pytest_plugins: Final = ("trackinizer.lib.postgres.testing",)
