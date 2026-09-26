"""The logical export reads the whole graph, all of it, and the same way twice.

Against a real PGlite engine rather than the mock connection: what the export
promises is about the catalog, the column types asyncpg hands back, and the
order Postgres returns rows in, none of which a mock can stand in for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.lib.custom_json import DictCodec, ListCodec, loads
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.api.export_routes import export_lines
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.store.export import _SCOPE_COLUMNS
from trackinizer.server.store.session_ir import SlashCommandRow
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stdout
from trackinizer.wire.bodies import SubmitExperiment, SubmitIssue
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_export import (
    EXPORT_FORMAT,
    EXPORT_TABLES,
    EXPORT_VERSION,
)
from trackinizer.wire.wire_metrics import MetricPoint


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine
    from trackinizer.server.store.export import GraphExport


_LEFT_OUT: Final = frozenset(
    {
        "allowlist",
        "api_keys",
        "applied_migrations",
        "inquiry_embeddings",
        "session_bodies",
        "session_ciphertext",
        "session_embeddings",
        "session_index_state",
        "users",
    },
)
"""Tables the export omits on purpose; ``wire_export.EXPORT_TABLES`` says why."""


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _seed(store: Store) -> dict[str, UUID]:
    """Write at least one row into every exported table; return the ids."""
    broad = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="Broad question"),
    )
    narrow = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="Narrow question"),
    )
    await store.add_edge(
        from_id=narrow,
        to_id=broad,
        edge_kind="narrows",
        actor="tester",
    )
    run = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="Loss sweep"),
    )
    await store.log_metrics(
        run,
        [
            MetricPoint(key="loss", step=1, value=0.5),
            MetricPoint(key="loss", step=0, value=1.5),
        ],
    )
    session = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'export test')",
            session,
        )
    part = await store.upsert_session_manifest(
        session,
        name="session.jsonl",
        metadata={"encoding": "utf-8"},
        ir_id=uuid4(),
        format="claude",
        records=1,
    )
    await store.append_session_records(
        session,
        [
            SessionRecordRow.of(
                session_id=session,
                part=part,
                idx=0,
                record=Stdout(text="hello\n"),
            ),
        ],
        slash_commands=[
            SlashCommandRow(
                timestamp=datetime(2026, 9, 19, tzinfo=UTC),
                command="compact",
            ),
        ],
    )
    return {"broad": broad, "narrow": narrow, "run": run, "session": session}


def _rows(graph: GraphExport, table: str) -> tuple[dict[str, object], ...]:
    """Return the exported rows of one table."""
    return dict(graph.tables)[table]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_every_table_is_exported_or_deliberately_left_out(store: Store) -> None:
    """A new table fails here until someone decides whether it is exported."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
        )
    tables = {cast(str, row["table_name"]) for row in rows}

    assert tables == set(EXPORT_TABLES) | _LEFT_OUT


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_export_carries_every_seeded_row(store: Store) -> None:
    """Rows, edges, audit, metrics and session parts all come out, in order."""
    ids = await _seed(store)

    graph = await store.export_graph()

    assert [table for table, _ in graph.tables] == list(EXPORT_TABLES)
    inquiry_ids = {row["id"] for row in _rows(graph, "inquiries")}
    assert set(ids.values()) <= inquiry_ids
    assert {
        (row["from_id"], row["edge_kind"], row["to_id"])
        for row in _rows(graph, "edges")
    } >= {(ids["narrow"], "narrows", ids["broad"])}
    # Provenance: every submit wrote a ``change_log`` row, and a change's id is
    # the idempotency key its client sent.
    audited = {row["subject_id"] for row in _rows(graph, "change_log")}
    assert {ids["broad"], ids["narrow"], ids["run"]} <= audited
    assert [row["step"] for row in _rows(graph, "experiment_metrics")] == [0, 1]
    # ``json`` columns arrive decoded, not as the text asyncpg returns for them.
    (manifest,) = _rows(graph, "session_manifests")
    assert manifest["metadata"] == {"encoding": "utf-8"}
    (record,) = _rows(graph, "session_records")
    assert isinstance(record["payload"], dict)
    assert "search" not in record
    (command,) = _rows(graph, "session_slash_commands")
    assert command["command"] == "compact"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_header_names_the_applied_migrations(store: Store) -> None:
    """A reader learns which schema wrote the rows from the first line alone."""
    graph = await store.export_graph()
    async with store.engine.acquire() as conn:
        applied = await conn.fetch("SELECT name FROM applied_migrations")

    header = DictCodec.coerce(loads(next(iter(export_lines(graph)))))

    assert header["format"] == EXPORT_FORMAT
    assert header["version"] == EXPORT_VERSION
    migrations = ListCodec.coerce(header["migrations"])
    assert migrations[0] == "schema.sql"
    assert sorted(map(str, migrations)) == sorted(
        cast(str, row["name"]) for row in applied
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_every_line_is_json_and_an_unchanged_graph_exports_identically(
    store: Store,
) -> None:
    """Real column types serialize, and two exports of one graph match byte for byte."""
    await _seed(store)

    first = b"".join(export_lines(await store.export_graph()))
    second = b"".join(export_lines(await store.export_graph()))

    assert first == second
    lines = first.decode().splitlines()
    assert all(isinstance(loads(line), dict) for line in lines)
    assert len(lines) > 1 + len(EXPORT_TABLES)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rows_written_later_land_at_the_end(store: Store) -> None:
    """A re-export appends new inquiries instead of reshuffling the old ones."""
    await _seed(store)
    before = _rows(await store.export_graph(), "inquiries")

    await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="Asked later"),
    )
    after = _rows(await store.export_graph(), "inquiries")

    assert after[: len(before)] == before
    assert after[-1]["title"] == "Asked later"


