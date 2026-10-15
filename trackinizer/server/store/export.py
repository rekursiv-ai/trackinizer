""":class:`_ExportMixin` -- the read side of the logical export.

:meth:`export_graph` reads every table in
:data:`~trackinizer.wire.wire_export.EXPORT_TABLES` and returns the rows as
plain Python values; the route owns turning them into JSON lines. A pure leaf
like :class:`_ReadMixin`: it reads through ``self.engine`` and calls no other
mixin.

Three choices worth knowing before changing this:

* **One snapshot for every table.** Under ``--engine pg`` a write landing
  between two tables' reads would leave, say, an edge whose inquiry the export
  never saw. ``REPEATABLE READ`` pins every read to one snapshot.
* **Everything is fetched before the connection goes back.** PGlite serves a
  single connection, so streaming the response straight off a cursor would
  hold it for as long as the slowest client takes to read the body, and every
  other request would wait behind it.
* **A session's transcript is exported to a caller who reads it.** The header
  names the audiences the export was read for and counts the sessions whose
  transcripts it left out, so a file that is not whole says so. A science chat's
  transcript goes only to the chat's members, whatever their role, so on a server
  with chats of more than one person no export holds them all: only a database
  dump does.

Columns come from the catalog rather than a list kept here, so a column added
by a migration is exported without touching this file. A new TABLE is a
decision, not a default: ``export_pglite_test`` fails until it is named in
``EXPORT_TABLES`` or in that test's list of deliberate omissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import json

from trackinizer.server.audience import (
    NOBODY,
    SessionReader,
    hidden_exists_sql,
    visible_sql,
)
from trackinizer.server.notify import tx
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.wire.wire_export import EXPORT_TABLES


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.lib.postgres import Conn


__all__ = [
    "GraphExport",
    "_ExportMixin",
]


_ORDER_BY: Final[Mapping[str, str]] = {
    "inquiries": "created, id",
    "edges": "from_id, to_id, edge_kind",
    "change_log": "created, id",
    "experiment_metrics": "experiment_id, key, step",
    "session_manifests": "session_id, part",
    "session_records": "session_id, part, idx",
    "session_slash_commands": "session_id, seq",
}
"""A total order per table: its primary key, led by ``created`` where the
table has one, so rows written since the last export land at the end of the
next one instead of reshuffling it."""

_SKIPPED_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    # A ``tsvector`` the database derives from ``text``; the text is exported.
    "session_records": frozenset({"search"}),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphExport:
    """Every exported row, read in one snapshot.

    Attributes:
      migrations: Applied schema files, oldest first.
      audiences: The audiences the export was read for, sorted. A session outside
        them has no transcript in ``tables``, so a file that names fewer audiences
        than the deployment has is not a whole backup.
      withheld_sessions: How many sessions have no transcript in ``tables`` because
        the reader may not read it, as the transcript test of
        :mod:`~trackinizer.server.audience` says. A file that counts any is not a
        whole backup.
      tables: ``(table, rows)`` in :data:`EXPORT_TABLES` order; each row maps
        column name to value in the table's column order.

    """

    migrations: tuple[str, ...]
    audiences: tuple[str, ...]
    withheld_sessions: int
    tables: tuple[tuple[str, tuple[dict[str, object], ...]], ...]


class _ExportMixin(_StoreShared):
    """The logical export for :class:`Store`."""

    async def export_graph(
        self,
        *,
        reader: SessionReader = NOBODY,
    ) -> GraphExport:
        """Read the whole graph in one read-only snapshot.

        Every table keyed by a session leaves out the rows of a session whose
        transcript ``reader`` may not read. The session's own row stays, as it does in
        the graph a caller reads: only the transcript is gated.

        Args:
          reader: Who reads.

        Returns:
          graph: The applied migrations, every exported table's rows, and how many
            sessions' transcripts it left out.

        """
        async with self.engine.acquire() as conn, tx(conn):
            await conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
            )
            # A fresh database applies the baseline and every numbered file in
            # one bootstrap, so they share ``applied_at``; the baseline still
            # came first, and a plain name tie-break would sort it after
            # ``schema.0NN.sql``.
            migrations = await conn.fetch(
                "SELECT name FROM applied_migrations "
                "ORDER BY applied_at, name <> 'schema.sql', name",
            )
            columns = await _exported_columns(conn)
            tables = [
                (
                    table,
                    await _read_table(conn, table, columns[table], reader=reader),
                )
                for table in EXPORT_TABLES
            ]
            params: list[object] = []
            withheld = await conn.fetchval(
                vetted_sql(
                    "SELECT count(*) FROM inquiries AS withheld "
                    "WHERE withheld.kind = 'AgentSession' AND NOT ",
                    visible_sql("withheld", params=params, reader=reader),
                ),
                *params,
            )
        return GraphExport(
            migrations=tuple(cast(str, row["name"]) for row in migrations),
            audiences=tuple(sorted(reader.audiences)),
            withheld_sessions=cast(int, withheld),
            tables=tuple(tables),
        )


async def _exported_columns(conn: Conn) -> dict[str, tuple[tuple[str, bool], ...]]:
    """Each exported table's columns in order, flagged when stored as ``json``."""
    rows = await conn.fetch(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[]) "
        "ORDER BY table_name, ordinal_position",
        list(EXPORT_TABLES),
    )
    columns: dict[str, list[tuple[str, bool]]] = {table: [] for table in EXPORT_TABLES}
    for row in rows:
        table = cast(str, row["table_name"])
        column = cast(str, row["column_name"])
        if column in _SKIPPED_COLUMNS.get(table, frozenset()):
            continue
        columns[table].append((column, row["data_type"] == "json"))
    return {table: tuple(cols) for table, cols in columns.items()}


async def _read_table(
    conn: Conn,
    table: str,
    columns: tuple[tuple[str, bool], ...],
    *,
    reader: SessionReader,
) -> tuple[dict[str, object], ...]:
    """Every row of ``table`` the caller may read, in its export order, as plain values."""
    select = ", ".join(f'"{name}"' for name, _ in columns)
    params: list[object] = []
    # Audience: gated -- a table keyed by session leaves out the rows of a session
    # ``reader`` may not read. Found from the columns, so a session table the export
    # gains is gated without an edit here.
    where = (
        vetted_sql(
            " WHERE ",
            hidden_exists_sql(
                f"{table}.session_id",
                params=params,
                reader=reader,
            ),
        )
        if any(name == "session_id" for name, _ in columns)
        else ""
    )
    records = await conn.fetch(
        vetted_sql(
            "SELECT ",
            select,
            " FROM ",
            table,
            where,
            " ORDER BY ",
            _ORDER_BY[table],
        ),
        *params,
    )
    json_columns = [name for name, is_json in columns if is_json]
    rows: list[dict[str, object]] = []
    for record in records:
        row = {name: record[name] for name, _ in columns}
        for name in json_columns:
            if (text := row[name]) is not None:
                row[name] = json.loads(cast(str, text))
        rows.append(row)
    return tuple(rows)
