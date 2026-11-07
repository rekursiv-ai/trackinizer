"""Archive one model's embeddings to files and restore them, against pglite."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal, Self, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import sys

import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.lib.postgres.testing import postgres_database
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.core import Store
from trackinizer.server.tools import archive_embeddings


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from trackinizer.lib.postgres import Conn


_OLD = "qwen3-embedding-4b@1024"
_NEW = "qwen3-embedding-0.6b@1024"


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped."""
    return integ_store


async def _session_with_vectors(store: Store, *, chunks: int) -> UUID:
    """One session holding ``chunks`` vectors and a marker for each model."""
    session_id = uuid4()
    vector = "[" + ",".join("0.25" for _ in range(1024)) + "]"
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) VALUES ($1, 'AgentSession', "
            "nextval('seq_agentsession'), 'active', 'tester@example.com', 'archive', "
            "'open')",
            session_id,
        )
        for model in (_OLD, _NEW):
            for chunk in range(chunks):
                await conn.execute(
                    "INSERT INTO session_embeddings (session_id, part, idx, field, "
                    "chunk, mapper, model, embedding, text_md5) VALUES ($1, 0, 0, "
                    "'content', $2, 'footprint-v1', $3, $4::halfvec, 'md5')",
                    session_id,
                    chunk,
                    model,
                    vector,
                )
            await conn.execute(
                "INSERT INTO session_index_state (session_id, part, idx, mapper, "
                "model, text_md5) VALUES ($1, 0, 0, 'footprint-v1', $2, 'md5')",
                session_id,
                model,
            )
            await conn.execute(
                "INSERT INTO session_index_cursor (mapper, model, snapshot) "
                "VALUES ('footprint-v1', $1, pg_current_snapshot()) "
                "ON CONFLICT DO NOTHING",
                model,
            )
    return session_id