async def _seed_partitioned(store: Store) -> dict[str, UUID]:
    """Two orgs plus a device-scoped row, the partition the request describes."""
    ours = await store.submit_issue(
        SubmitIssue(
            account="tester@example.com",
            title="Ours",
            labels=["org:rekursiv"],
        ),
    )
    ours_child = await store.submit_issue(
        SubmitIssue(
            account="tester@example.com",
            title="Ours, narrower",
            labels=["org:rekursiv"],
        ),
    )
    theirs = await store.submit_issue(
        SubmitIssue(
            account="tester@example.com",
            title="Theirs",
            labels=["org:other"],
        ),
    )
    device = await store.submit_issue(
        SubmitIssue(
            account="tester@example.com",
            title="Ours, device scoped",
            labels=["org:rekursiv", "machine:laptop-1"],
        ),
    )
    for child in (ours_child, device):
        await store.add_edge(
            from_id=child,
            to_id=ours,
            edge_kind="narrows",
            actor="tester",
        )
    return {
        "ours": ours,
        "ours_child": ours_child,
        "theirs": theirs,
        "device": device,
    }


def test_every_exported_table_says_how_a_selector_reaches_it() -> None:
    """A new exported table fails here until it declares its scope column."""
    assert set(_SCOPE_COLUMNS) == set(EXPORT_TABLES)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_selector_exports_only_the_inquiries_it_matches(store: Store) -> None:
    """The subgraph holds the selected rows and nothing from the other org."""
    ids = await _seed_partitioned(store)

    graph = await store.export_graph(
        selector=(Filter(field="labels", op="is", value="org:rekursiv"),),
    )

    assert {row["id"] for row in _rows(graph, "inquiries")} == {
        ids["ours"],
        ids["ours_child"],
        ids["device"],
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clauses_and_together_the_way_the_request_spells_it(
    store: Store,
) -> None:
    """``org:rekursiv AND NOT machine:*`` is two clauses, and drops the device row."""
    ids = await _seed_partitioned(store)

    graph = await store.export_graph(
        selector=(
            Filter(field="labels", op="is", value="org:rekursiv"),
            Filter(field="labels", op="nre", value="^machine:"),
        ),
    )

    assert {row["id"] for row in _rows(graph, "inquiries")} == {
        ids["ours"],
        ids["ours_child"],
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_edge_rides_along_only_when_both_ends_do(store: Store) -> None:
    """A subgraph never carries an edge naming a row the reader will not receive."""
    ids = await _seed_partitioned(store)

    graph = await store.export_graph(
        selector=(
            Filter(field="labels", op="is", value="org:rekursiv"),
            Filter(field="labels", op="nre", value="^machine:"),
        ),
    )

    exported = {row["id"] for row in _rows(graph, "inquiries")}
    edges = {(row["from_id"], row["to_id"]) for row in _rows(graph, "edges")}
    assert (ids["ours_child"], ids["ours"]) in edges
    # The device row's edge points at a selected parent, but the device end was
    # excluded, so the edge goes with it.
    assert (ids["device"], ids["ours"]) not in edges
    assert all({source, target} <= exported for source, target in edges)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_selector_reaches_the_tables_hanging_off_the_inquiries(
    store: Store,
) -> None:
    """Audit and the per-kind detail tables are scoped too, not exported whole."""
    await _seed(store)
    ids = await _seed_partitioned(store)

    graph = await store.export_graph(
        selector=(Filter(field="labels", op="is", value="org:rekursiv"),),
    )

    selected = {ids["ours"], ids["ours_child"], ids["device"]}
    assert {row["subject_id"] for row in _rows(graph, "change_log")} <= selected
    # ``_seed`` wrote metrics and session rows under unlabelled inquiries, so
    # every one of them is out of scope here.
    assert _rows(graph, "experiment_metrics") == ()
    assert _rows(graph, "session_manifests") == ()
    assert _rows(graph, "session_records") == ()
    assert _rows(graph, "session_slash_commands") == ()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_header_says_which_subgraph_this_is(store: Store) -> None:
    """A reader can tell a selector's slice from a backup, and read the selector."""
    await _seed_partitioned(store)
    selector = (Filter(field="labels", op="is", value="org:rekursiv"),)

    scoped = await store.export_graph(selector=selector)
    whole = await store.export_graph()

    header = DictCodec.coerce(loads(next(iter(export_lines(scoped)))))
    assert header["selector"] == [
        {"field": "labels", "op": "is", "value": "org:rekursiv"},
    ]
    # Absent, not empty: a whole-graph export is byte-identical to the ones
    # written before selectors existed.
    assert "selector" not in DictCodec.coerce(loads(next(iter(export_lines(whole)))))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_empty_selector_is_the_whole_graph(store: Store) -> None:
    """The unfiltered export is untouched, byte for byte, by this feature."""
    await _seed_partitioned(store)

    explicit = b"".join(export_lines(await store.export_graph(selector=())))
    default = b"".join(export_lines(await store.export_graph()))

    assert explicit == default


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_selector_matching_nothing_exports_a_header_and_no_rows(
    store: Store,
) -> None:
    """An empty subgraph is a valid export, not an error and not everything."""
    await _seed_partitioned(store)

    graph = await store.export_graph(
        selector=(Filter(field="labels", op="is", value="org:nobody"),),
    )

    assert all(rows == () for _, rows in graph.tables)
    assert len(b"".join(export_lines(graph)).decode().splitlines()) == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
