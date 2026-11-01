"""The canvas and Chat migrations match fresh bootstrap, and Chat is on by default.

Each runs its migrations on the session's build inside a transaction rolled back after
it (``pglite_rollback_conn``), so the next test meets the build untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain

import pytest

from trackinizer.server.sql import load_sql


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_migration_matches_fresh_schema(
    pglite_rollback_conn: Conn,
) -> None:
    """Migrated and fresh databases expose the same columns and indexes."""
    conn = pglite_rollback_conn
    tables = ["visual_canvases", "visual_canvas_operations"]
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1) ORDER BY table_name, ordinal_position"
    )
    indexes = (
        "SELECT tablename, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY($1) "
        "ORDER BY tablename, indexdef"
    )
    preference_column = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'users' "
        "AND column_name = 'visual_canvas_enabled'"
    )
    fresh_columns = [dict(row) for row in await conn.fetch(columns, tables)]
    fresh_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
    fresh_preference = await conn.fetchrow(preference_column)
    assert fresh_columns
    assert fresh_indexes
    assert fresh_preference is not None
    await conn.execute("DROP TABLE visual_canvas_operations")
    await conn.execute("DROP TABLE visual_canvases")
    await conn.execute("ALTER TABLE users DROP COLUMN visual_canvas_enabled")
    await conn.execute(load_sql("schema.026"))
    await conn.execute(load_sql("schema.027"))
    await conn.execute(load_sql("schema.033"))
    await conn.execute(load_sql("schema.042"))
    await conn.execute(load_sql("schema.058"))
    migrated_columns = [dict(row) for row in await conn.fetch(columns, tables)]
    migrated_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
    migrated_preference = await conn.fetchrow(preference_column)
    assert migrated_columns == fresh_columns
    assert migrated_indexes == fresh_indexes
    assert migrated_preference is not None
    assert dict(migrated_preference) == dict(fresh_preference)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_chat_store_is_gone_from_a_fresh_schema_and_a_migrated_one(
    pglite_rollback_conn: Conn,
) -> None:
    """A fresh database has no chat tables; 037 drops them, with their rows, from an old one."""
    conn = pglite_rollback_conn
    tables = "SELECT tablename FROM pg_tables WHERE tablename LIKE 'chat\\_%'"
    assert await conn.fetch(tables) == []
    # A database that ran 033 holds the store, and a conversation in it.
    await conn.execute(load_sql("canvas_rename_down"))
    await conn.execute(load_sql("schema.033"))
    await conn.execute(load_sql("schema.058"))
    assert {row["tablename"] for row in await conn.fetch(tables)} == {
        "chat_conversations",
        "chat_messages",
    }
    user, canvas = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES ($1, 'u@example.com', 'U', 'writer', 'active')",
        user,
    )
    await conn.execute(
        "INSERT INTO visual_canvases (id, user_id, metadata, state) "
        'VALUES ($1, $2, \'{"name": "unnamed-canvas#1"}\', $3)',
        canvas,
        user,
        {"visuals": []},
    )
    await conn.execute(
        "INSERT INTO chat_conversations (id, user_id, workspace_id, title) "
        "VALUES (gen_random_uuid(), $1, $2, 't')",
        user,
        canvas,
    )
    await conn.execute(load_sql("schema.037"))
    assert await conn.fetch(tables) == []
    # Replaying it is a no-op, not an error.
    await conn.execute(load_sql("schema.037"))
    assert await conn.fetch(tables) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_canvas_is_on_by_default_and_the_migration_turns_it_on(
    pglite_rollback_conn: Conn,
) -> None:
    """A new user has the canvas; migrating turns it on for existing users."""
    conn = pglite_rollback_conn
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES (gen_random_uuid(), 'new@example.com', 'New', 'writer', 'active')",
    )
    assert await conn.fetchval(
        "SELECT visual_canvas_enabled FROM users WHERE email = 'new@example.com'",
    )
    await conn.execute(
        "ALTER TABLE users ALTER COLUMN visual_canvas_enabled SET DEFAULT FALSE",
    )
    await conn.execute(
        "UPDATE users SET visual_canvas_enabled = FALSE",
    )
    await conn.execute(load_sql("canvas_rename_down"))
    await conn.execute(load_sql("schema.033"))
    await conn.execute(load_sql("schema.058"))
    assert await conn.fetchval(
        "SELECT visual_canvas_enabled FROM users WHERE email = 'new@example.com'",
    )
    assert (
        await conn.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'visual_canvas_enabled'",
        )
        == "true"
    )


def _visual(kind: str, *, placement: str = "main") -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "type": kind,
        "version": 1,
        "placement": placement,
        "record_id": None,
        "params": {},
        "floating_rect": None,
    }


async def _canvases_before_chat(conn: Conn) -> dict[str, uuid.UUID]:
    """Make an old canvas, one with Chat already, and a full one."""
    user = uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES ($1, 'old@example.com', 'O', 'writer', 'active')",
        user,
    )
    ids = {"old": uuid.uuid4(), "has_chat": uuid.uuid4(), "full": uuid.uuid4()}
    states = {
        "old": [_visual("trax.browse")],
        "has_chat": [_visual("trax.browse"), _visual("trax.chat", placement="side")],
        "full": [_visual("trax.timeline") for _ in range(12)],
    }
    for name, canvas in ids.items():
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, revision, state) "
            "VALUES ($1, $2, jsonb_build_object('name', $3::text), 5, $4)",
            canvas,
            user,
            name,
            {"visuals": states[name]},
        )
    return ids


async def _chat_state(
    conn: Conn,
    *,
    canvas: uuid.UUID,
) -> tuple[int, list[str], str]:
    row = await conn.fetchrow(
        "SELECT revision, state FROM visual_canvases WHERE id = $1",
        canvas,
    )
    assert row is not None
    # The stored JSON as the migration left it: reading it as a canvas drops Chat.
    visuals = from_plain(
        from_plain(row["state"], dict[str, object])["visuals"],
        list[dict[str, object]],
    )
    chats = [
        from_plain(v["placement"], str) for v in visuals if v["type"] == "trax.chat"
    ]
    return (
        from_plain(row["revision"], int),
        [from_plain(v["type"], str) for v in visuals],
        ",".join(chats),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_migration_gives_an_old_canvas_chat_once(
    pglite_rollback_conn: Conn,
) -> None:
    """An old canvas gets Chat floating and a new revision; running again adds none."""
    conn = pglite_rollback_conn
    ids = await _canvases_before_chat(conn)
    await conn.execute(load_sql("canvas_rename_down"))
    await conn.execute(load_sql("schema.033"))
    await conn.execute(load_sql("schema.058"))
    revision, types, chat = await _chat_state(conn, canvas=ids["old"])
    assert (revision, types, chat) == (6, ["trax.browse", "trax.chat"], "floating")
    assert await _chat_state(conn, canvas=ids["has_chat"]) == (
        5,
        ["trax.browse", "trax.chat"],
        "side",
    )
    full_revision, full_types, full_chat = await _chat_state(
        conn,
        canvas=ids["full"],
    )
    assert (full_revision, len(full_types), full_chat) == (5, 12, "")
    await conn.execute(load_sql("canvas_rename_down"))
    await conn.execute(load_sql("schema.033"))
    await conn.execute(load_sql("schema.058"))
    assert await _chat_state(conn, canvas=ids["old"]) == (6, types, "floating")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
