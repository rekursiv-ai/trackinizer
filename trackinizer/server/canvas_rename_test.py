"""The canvas cutover ships an upgrade and an operator-only rollback.

Each test is a unit of one step, run on the session's build inside a transaction
rolled back after it (``pglite_rollback_conn``, ``integ_rollback_conn``), on PGlite and
on Postgres. One test shows the rollback, run on the build with the DDL that came
after the cutover taken away, is the independent pre-cutover baseline
(``testdata/schema_before_canvas_rename.sql``) by its complete catalog; that test
alone loads the frozen schema. The others start from that rollback and the old rows.

Coverage of the round trip and collisions this file once ran end to end, per engine:

* an upgraded frozen database has a fresh install's catalog: the rollback is the
  frozen schema (``test_the_rollback_is_the_frozen_schema_*``) and the cutover takes
  it to the build's catalog (``test_the_cutover_upgrades_the_old_rows_*``);
* every converted row, timestamp, revision, hash and label:
  ``test_the_cutover_upgrades_the_old_rows_*``;
* the rollback restores the old catalog and rows, and a second upgrade the new rows:
  ``test_the_rollback_restores_*``; a rollback keeps what the new writer wrote:
  ``test_the_rollback_keeps_what_the_new_writer_wrote_*``;
* a boot that finds the cutover recorded runs it no more: ``core_test``'s
  ``test_bootstrap_skips_baseline_for_existing_db``; a replayed operation answers from
  the receipt the cutover rewrote: the receipt's fields here, and ``canvas_store_test``'s
  ``test_the_provider_is_stored_and_a_replay_answers_with_it``;
* each collision refuses the cutover without partial writes or DDL:
  ``test_a_collision_refuses_the_cutover_*``; nothing records a refused migration:
  ``core_test``'s ``test_bootstrap_records_no_migration_whose_body_fails``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import uuid

from treekle import from_plain

import asyncpg
import pytest

from trackinizer.conftest import savepoint
from trackinizer.server.schema_gen import substitute_schema_placeholders
from trackinizer.server.sql import load_sql, schema_migrations
from trackinizer.server.visuals.canvas_store import state_from_row


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_CWD: Final = Path(__file__).resolve().parent
_CANVAS: Final = uuid.UUID(int=4)

_COLLISIONS: Final = (
    "UPDATE visual_workspaces SET station = station || '{\"workspace_id\":null}'",
    "UPDATE visual_workspaces SET station = jsonb_set(station, '{suggestion,workspace_id}', 'null')",
    "UPDATE users SET preferences = preferences || '{\"workspaces\":{}}'",
    "UPDATE visual_workspace_operations SET response = response || '{\"workspace_id\":null}'",
    "UPDATE visual_workspace_operations SET response = jsonb_set(response, '{suggestion,workspace_id}', 'null')",
    "UPDATE inquiries SET labels = labels || ARRAY['workspace'] WHERE kind = 'Issue'",
    "UPDATE inquiries SET labels = labels || ARRAY['canvas:00000000-0000-0000-0000-000000000004'] WHERE kind = 'AgentSession'",
)
"""Each owned target key or label, already present where the cutover would write it."""


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


async def _frozen_schema(conn: Conn) -> None:
    """Load the independent pre-cutover baseline over an emptied schema."""
    body = (_CWD / "testdata" / "schema_before_canvas_rename.sql").read_text()
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await conn.execute(substitute_schema_placeholders(body))
    await conn.execute(
        "CREATE TABLE applied_migrations (name TEXT PRIMARY KEY, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())",
    )


async def _undo_after_canvas_rename(conn: Conn) -> None:
    """Remove later DDL before comparing the complete pre-cutover catalog."""
    await conn.execute("DROP TABLE session_index_cursor")
    await conn.execute(
        "DROP TRIGGER session_records_stamp_change ON session_records; "
        "DROP FUNCTION session_records_stamp_change(); "
        "ALTER TABLE session_records DROP COLUMN changed_xid",
    )
    await conn.execute("ALTER TABLE gpu_jobs DROP COLUMN requested_node")
    await conn.execute("DROP TABLE gpu_allowance_resets")
    await conn.execute(
        "ALTER TABLE session_liveness DROP COLUMN machine, DROP COLUMN run, "
        "DROP COLUMN cli, DROP COLUMN cwd",
    )
    await conn.execute("ALTER TABLE api_keys DROP COLUMN machine_id")
    await conn.execute(
        "DELETE FROM applied_migrations "
        "WHERE name IN ('schema.060.sql', 'schema.061.sql', 'schema.062.sql', "
        "'schema.063.sql')",
    )


async def _old_database(conn: Conn) -> None:
    """Roll the build back to the pre-cutover schema, and give it the old rows."""
    await conn.execute(load_sql("canvas_rename_down"))
    await _undo_after_canvas_rename(conn)
    await conn.execute((_CWD / "testdata" / "canvas_rename_seed.sql").read_text())


async def _upgrade(conn: Conn) -> None:
    """Run what a boot of a pre-cutover database runs: the cutover, then later DDL."""
    for step in ("schema.058", "schema.060", "schema.061", "schema.062", "schema.063"):
        await conn.execute(load_sql(step))


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


async def _rollback_is_frozen(conn: Conn) -> None:
    """Show the rollback, later DDL aside, leaves exactly the frozen pre-cutover catalog."""
    await conn.execute(load_sql("canvas_rename_down"))
    await _undo_after_canvas_rename(conn)
    rolled = await _catalog(conn)
    await _frozen_schema(conn)
    assert rolled == await _catalog(conn)


async def _upgrade_old_rows(conn: Conn) -> None:
    """Show the cutover takes old rows and the old catalog to the build's."""
    fresh = await _catalog(conn)
    await _old_database(conn)
    before = await _snapshot(conn, old=True)
    await _upgrade(conn)
    migrated_catalog = await _catalog(conn)
    assert ("table", "gpu_allowance_resets", "") in migrated_catalog
    assert migrated_catalog == fresh
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
    assert from_plain(receipt["suggestion"], dict[str, object])["workspace_id"] == str(
        uuid.UUID(int=6),
    )
    for old, new in zip(before["inquiries"], after["inquiries"], strict=True):
        assert {key: value for key, value in old.items() if key != "labels"} == {
            key: value for key, value in new.items() if key != "labels"
        }
    assert after["canvases"][0]["created_at"] == before["canvases"][0]["created_at"]
    assert after["canvases"][0]["modified_at"] == before["canvases"][0]["modified_at"]
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