async def _rows(store: Store, model: str) -> dict[str, set[tuple[object, ...]]]:
    """Every row ``model`` holds, as text, per table (the cursor as a count)."""
    async with store.engine.acquire() as conn:
        vectors = await conn.fetch(
            "SELECT session_id, chunk, embedding::text AS e, created "
            "FROM session_embeddings WHERE model = $1",
            model,
        )
        markers = await conn.fetch(
            "SELECT session_id, text_md5, created FROM session_index_state "
            "WHERE model = $1",
            model,
        )
        cursors = await conn.fetchval(
            "SELECT count(*) FROM session_index_cursor WHERE model = $1",
            model,
        )
    return {
        "session_embeddings": {tuple(row.values()) for row in vectors},
        "session_index_state": {tuple(row.values()) for row in markers},
        "session_index_cursor": {(cursors,)},
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_archive_moves_one_model_out_and_restore_brings_it_back(
    store: Store,
    pg_dsn: str,
    tmp_path: Path,
) -> None:
    """The archived model leaves the database whole; the other stays untouched."""
    await _session_with_vectors(store, chunks=2)
    before = await _rows(store, _OLD)
    kept = await _rows(store, _NEW)
    out = tmp_path / "nested" / "embeddings-4b"

    archived = await archive_embeddings.archive(pg_dsn, model=_OLD, out=out)

    assert (out / "model.txt").read_text() == _OLD + "\n"
    assert archived == {"session_embeddings": 2, "session_index_state": 1}
    assert await _rows(store, _OLD) == {
        "session_embeddings": set(),
        "session_index_state": set(),
        "session_index_cursor": {(0,)},
    }
    assert await _rows(store, _NEW) == kept

    restored = await archive_embeddings.restore(pg_dsn, source=out)

    assert restored == archived
    # Everything but the cursor returns: the next sweep re-reads the pending rows.
    assert await _rows(store, _OLD) == {**before, "session_index_cursor": {(0,)}}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_restore_skips_deleted_sessions_and_rows_already_present(
    store: Store,
    pg_dsn: str,
    tmp_path: Path,
) -> None:
    gone = await _session_with_vectors(store, chunks=1)
    await _session_with_vectors(store, chunks=1)
    out = tmp_path / "embeddings-4b"
    await archive_embeddings.archive(pg_dsn, model=_OLD, out=out)
    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM inquiries WHERE id = $1", gone)

    first = await archive_embeddings.restore(pg_dsn, source=out)
    again = await archive_embeddings.restore(pg_dsn, source=out)

    assert first == {"session_embeddings": 1, "session_index_state": 1}
    assert again == {"session_embeddings": 0, "session_index_state": 0}


@pytest_asyncio.fixture(loop_scope="session")
async def schema_growth_store(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[tuple[str, Store]]:
    """Isolate catalog changes from the shared engine's cached pgvector types."""
    with postgres_database(request, dbname="archive_schema_growth") as dsn:
        async with PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine:
            store = Store(engine, embed=StubEmbedder())
            await store.bootstrap()
            yield dsn, store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_restore_uses_defaults_for_columns_added_after_archive(
    schema_growth_store: tuple[str, Store],
    tmp_path: Path,
) -> None:
    """An older binary archive restores after either table gains a default column."""
    pg_dsn, store = schema_growth_store
    await _session_with_vectors(store, chunks=2)
    before = await _rows(store, _OLD)
    out = tmp_path / "archive"
    archived = await archive_embeddings.archive(pg_dsn, model=_OLD, out=out)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "ALTER TABLE session_embeddings ADD COLUMN archive_note "
            "text NOT NULL DEFAULT 'new-default'",
        )
        await conn.execute(
            "ALTER TABLE session_index_state ADD COLUMN archive_note "
            "text NOT NULL DEFAULT 'new-default'",
        )

    assert await archive_embeddings.restore(pg_dsn, source=out) == archived
    assert await _rows(store, _OLD) == {**before, "session_index_cursor": {(0,)}}
    async with store.engine.acquire() as conn:
        vectors = await conn.fetch(
            "SELECT archive_note FROM session_embeddings WHERE model = $1",
            _OLD,
        )
        markers = await conn.fetch(
            "SELECT archive_note FROM session_index_state WHERE model = $1",
            _OLD,
        )
    assert [row["archive_note"] for row in vectors] == ["new-default"] * 2
    assert [row["archive_note"] for row in markers] == ["new-default"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_archive_refuses_a_directory_that_exists(
    store: Store,
    pg_dsn: str,
    tmp_path: Path,
) -> None:
    await _session_with_vectors(store, chunks=1)
    before = await _rows(store, _OLD)

    with pytest.raises(FileExistsError):
        await archive_embeddings.archive(pg_dsn, model=_OLD, out=tmp_path)

    assert await _rows(store, _OLD) == before


class _ShortCopy:
    """A connection whose every COPY reports one row fewer than it wrote."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> object:
        return cast(object, getattr(self._conn, name))

    async def copy_from_query(
        self,
        query: str,
        *args: object,
        output: Path,
        format: Literal["binary"],
    ) -> str:
        status = await self._conn.copy_from_query(
            query,
            *args,
            output=output,
            format=format,
        )
        return f"COPY {int(status.rsplit(' ', 1)[1]) - 1}"


class _ShortCopyEngine:
    """A ``PostgresEngine`` lending :class:`_ShortCopy` connections."""

    def __init__(self, *, dsn: str, listen_channel: str) -> None:
        self._engine = PostgresEngine(dsn=dsn, listen_channel=listen_channel)

    async def __aenter__(self) -> Self:
        await self._engine.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._engine.__aexit__(None, None, None)

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[_ShortCopy]:
        async with self._engine.acquire() as conn:
            yield _ShortCopy(conn)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_archive_that_lost_a_row_deletes_nothing(
    store: Store,
    pg_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _session_with_vectors(store, chunks=2)
    before = await _rows(store, _OLD)
    monkeypatch.setattr(archive_embeddings, "PostgresEngine", _ShortCopyEngine)

    with pytest.raises(archive_embeddings.ArchiveMismatchError, match="copied 1 rows"):
        await archive_embeddings.archive(pg_dsn, model=_OLD, out=tmp_path / "a")

    assert await _rows(store, _OLD) == before


@pytest.mark.parametrize(
    ("argv", "missing"),
    [
        (["dsn"], "command"),
        (["dsn", "archive", "--out", "archive"], "--model"),
        (["dsn", "archive", "--model", "m"], "--out"),
        (["dsn", "restore"], "--from"),
    ],
)
def test_main_requires_command_and_operation_arguments(
    argv: list[str],
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["archive", *argv])
    with pytest.raises(SystemExit) as exited:
        archive_embeddings.main()
    assert exited.value.code == 2
    assert missing in capsys.readouterr().err


def test_main_archives_with_the_named_model_and_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, Path]] = []

    async def archive(dsn: str, *, model: str, out: Path) -> dict[str, int]:
        calls.append((dsn, model, out))
        return {"session_embeddings": 3, "session_index_state": 2}

    out = tmp_path / "archive"
    monkeypatch.setattr(archive_embeddings, "archive", archive)
    monkeypatch.setattr(
        sys,
        "argv",
        ["archive", "dsn", "archive", "--model", _OLD, "--out", str(out)],
    )

    assert archive_embeddings.main() == 0
    assert calls == [("dsn", _OLD, out)]
    assert capsys.readouterr().out == (
        f"archived {_OLD} to {out}: session_embeddings=3 session_index_state=2\n"
    )


def test_main_reports_a_mismatch_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    async def mismatched(dsn: str, *, model: str, out: Path) -> dict[str, int]:
        del dsn, model, out
        raise archive_embeddings.ArchiveMismatchError("session_embeddings: copied 1")

    monkeypatch.setattr(archive_embeddings, "archive", mismatched)
    out = str(tmp_path / "a")
    monkeypatch.setattr(
        sys,
        "argv",
        ["archive", "dsn", "archive", "--model", "m", "--out", out],
    )

    assert archive_embeddings.main() == 1
    assert capsys.readouterr().out == "nothing deleted: session_embeddings: copied 1\n"


def test_main_refuses_an_out_directory_that_exists(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """An existing ``--out`` is a usage error, before any database is reached."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["archive", "dsn", "archive", "--model", "m", "--out", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exited:
        archive_embeddings.main()

    assert exited.value.code == 2
    assert f"--out {tmp_path} already exists" in capsys.readouterr().err


def test_main_refuses_a_from_directory_with_no_archive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A ``--from`` that ``archive`` did not write is a usage error naming the gap."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["archive", "dsn", "restore", "--from", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exited:
        archive_embeddings.main()

    assert exited.value.code == 2
    assert f"no {tmp_path / 'model.txt'}" in capsys.readouterr().err


def test_main_restores_from_the_named_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Path]] = []

    async def restore(dsn: str, *, source: Path) -> dict[str, int]:
        calls.append((dsn, source))
        return {"session_embeddings": 3, "session_index_state": 2}

    monkeypatch.setattr(archive_embeddings, "restore", restore)
    monkeypatch.setattr(
        sys,
        "argv",
        ["archive", "dsn", "restore", "--from", str(tmp_path)],
    )

    assert archive_embeddings.main() == 0
    assert calls == [("dsn", tmp_path)]
    assert capsys.readouterr().out == (
        f"restored from {tmp_path}: session_embeddings=3 session_index_state=2\n"
    )


