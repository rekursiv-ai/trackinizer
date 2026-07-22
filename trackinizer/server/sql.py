"""Asset-loading helpers for SQL files under ``assets/``.

Lives in its own module so ``custom_types.py`` can stay types-only while
``trackinizer.py`` owns the Store substrate without forming a cycle.

Two helpers:

* :func:`load_sql` -- one-off lookup by stem (``next_issue``,
  ``cost_subtree``, ...). Used by query consumers.
* :func:`schema_migrations` -- load the canonical schema asset for bootstrap.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import functools


@functools.cache
def load_sql(name: str) -> str:
    """Load a SQL query from ``assets/<name>.sql`` (cached per-name).

    Args:
      name: Stem of the SQL file under ``assets/``, without extension.

    Returns:
      sql: File contents as a UTF-8 string.

    """
    return (Path(__file__).parent / "assets" / f"{name}.sql").read_text(
        encoding="utf-8"
    )


def schema_migrations() -> Iterator[tuple[str, str]]:
    """Yield ``(name, body)`` for the baseline then each numbered migration.

    ``assets/schema.sql`` is the baseline -- the current canonical
    schema, applied to fresh databases. ``assets/schema.NNN.sql`` files
    (``schema.001.sql``, ``schema.002.sql``, ...) are additive
    migrations applied to already-deployed databases that have outgrown
    the baseline. They run in lexical order *after* the baseline.

    ``name`` is the file basename and the primary key in
    ``applied_migrations``; both baseline and numbered files are
    recorded there so each runs exactly once per database.

    Yields:
      pair: ``(name, body)`` -- baseline first, then each
        ``schema.NNN.sql`` in lexical order.

    """
    assets = Path(__file__).parent / "assets"
    baseline = assets / "schema.sql"
    yield baseline.name, baseline.read_text(encoding="utf-8")
    for path in sorted(assets.glob("schema.*.sql")):
        yield path.name, path.read_text(encoding="utf-8")