async def _rollback_restores(conn: Conn) -> None:
    """Roll the upgrade back to the old catalog and rows, then upgrade again."""
    await _old_database(conn)
    old_catalog = await _catalog(conn)
    before = await _snapshot(conn, old=True)
    await _upgrade(conn)
    after = await _snapshot(conn, old=False)
    await conn.execute(load_sql("canvas_rename_down"))
    await _undo_after_canvas_rename(conn)
    assert await _catalog(conn) == old_catalog
    assert await _snapshot(conn, old=True) == before
    await _upgrade(conn)
    assert await _snapshot(conn, old=False) == after


async def _rollback_keeps_new_rows(conn: Conn) -> None:
    """Keep, through a rollback and the upgrade after it, a canvas the new writer made."""
    await _old_database(conn)
    await _upgrade(conn)
    await conn.execute(
        "INSERT INTO visual_canvases (id, user_id, state, metadata) "
        'VALUES ($1, $2, \'{"visuals":[]}\', \'{"name":"new canvas",'
        '"workspace_id":"00000000-0000-0000-0000-000000000002"}\')',
        uuid.UUID(int=8),
        uuid.UUID(int=1),
    )
    written = await _snapshot(conn, old=False)
    await conn.execute(load_sql("canvas_rename_down"))
    await conn.execute(load_sql("schema.058"))
    assert await _snapshot(conn, old=False) == written


async def _collision_refuses(conn: Conn, collision: str) -> None:
    """Show an owned target refuses the cutover without partial writes or DDL."""
    await _old_database(conn)
    await conn.execute(collision)
    before = await _snapshot(conn, old=True)
    catalog = await _catalog(conn)
    with pytest.raises(asyncpg.RaiseError, match="Canvas cutover"):
        async with savepoint(conn):
            await conn.execute(load_sql("schema.058"))
    assert await _snapshot(conn, old=True) == before
    assert await _catalog(conn) == catalog


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_rollback_is_the_frozen_schema_pglite(
    pglite_rollback_conn: Conn,
) -> None:
    """On PGlite, the rollback of the build is the frozen pre-cutover schema."""
    await _rollback_is_frozen(pglite_rollback_conn)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_rollback_is_the_frozen_schema_pg(integ_rollback_conn: Conn) -> None:
    """On PostgreSQL, the rollback of the build is the frozen pre-cutover schema."""
    await _rollback_is_frozen(integ_rollback_conn)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_cutover_upgrades_the_old_rows_pglite(
    pglite_rollback_conn: Conn,
) -> None:
    """PGLite preserves old rows and reaches the complete named catalog."""
    await _upgrade_old_rows(pglite_rollback_conn)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_cutover_upgrades_the_old_rows_pg(integ_rollback_conn: Conn) -> None:
    """PostgreSQL preserves the same rows and reaches the same catalog."""
    await _upgrade_old_rows(integ_rollback_conn)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_rollback_restores_the_old_rows_pglite(
    pglite_rollback_conn: Conn,
) -> None:
    """PGLite rolls back old rows exactly and upgrades again."""
    await _rollback_restores(pglite_rollback_conn)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_rollback_restores_the_old_rows_pg(integ_rollback_conn: Conn) -> None:
    """PostgreSQL rolls back old rows exactly and upgrades again."""
    await _rollback_restores(integ_rollback_conn)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_rollback_keeps_what_the_new_writer_wrote_pglite(
    pglite_rollback_conn: Conn,
) -> None:
    """PGLite keeps a canvas made after the upgrade through a rollback."""
    await _rollback_keeps_new_rows(pglite_rollback_conn)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_the_rollback_keeps_what_the_new_writer_wrote_pg(
    integ_rollback_conn: Conn,
) -> None:
    """PostgreSQL keeps a canvas made after the upgrade through a rollback."""
    await _rollback_keeps_new_rows(integ_rollback_conn)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("collision", _COLLISIONS)
async def test_a_collision_refuses_the_cutover_pglite(
    pglite_rollback_conn: Conn,
    collision: str,
) -> None:
    """On PGlite, an owned target refuses the cutover without partial writes or DDL."""
    await _collision_refuses(pglite_rollback_conn, collision)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("collision", _COLLISIONS)
async def test_a_collision_refuses_the_cutover_pg(
    integ_rollback_conn: Conn,
    collision: str,
) -> None:
    """On PostgreSQL, an owned target refuses the cutover without writes or DDL."""
    await _collision_refuses(integ_rollback_conn, collision)


def test_canvas_cutover_ships_up_and_keeps_down_out_of_bootstrap() -> None:
    """An existing database upgrades once; startup never runs the rollback."""
    names = {name for name, _ in schema_migrations()}
    assert "schema.058.sql" in names
    assert "canvas_rename_down.sql" not in names
    assert (_CWD / "assets" / "canvas_rename_down.sql").is_file()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
