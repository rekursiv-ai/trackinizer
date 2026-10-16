"""Selector query budgets at the database boundary, without a database process."""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest

from trackinizer.conftest import executed_sql, make_store
from trackinizer.server.regex_timeout import STATEMENT_TIMEOUT_MS
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_export import EXPORT_TABLES


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True], ids=["whole-graph", "selector"])
async def test_query_budget_is_set_before_reading_a_selected_graph(
    selected: bool,
) -> None:
    """Bound selector reads while retaining the whole-graph backup's query budget."""
    store, engine = make_store()
    engine.conn.fetch.side_effect = [asyncpg.QueryCanceledError("query stopped"), []]
    selector = (Filter(field="labels", op="re", value="x"),) if selected else ()

    with pytest.raises(asyncpg.QueryCanceledError):
        await store.export_graph(selector=selector)

    statements = executed_sql(engine.conn)
    timeout = f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"
    if selected:
        assert timeout in statements
        first_read = next(
            i for i, sql in enumerate(statements) if sql.startswith("SELECT")
        )
        assert statements.index(timeout) < first_read
    else:
        assert timeout not in statements


@pytest.mark.asyncio
async def test_store_preserves_selected_rows_external_references() -> None:
    """Keep external IDs on the database's selected rows without extra lookups."""
    store, engine = make_store()
    inquiry_id, codechange_id, audit_id, cause_id = (UUID(int=i) for i in range(1, 5))
    catalog = [
        {
            "table_name": table,
            "column_name": column,
            "data_type": "ARRAY" if column == "experiment_codechanges" else "uuid",
        }
        for table, columns in (
            ("inquiries", ("id", "experiment_codechanges")),
            ("edges", ("from_id", "to_id")),
            ("change_log", ("id", "subject_id", "caused_by")),
            ("experiment_metrics", ("experiment_id",)),
            ("session_manifests", ("session_id",)),
            ("session_records", ("session_id",)),
            ("session_slash_commands", ("session_id",)),
        )
        for column in columns
    ]
    engine.conn.fetch.side_effect = [
        [{"name": "schema.sql"}],
        catalog,
        [{"id": inquiry_id, "experiment_codechanges": [codechange_id]}],
        [],
        [{"id": audit_id, "subject_id": inquiry_id, "caused_by": cause_id}],
        [],
        [],
        [],
        [],
    ]
    engine.conn.fetchval.return_value = 0

    graph = await store.export_graph(
        selector=(Filter(field="labels", op="is", value="org:rekursiv"),),
    )

    tables = dict(graph.tables)
    assert tables["inquiries"] == (
        {"id": inquiry_id, "experiment_codechanges": [codechange_id]},
    )
    assert tables["change_log"] == (
        {"id": audit_id, "subject_id": inquiry_id, "caused_by": cause_id},
    )


@pytest.mark.asyncio
async def test_the_catalog_drops_derived_columns_and_decodes_json_ones() -> None:
    """``search`` is skipped wherever it falls; a ``json`` column arrives decoded."""
    store, engine = make_store()
    catalog = [
        {"table_name": "inquiries", "column_name": "id", "data_type": "uuid"},
        {"table_name": "inquiries", "column_name": "meta", "data_type": "json"},
        {"table_name": "session_records", "column_name": "search", "data_type": "x"},
        {"table_name": "session_records", "column_name": "text", "data_type": "text"},
        {"table_name": "session_records", "column_name": "idx", "data_type": "int"},
    ]
    rows = {
        "inquiries": [{"id": "i", "meta": '{"k":1}'}],
        "session_records": [{"text": "t", "idx": 0}],
    }
    engine.conn.fetch.side_effect = [
        [{"name": "schema.sql"}],
        catalog,
        *(rows.get(table, []) for table in EXPORT_TABLES),
    ]
    engine.conn.fetchval.return_value = 0

    graph = await store.export_graph()

    tables = dict(graph.tables)
    assert tables["inquiries"] == ({"id": "i", "meta": {"k": 1}},)
    assert tables["session_records"] == ({"text": "t", "idx": 0},)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
