"""Tests for loading the SQL assets: one query by stem, and the schema in order."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from trackinizer.server.sql import load_sql, schema_migrations


_CWD: Final[Path] = Path(__file__).resolve().parent

_ASSETS = _CWD / "assets"


def test_load_sql_is_the_assets_file_and_cached_per_name() -> None:
    expected = (_ASSETS / "schema.sql").read_text(encoding="utf-8")

    assert load_sql("schema") == expected
    assert load_sql("schema") is load_sql("schema")


def test_load_sql_of_an_unknown_stem_raises() -> None:
    with pytest.raises(FileNotFoundError):
        _ = load_sql("no_such_query")


def test_the_baseline_comes_first_then_each_migration_in_lexical_order() -> None:
    migrations = list(schema_migrations())
    names = [name for name, _ in migrations]

    assert names[0] == "schema.sql"
    assert names[1:] == sorted(path.name for path in _ASSETS.glob("schema.*.sql"))
    assert len(names) > 1
    assert all(
        body == (_ASSETS / name).read_text(encoding="utf-8")
        for name, body in migrations
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
