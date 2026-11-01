"""Selector query budgets at the database boundary, without a database process."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override
from uuid import UUID

import asyncpg
import pytest

from trackinizer.conftest import executed_sql, make_store
from trackinizer.server.access_policy import DefaultPolicy, Principal
from trackinizer.server.regex_timeout import STATEMENT_TIMEOUT_MS
from trackinizer.server.store.export import _Kept
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_export import EXPORT_TABLES


if TYPE_CHECKING:
    from trackinizer.server.access_policy import ResourceKind


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True], ids=["whole-graph", "selector"])
async def test_query_budget_is_set_before_reading_a_selected_graph(
    selected: bool,
) -> None:
    """Bound selector reads while retaining the whole-graph backup's query budget."""
    store, engine = make_store()
    engine.conn.fetch.side_effect = [
        asyncpg.QueryCanceledError("query stopped"),
        list[dict[str, object]](),
    ]
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
        list[dict[str, object]](),
        [{"id": audit_id, "subject_id": inquiry_id, "caused_by": cause_id}],
        list[dict[str, object]](),
        list[dict[str, object]](),
        list[dict[str, object]](),
        list[dict[str, object]](),
        # The withheld sessions.
        list[dict[str, object]](),
        # The read inquiries and changes of installed kinds.
        [{"id": inquiry_id}],
        [{"id": audit_id}],
    ]

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
        {
            "table_name": "session_records",
            "column_name": "session_id",
            "data_type": "u",
        },
        {"table_name": "session_records", "column_name": "search", "data_type": "x"},
        {"table_name": "session_records", "column_name": "text", "data_type": "text"},
        {"table_name": "session_records", "column_name": "idx", "data_type": "int"},
    ]
    rows = {
        "inquiries": [{"id": "i", "meta": '{"k":1}'}],
        "session_records": [{"session_id": "i", "text": "t", "idx": 0}],
    }
    engine.conn.fetch.side_effect = [
        [{"name": "schema.sql"}],
        catalog,
        *(rows.get(table, []) for table in EXPORT_TABLES),
        list[dict[str, object]](),
        # The read inquiries and changes of installed kinds.
        [{"id": "i"}],
        list[dict[str, object]](),
    ]

    graph = await store.export_graph()

    tables = dict(graph.tables)
    assert tables["inquiries"] == ({"id": "i", "meta": {"k": 1}},)
    assert tables["session_records"] == ({"session_id": "i", "text": "t", "idx": 0},)


@pytest.mark.asyncio
async def test_the_policy_decides_what_is_kept_after_the_snapshot_commits() -> None:
    """A read-only snapshot reads no grant; the policy keeps rows once it commits."""
    store, engine = make_store()
    store.access_policy = _Marked()
    kept, hidden, change, dropped = (UUID(int=i) for i in range(1, 5))
    catalog = [
        {"table_name": table, "column_name": column, "data_type": "uuid"}
        for table, columns in (
            ("inquiries", ("id",)),
            ("edges", ("from_id", "to_id")),
            ("change_log", ("id", "subject_id")),
        )
        for column in columns
    ]
    rows: dict[str, list[dict[str, object]]] = {
        "inquiries": [{"id": kept}, {"id": hidden}],
        "edges": [{"from_id": kept, "to_id": hidden}, {"from_id": kept, "to_id": kept}],
        "change_log": [
            {"id": change, "subject_id": kept},
            {"id": dropped, "subject_id": hidden},
        ],
    }
    engine.conn.fetch.side_effect = [
        [{"name": "schema.sql"}],
        catalog,
        *(rows.get(table, []) for table in EXPORT_TABLES),
        # Withheld sessions, then what the policy keeps of the inquiries and changes.
        [{"id": hidden}, {"id": kept}],
        [{"id": kept}],
        [{"id": change}],
    ]

    graph = await store.export_graph(
        principal=Principal(email="bea@example.com", role="viewer"),
    )

    tables = dict(graph.tables)
    assert tables["inquiries"] == ({"id": kept},)
    assert tables["edges"] == ({"from_id": kept, "to_id": kept},)
    assert tables["change_log"] == ({"id": change, "subject_id": kept},)
    assert graph.withheld_sessions == 1
    statements = executed_sql(engine.conn)
    committed = statements.index("COMMIT")
    marked = [i for i, sql in enumerate(statements) if "marked_by_policy" in sql]
    assert "REPEATABLE READ, READ ONLY" in statements[1]
    assert len(marked) == 2
    assert min(marked) > committed


def test_a_kept_row_hangs_off_kept_inquiries_and_a_kept_change_by_its_id() -> None:
    kept = _Kept(
        inquiries=frozenset({UUID(int=1), UUID(int=2)}),
        changes=frozenset({UUID(int=9)}),
    )
    assert kept.inquiry(UUID(int=2))
    assert not kept.inquiry(UUID(int=3))
    assert kept.row("inquiries", {"id": UUID(int=1)})
    assert not kept.row("inquiries", {"id": UUID(int=3)})
    assert kept.row("edges", {"from_id": UUID(int=1), "to_id": UUID(int=2)})
    assert not kept.row("edges", {"from_id": UUID(int=1), "to_id": UUID(int=3)})
    assert not kept.row("edges", {"from_id": UUID(int=3), "to_id": UUID(int=1)})
    assert kept.row("session_records", {"session_id": UUID(int=2)})
    assert kept.row("change_log", {"id": UUID(int=9), "subject_id": UUID(int=3)})
    assert not kept.row("change_log", {"id": UUID(int=8), "subject_id": UUID(int=1)})
    everything = _Kept()
    assert everything.inquiry(UUID(int=5))
    assert everything.row("edges", {"from_id": UUID(int=5), "to_id": UUID(int=6)})
    assert everything.row("change_log", {"id": UUID(int=7)})


@dataclass(frozen=True, slots=True, kw_only=True)
class _Marked(DefaultPolicy):
    """A policy whose read filter names a marker, so a test sees where it is asked."""

    @override
    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        del kind, params, principal
        return f"{alias}.marked_by_policy"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