@pytest.mark.asyncio
async def test_archive_copies_every_table_before_deleting_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    engine.conn.fetchval.side_effect = [2, 1]
    copy = AsyncMock(side_effect=["COPY 2", "COPY 1"])
    execute = AsyncMock(return_value="UPDATE 1")
    events = Mock()
    events.attach_mock(copy, "copy")
    events.attach_mock(execute, "execute")
    monkeypatch.setattr(engine.conn, "copy_from_query", copy)
    monkeypatch.setattr(engine.conn, "execute", execute)
    factory = Mock(return_value=engine)
    monkeypatch.setattr(archive_embeddings, "PostgresEngine", factory)
    out = tmp_path / "nested" / "archive"

    counts = await archive_embeddings.archive("dsn", model=_OLD, out=out)

    assert counts == {"session_embeddings": 2, "session_index_state": 1}
    assert (out / "model.txt").read_text() == _OLD + "\n"
    factory.assert_called_once_with(dsn="dsn", listen_channel=NOTIFY_CHANNEL)
    assert engine.entered
    assert engine.exited
    tables = archive_embeddings._archived_tables()
    assert tables == {
        "session_embeddings": (
            "session_id, part, idx, field, chunk, mapper, model, embedding, "
            "text_md5, created"
        ),
        "session_index_state": "session_id, part, idx, mapper, model, text_md5, created",
    }
    assert [event[0] for event in events.mock_calls] == [
        "execute",
        "execute",
        "copy",
        "copy",
        "execute",
        "execute",
        "execute",
        "execute",
    ]
    queries = (
        (
            "SELECT session_id, part, idx, field, chunk, mapper, model, embedding, "
            "text_md5, created FROM session_embeddings WHERE model = $1"
        ),
        (
            "SELECT session_id, part, idx, mapper, model, text_md5, created "
            "FROM session_index_state WHERE model = $1"
        ),
    )
    for call, table, query in zip(copy.await_args_list, tables, queries, strict=True):
        assert call.args == (query, _OLD)
        assert call.kwargs == {"output": out / f"{table}.copy", "format": "binary"}
    executed = [call.args for call in engine.conn.execute.await_args_list]
    assert executed == [
        ("BEGIN",),
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",),
        ("DELETE FROM session_embeddings WHERE model = $1", _OLD),
        ("DELETE FROM session_index_state WHERE model = $1", _OLD),
        ("DELETE FROM session_index_cursor WHERE model = $1", _OLD),
        ("COMMIT",),
    ]


