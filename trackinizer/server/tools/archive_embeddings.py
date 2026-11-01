#!/bin/sh
# ruff: noqa: EXE003, D300, D205, T201 -- Polyglot script; a CLI main that prints.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 -P "$0" "$@"
Move one model's session embeddings out of the database, or back in.

``archive`` copies a model's ``session_embeddings`` rows and its freshness
markers (``session_index_state``) to files under ``--out``, then deletes them
and the model's sweep cursor, all in one transaction: the delete runs only if
both copies wrote exactly the rows the database holds. ``restore`` loads such a
directory back, skipping rows whose session has since been deleted and rows
already present, so a restore over a partly re-embedded corpus keeps the newer
ones. A restored model has no cursor, so its next sweep reads every record
whose marker does not match and re-embeds only what changed meanwhile.

The files are PostgreSQL binary ``COPY``, readable only by a server with the
same pgvector (``halfvec``); they are a restore point, not an interchange
format. Run as the role that owns the tables: the delete needs it.

Examples:
  ./archive_embeddings.py "$TRACKINIZER_DSN" archive \
      --model qwen3-embedding-4b@1024 --out /srv/backups/embeddings-4b
  ./archive_embeddings.py "$TRACKINIZER_DSN" restore --from /srv/backups/embeddings-4b

'''
# fmt: on

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import argparse
import asyncio

from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.notify import NOTIFY_CHANNEL, tx


def main() -> int:
    """Archive or restore one model's embeddings.

    Returns:
      exit_code: 0 on success, 1 when the archive's copies disagree with the
        database (nothing was deleted), 2 for a directory the command cannot use.

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast("Flags", parser.parse_args())
    if flags.command == "archive":
        try:
            counts = asyncio.run(archive(flags.dsn, model=flags.model, out=flags.out))
        except FileExistsError:
            parser.error(f"--out {flags.out} already exists; name a new directory")
        except ArchiveMismatchError as error:
            print(f"nothing deleted: {error}")
            return 1
        print(f"archived {flags.model} to {flags.out}: {_describe(counts)}")
        return 0
    try:
        counts = asyncio.run(restore(flags.dsn, source=flags.source))
    except FileNotFoundError as error:
        parser.error(f"--from {flags.source} is not an archive: no {error.filename}")
    print(f"restored from {flags.source}: {_describe(counts)}")
    return 0


class Flags(Protocol):
    """Parsed command-line flags."""

    dsn: str
    command: str
    model: str
    out: Path
    source: Path


# Audience: internal -- an operator tool run on the server; it reports only counts.
async def archive(dsn: str, *, model: str, out: Path) -> dict[str, int]:
    """Copy ``model``'s rows to files under ``out``, then delete them.

    Args:
      dsn: Postgres DSN of the trackinizer database.
      model: The stored embedder identity (``slug@dim``) to move out.
      out: A directory that must not exist yet; created here.

    Returns:
      counts: Rows archived per table.

    Raises:
      ArchiveMismatchError: A copy wrote a different number of rows than the
        table holds for ``model``; the transaction rolls back, deleting nothing.

    """
    # The model name rides beside the copies, so ``restore`` needs no flag that
    # could disagree with what the files hold.
    await asyncio.to_thread(out.mkdir, parents=True)
    _ = await asyncio.to_thread((out / "model.txt").write_text, model + "\n")
    counts: dict[str, int] = {}
    async with (
        PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine,
        engine.acquire() as conn,
        tx(conn),
    ):
        # One snapshot for the counts, the copies and the deletes, so a row the
        # follow service writes meanwhile is in all of them or in none.
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        for table, columns in _archived_tables().items():
            held = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE model = $1",  # noqa: S608 -- table is a fixed name.
                model,
            )
            status = await conn.copy_from_query(
                f"SELECT {columns} FROM {table} WHERE model = $1",  # noqa: S608 -- table is a fixed name.
                model,
                output=out / f"{table}.copy",
                format="binary",
            )
            copied = int(status.rsplit(" ", 1)[1])
            if copied != held:
                raise ArchiveMismatchError(
                    f"{table}: copied {copied} rows of {model}, the table holds {held}",
                )
            counts[table] = copied
        for table in _archived_tables():
            await conn.execute(f"DELETE FROM {table} WHERE model = $1", model)  # noqa: S608 -- table is a fixed name.
        await conn.execute("DELETE FROM session_index_cursor WHERE model = $1", model)
    return counts


# Audience: internal -- an operator tool run on the server; it reports only counts.
async def restore(dsn: str, *, source: Path) -> dict[str, int]:
    """Load an :func:`archive` directory back; return the rows inserted per table.

    Args:
      dsn: Postgres DSN of the trackinizer database.
      source: A directory :func:`archive` wrote.

    Returns:
      counts: Rows inserted per table; a row already present, or whose session
        no longer exists, is not counted.

    """
    model = (await asyncio.to_thread((source / "model.txt").read_text)).strip()
    counts: dict[str, int] = {}
    async with (
        PostgresEngine(dsn=dsn, listen_channel=NOTIFY_CHANNEL) as engine,
        engine.acquire() as conn,
        tx(conn),
    ):
        for table, columns in _archived_tables().items():
            staging = f"restore_{table}"
            await conn.execute(
                f"CREATE TEMP TABLE {staging} (LIKE {table}) ON COMMIT DROP",
            )
            _ = await conn.copy_to_table(
                staging,
                source=source / f"{table}.copy",
                columns=columns.split(", "),
                format="binary",
            )
            # A session deleted since the archive took its rows' FK target with it.
            status = await conn.execute(
                f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {staging} s "  # noqa: S608 -- fixed names.
                "WHERE s.model = $1 "
                "AND EXISTS (SELECT 1 FROM inquiries i WHERE i.id = s.session_id) "
                "ON CONFLICT DO NOTHING",
                model,
            )
            counts[table] = int(status.rsplit(" ", 1)[1])
    return counts


class ArchiveMismatchError(RuntimeError):
    """An archive copy wrote a different number of rows than the table holds."""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument("dsn", help="Postgres DSN of the trackinizer database.")
    commands = parser.add_subparsers(dest="command", required=True)
    archive_parser = commands.add_parser(
        "archive",
        help="Copy a model's rows to --out, then delete them from the database.",
    )
    archive_parser.add_argument(
        "--model",
        required=True,
        help="The stored embedder identity to move out (slug@dim).",
    )
    archive_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="A new directory for the archive; it must not exist yet.",
    )
    restore_parser = commands.add_parser(
        "restore",
        help="Load an archive directory back into the database.",
    )
    restore_parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        help="A directory `archive` wrote.",
    )


def _describe(counts: dict[str, int]) -> str:
    """Render ``table=rows`` pairs for the closing line."""
    return " ".join(f"{table}={rows}" for table, rows in counts.items())


# Every column, named, so a restore into a table that has since gained a column
# with a default still loads; the cursor is left out on purpose (see the docstring).
def _archived_tables() -> dict[str, str]:
    """Return ``table -> columns`` for the tables an archive moves."""
    return {
        "session_embeddings": (
            "session_id, part, idx, field, chunk, mapper, model, embedding, "
            "text_md5, created"
        ),
        "session_index_state": "session_id, part, idx, mapper, model, text_md5, created",
    }


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
