"""Archive one model's embeddings to files and restore them, against pglite."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID, uuid4

import sys

import pytest

from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.tools import archive_embeddings


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.core import Store


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
    out = tmp_path / "embeddings-4b"

    archived = await archive_embeddings.archive(pg_dsn, model=_OLD, out=out)

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
        return getattr(self._conn, name)

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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