@pytest.mark.asyncio
async def test_a_short_second_copy_rolls_back_before_any_model_is_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    engine.conn.fetchval.side_effect = [2, 1]
    copy = AsyncMock(side_effect=["COPY 2", "COPY 0"])
    monkeypatch.setattr(engine.conn, "copy_from_query", copy)
    monkeypatch.setattr(archive_embeddings, "PostgresEngine", Mock(return_value=engine))

    with pytest.raises(
        archive_embeddings.ArchiveMismatchError,
        match=(
            r"^session_index_state: copied 0 rows of "
            r"qwen3-embedding-4b@1024, the table holds 1$"
        ),
    ):
        await archive_embeddings.archive("dsn", model=_OLD, out=tmp_path / "archive")

    assert [call.args for call in engine.conn.execute.await_args_list] == [
        ("BEGIN",),
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",),
    ]
    engine.conn.fetch.assert_awaited_once_with("ROLLBACK")
    assert copy.await_count == 2
    assert engine.exited


@pytest.mark.asyncio
async def test_restore_stages_explicit_columns_and_counts_only_inserted_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "model.txt").write_text(f"  {_OLD}\n")
    engine = FakeEngine()
    copy = AsyncMock(return_value="COPY 3")
    monkeypatch.setattr(engine.conn, "copy_to_table", copy)
    engine.conn.execute.side_effect = [
        "BEGIN",
        "CREATE TABLE",
        "INSERT 0 2",
        "CREATE TABLE",
        "INSERT 0 1",
        "COMMIT",
    ]
    monkeypatch.setattr(archive_embeddings, "PostgresEngine", Mock(return_value=engine))

    assert await archive_embeddings.restore("dsn", source=tmp_path) == {
        "session_embeddings": 2,
        "session_index_state": 1,
    }

    statements = (
        (
            (
                "CREATE TEMP TABLE restore_session_embeddings "
                "(LIKE session_embeddings INCLUDING DEFAULTS) ON COMMIT DROP"
            ),
            (
                "INSERT INTO session_embeddings (session_id, part, idx, field, chunk, "
                "mapper, model, embedding, text_md5, created) SELECT session_id, part, "
                "idx, field, chunk, mapper, model, embedding, text_md5, created "
                "FROM restore_session_embeddings s WHERE s.model = $1 "
                "AND EXISTS (SELECT 1 FROM inquiries i WHERE i.id = s.session_id) "
                "ON CONFLICT DO NOTHING"
            ),
        ),
        (
            (
                "CREATE TEMP TABLE restore_session_index_state "
                "(LIKE session_index_state INCLUDING DEFAULTS) ON COMMIT DROP"
            ),
            (
                "INSERT INTO session_index_state (session_id, part, idx, mapper, model, "
                "text_md5, created) SELECT session_id, part, idx, mapper, model, "
                "text_md5, created FROM restore_session_index_state s WHERE s.model = $1 "
                "AND EXISTS (SELECT 1 FROM inquiries i WHERE i.id = s.session_id) "
                "ON CONFLICT DO NOTHING"
            ),
        ),
    )
    for index, (table, columns) in enumerate(
        archive_embeddings._archived_tables().items(),
    ):
        assert copy.await_args_list[index].args == (f"restore_{table}",)
        assert copy.await_args_list[index].kwargs == {
            "source": tmp_path / f"{table}.copy",
            "columns": columns.split(", "),
            "format": "binary",
        }
        create, insert = engine.conn.execute.await_args_list[
            1 + 2 * index : 3 + 2 * index
        ]
        create_sql, insert_sql = statements[index]
        assert create.args == (create_sql,)
        assert insert.args == (insert_sql, _OLD)
    assert engine.conn.execute.await_args_list[-1].args == ("COMMIT",)
    assert engine.exited


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
