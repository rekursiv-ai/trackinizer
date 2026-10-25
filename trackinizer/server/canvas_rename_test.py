"""The canvas cutover ships an upgrade and an operator-only rollback."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import uuid

from treekle import from_plain

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.notify import NOTIFY_CHANNEL, tx
from trackinizer.server.schema_gen import substitute_schema_placeholders
from trackinizer.server.sql import load_sql, schema_migrations
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.canvas_store import (
    apply_canvas_operation,
    state_from_row,
)
from trackinizer.server.visuals.canvases import (
    ApplyCanvasOperation,
    ChooseProvider,
)
from trackinizer.server.visuals.catalog import default_catalog


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import Conn, DatabaseEngine


_CWD: Final = Path(__file__).resolve().parent
_USER: Final = uuid.UUID(int=1)
_CANVAS: Final = uuid.UUID(int=4)


@pytest_asyncio.fixture(loop_scope="session")
async def rename_pg(pg_dsn: str) -> AsyncIterator[postgres.PostgresEngine]:
    """Own a sibling database; never alter the shared integration database."""
    base = pg_dsn.rsplit("/", 1)[0]
    name = f"canvas_rename_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        async with postgres.PostgresEngine(
            dsn=f"{base}/{name}",
            listen_channel=NOTIFY_CHANNEL,
        ) as engine:
            async with engine.acquire() as conn:
                await conn.execute("CREATE EXTENSION vector")
            yield engine
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


async def _catalog(conn: Conn) -> set[tuple[str, str, str]]:
    """Compare names and definitions, including every sequence property."""
    sql = (_CWD / "testdata" / "canvas_rename_catalog.sql").read_text()
    return {
        (
            from_plain(row["kind"], str),
            from_plain(row["name"], str),
            from_plain(row["definition"], str),
        )
        for row in await conn.fetch(sql)
    }


async def _old_database(engine: DatabaseEngine) -> None:
    """Load the independent pre-cutover baseline and mark only its migrations."""
    body = (_CWD / "testdata" / "schema_before_canvas_rename.sql").read_text()
    async with engine.acquire() as conn:
        await conn.execute(substitute_schema_placeholders(body))
        await conn.execute(
            "CREATE TABLE applied_migrations (name TEXT PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())",
        )
        # Bootstrap must apply the cutover and the later DDL absent from this
        # frozen baseline; marking that DDL applied hides upgrade/fresh drift.
        for name, _ in schema_migrations():
            if name not in {"schema.058.sql", "schema.060.sql", "schema.061.sql"}:
                await conn.execute(
                    "INSERT INTO applied_migrations (name) VALUES ($1)",
                    name,
                )
        await conn.execute((_CWD / "testdata" / "canvas_rename_seed.sql").read_text())


async def _undo_after_canvas_rename(conn: Conn) -> None:
    """Remove later DDL before comparing the complete pre-cutover catalog."""
    await conn.execute("DROP TABLE gpu_allowance_resets")
    await conn.execute(
        "ALTER TABLE session_liveness DROP COLUMN machine, DROP COLUMN run, "
        "DROP COLUMN cli, DROP COLUMN cwd",
    )
    await conn.execute("ALTER TABLE api_keys DROP COLUMN machine_id")
    await conn.execute(
        "DELETE FROM applied_migrations "
        "WHERE name IN ('schema.060.sql', 'schema.061.sql')",
    )


async def _snapshot(conn: Conn, *, old: bool) -> dict[str, list[dict[str, object]]]:
    """Read all seeded rows, normalizing column names but never their values."""
    canvas_sql = (
        "SELECT id, user_id, revision, state, station AS metadata, created_at, modified_at "
        "FROM visual_workspaces ORDER BY id"
        if old
        else "SELECT id, user_id, revision, state, metadata, created_at, modified_at "
        "FROM visual_canvases ORDER BY id"
    )
    receipts_sql = (
        "SELECT workspace_id AS canvas_id, key, request_hash, response, created_at "
        "FROM visual_workspace_operations ORDER BY key"
        if old
        else "SELECT canvas_id, key, request_hash, response, created_at "
        "FROM visual_canvas_operations ORDER BY key"
    )
    return {
        name: [dict(row) for row in await conn.fetch(sql)]
        for name, sql in (
            ("canvases", canvas_sql),
            ("receipts", receipts_sql),
            ("users", "SELECT id, preferences FROM users ORDER BY id"),
            (
                "inquiries",
                "SELECT id, title, app, labels, created, modified FROM inquiries ORDER BY id",
            ),
            ("edges", "SELECT * FROM edges ORDER BY from_id, to_id"),
        )
    }


async def _round_trip(engine: DatabaseEngine) -> None:
    """Upgrade actual old rows; rollback post-up writes; upgrade and bootstrap twice."""
    await _old_database(engine)
    async with engine.acquire() as conn:
        old_catalog = await _catalog(conn)
        before = await _snapshot(conn, old=True)
    store = Store(engine, embed=StubEmbedder())
    await store.bootstrap()
    async with engine.acquire() as conn:
        migrated_catalog = await _catalog(conn)
        assert ("table", "gpu_allowance_resets", "") in migrated_catalog
        after = await _snapshot(conn, old=False)
        metadata = from_plain(after["canvases"][0]["metadata"], dict[str, object])
        state = state_from_row(after["canvases"][0])
        assert state.workspace_id == uuid.UUID(int=2)
        assert state.conversation_id == uuid.UUID(int=5)
        assert state.session_id == uuid.UUID(int=3)
        assert state.layout is not None
        assert state.layout.chat_ch == 42
        assert state.marks_seq == 3
        assert state.provider == "claude"
        assert metadata["workspace_id"] == str(uuid.UUID(int=2))
        assert "workstream_id" not in metadata
        suggestion = from_plain(metadata["suggestion"], dict[str, object])
        assert suggestion["workspace_id"] == str(uuid.UUID(int=6))
        assert after["edges"] == before["edges"]
        preferences = from_plain(after["users"][0]["preferences"], dict[str, object])
        old_preferences = from_plain(
            before["users"][0]["preferences"],
            dict[str, object],
        )
        assert preferences == {
            "theme": "dark",
            "workspaces": old_preferences["workstreams"],
        }
        receipt = from_plain(after["receipts"][0]["response"], dict[str, object])
        assert receipt["workspace_id"] == str(uuid.UUID(int=2))
        assert from_plain(receipt["suggestion"], dict[str, object])[
            "workspace_id"
        ] == str(uuid.UUID(int=6))
        for old, new in zip(before["inquiries"], after["inquiries"], strict=True):
            assert {key: value for key, value in old.items() if key != "labels"} == {
                key: value for key, value in new.items() if key != "labels"
            }
        assert after["canvases"][0]["created_at"] == before["canvases"][0]["created_at"]
        assert (
            after["canvases"][0]["modified_at"] == before["canvases"][0]["modified_at"]
        )
        assert after["canvases"][0]["revision"] == 19
        assert metadata["name"] == "unnamed-workstream#1"
        assert (
            after["receipts"][0]["request_hash"]
            == "694bea63d8f323ca1443d9135181fb1d7e4d056709a4f50e308aeb68c7671e9d"
        )
        assert after["inquiries"][0]["labels"] == ["workspace", "scratch"]
        assert after["inquiries"][1]["labels"] == [
            f"canvas:{_CANVAS}",
            "git:https://example.com/repo@main",
        ]
        async with tx(conn):
            await conn.execute(load_sql("canvas_rename_down"))
            await _undo_after_canvas_rename(conn)
        assert await _catalog(conn) == old_catalog
        assert await _snapshot(conn, old=True) == before
    await store.bootstrap()
    await store.bootstrap()
    replay = await apply_canvas_operation(
        store,
        user_id=_USER,
        canvas_id=_CANVAS,
        key=uuid.UUID(int=7),
        body=ApplyCanvasOperation(
            revision=17,
            operation=ChooseProvider(kind="provider", provider="claude"),
        ),
        catalog=default_catalog(),
        inbound=InboundQueue(),
        assistant=None,
    )
    assert replay is not None
    assert replay.replayed
    assert replay.state.revision == 18
    assert replay.state.workspace_id == uuid.UUID(int=2)
    async with engine.acquire() as conn:
        assert await _snapshot(conn, old=False) == after
        # Exercise rollback of a canvas created by the new writer too.
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) "
            'VALUES ($1, $2, \'{"visuals":[]}\', \'{"name":"new canvas",'
            '"workspace_id":"00000000-0000-0000-0000-000000000002"}\')',
            uuid.UUID(int=8),
            _USER,
        )
        written = await _snapshot(conn, old=False)
        async with tx(conn):
            await conn.execute(load_sql("canvas_rename_down"))
    await store.bootstrap()
    async with engine.acquire() as conn:
        assert await _snapshot(conn, old=False) == written
        # A separate fresh schema in the same engine avoids comparing PG to PGLite.
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await store.bootstrap()
    async with engine.acquire() as conn:
        assert await _catalog(conn) == migrated_catalog


async def _collisions(engine: DatabaseEngine) -> None:
    """Every owned target key/label refuses cutover without partial writes or DDL."""
    for sql in (
        "UPDATE visual_workspaces SET station = station || '{\"workspace_id\":null}'",
        "UPDATE visual_workspaces SET station = jsonb_set(station, '{suggestion,workspace_id}', 'null')",
        "UPDATE users SET preferences = preferences || '{\"workspaces\":{}}'",
        "UPDATE visual_workspace_operations SET response = response || '{\"workspace_id\":null}'",
        "UPDATE visual_workspace_operations SET response = jsonb_set(response, '{suggestion,workspace_id}', 'null')",
        "UPDATE inquiries SET labels = labels || ARRAY['workspace'] WHERE kind = 'Issue'",
        "UPDATE inquiries SET labels = labels || ARRAY['canvas:00000000-0000-0000-0000-000000000004'] WHERE kind = 'AgentSession'",
    ):
        async with engine.acquire() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _old_database(engine)
        async with engine.acquire() as conn:
            await conn.execute(sql)
            before = await _snapshot(conn, old=True)
            catalog = await _catalog(conn)
        with pytest.raises(asyncpg.RaiseError, match="Canvas cutover"):
            await Store(engine, embed=StubEmbedder()).bootstrap()
        async with engine.acquire() as conn:
            assert await _snapshot(conn, old=True) == before
            assert await _catalog(conn) == catalog
            assert (
                await conn.fetchval(
                    "SELECT 1 FROM applied_migrations WHERE name = 'schema.058.sql'",
                )
                is None
            )
    async with engine.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_cutover_round_trip_pglite(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """PGLite preserves old rows, post-up writes, and the complete named catalog."""
    await reset_schema(pglite_engine)
    await _collisions(pglite_engine)
    await _round_trip(pglite_engine)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_cutover_round_trip_pg(rename_pg: postgres.PostgresEngine) -> None:
    """PostgreSQL preserves the same rows and catalog under bootstrap and rollback."""
    await _collisions(rename_pg)
    await _round_trip(rename_pg)


def test_canvas_cutover_ships_up_and_keeps_down_out_of_bootstrap() -> None:
    """An existing database upgrades once; startup never runs the rollback."""
    names = {name for name, _ in schema_migrations()}
    assert "schema.058.sql" in names
    assert "canvas_rename_down.sql" not in names
    assert (_CWD / "assets" / "canvas_rename_down.sql").is_file()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
