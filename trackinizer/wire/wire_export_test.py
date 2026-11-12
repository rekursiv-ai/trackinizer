from __future__ import annotations

from trackinizer.wire.wire_export import (
    EXPORT_API_PATH,
    EXPORT_API_PATHS,
    EXPORT_SELECTOR_FIELDS,
    EXPORT_TABLES,
)


def test_registry_names_the_export_route() -> None:
    assert EXPORT_API_PATHS == (EXPORT_API_PATH,)
    assert EXPORT_API_PATH == "/api/export"


def test_tables_write_parents_first_once_each() -> None:
    assert len(set(EXPORT_TABLES)) == len(EXPORT_TABLES)
    assert EXPORT_TABLES.index("inquiries") < EXPORT_TABLES.index("edges")
    assert EXPORT_TABLES.index("session_manifests") < EXPORT_TABLES.index(
        "session_records",
    )
    assert "api_keys" not in EXPORT_TABLES


def test_selector_filters_on_labels_only() -> None:
    assert frozenset({"labels"}) == EXPORT_SELECTOR_FIELDS


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
