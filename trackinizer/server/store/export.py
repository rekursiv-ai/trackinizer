""":class:`_ExportMixin` -- the read side of the logical export.

:meth:`export_graph` reads every table in
:data:`~trackinizer.wire.wire_export.EXPORT_TABLES` and returns the rows as
plain Python values; the route owns turning them into JSON lines. A pure leaf
like :class:`_ReadMixin`: it reads through ``self.engine`` and calls no other
mixin.

A selector narrows the export to a subgraph. It picks the inquiries, and
:data:`_SCOPE_COLUMNS` carries that choice to every other table: a row rides
along when the inquiry it hangs off was selected. ``edges`` needs BOTH ends,
since an edge to a row outside the selection would arrive dangling.

Filters reuse :mod:`trackinizer.wire.filters` rather than a selector language
of this module's own. The example in the request, ``org:rekursiv AND NOT
machine:*``, is already two clauses the list queries can say: ``labels is
org:rekursiv`` and ``labels nre ^machine:``. They AND, which is what a
partition asks for and all the request asks for; OR would need a grammar, and
no caller has needed one yet.

Two choices worth knowing before changing this:

* **One snapshot for every table.** Under ``--engine pg`` a write landing
  between two tables' reads would leave, say, an edge whose inquiry the export
  never saw. ``REPEATABLE READ`` pins every read to one snapshot.
* **Everything is fetched before the connection goes back.** PGlite serves a
  single connection, so streaming the response straight off a cursor would
  hold it for as long as the slowest client takes to read the body, and every
  other request would wait behind it.

Columns come from the catalog rather than a list kept here, so a column added
by a migration is exported without touching this file. A new TABLE is a
decision, not a default: ``export_pglite_test`` fails until it is named in
``EXPORT_TABLES`` or in that test's list of deliberate omissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from trackinizer.lib.custom_json import loads
from trackinizer.server.notify import tx
from trackinizer.server.store.read import _lower_filter
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_export import EXPORT_TABLES


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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

_SCOPE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "inquiries": ("id",),
    # Both ends, so the subgraph closes: an edge whose peer was not selected
    # would name a row the reader never receives.
    "edges": ("from_id", "to_id"),
    # FK-free by design (it outlives its subject), so this is the one scope
    # the schema does not enforce for us.
    "change_log": ("subject_id",),
    "experiment_metrics": ("experiment_id",),
    "session_manifests": ("session_id",),
    "session_records": ("session_id",),
    "session_slash_commands": ("session_id",),
}
"""Per table, the columns that must name a selected inquiry.

Every exported table hangs off ``inquiries`` by a single id, so a selector on
the inquiries carries to all of them. A new table in :data:`EXPORT_TABLES`
needs its entry here; ``export_pglite_test`` fails until it has one."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphExport:
    """Every exported row, read in one snapshot.

    Attributes:
      migrations: Applied schema files, oldest first.
      tables: ``(table, rows)`` in :data:`EXPORT_TABLES` order; each row maps
        column name to value in the table's column order.
      selector: The filters that narrowed this export, empty for a whole
        graph. Carried so the header can say which subgraph this is: the rows
        alone cannot tell a reader whether a row is absent or excluded.

    """

    migrations: tuple[str, ...]
    tables: tuple[tuple[str, tuple[dict[str, object], ...]], ...]
    selector: tuple[Filter, ...] = ()


class _ExportMixin(_StoreShared):
    """The logical export for :class:`Store`."""

    async def export_graph(
        self,
        *,
        selector: Sequence[Filter] = (),
    ) -> GraphExport:
        """Read the graph, or one selector's subgraph, in one read-only snapshot.

        Args:
          selector: Filters on ``inquiries``, ANDed. Empty exports everything.

        Returns:
          graph: The applied migrations and every exported table's rows.

        """
        scope, params = _scope(selector)
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
                (table, await _read_table(conn, table, columns[table], scope, params))
                for table in EXPORT_TABLES
            ]
        return GraphExport(
            migrations=tuple(cast(str, row["name"]) for row in migrations),
            tables=tuple(tables),
            selector=tuple(selector),
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


# The selected ids are a SUBQUERY reused per table rather than a list read back and
# bound per statement: the ids never leave the database, one plan serves every table,
# and the repeated ``$n`` placeholders bind the same operands each time.
def _scope(selector: Sequence[Filter]) -> tuple[str, list[object]]:
    """Render ``selector`` as the subquery selecting its inquiry ids, or ``""``."""
    if not selector:
        return "", []
    params: list[object] = []
    clauses: list[str] = []
    for filt in selector:
        clause = _lower_filter(filt, params)
        if clause is None:
            # Every filter the route admits lowers, so this is a programming
            # error rather than bad input: a Python-evaluated clause would
            # silently widen the subgraph to the whole graph.
            raise ValueError(f"export selector does not lower to SQL: {filt}")
        clauses.append(clause)
    return vetted_sql("SELECT id FROM inquiries WHERE ", " AND ".join(clauses)), params


def _where(table: str, scope: str) -> str:
    """Render the ``WHERE`` keeping only ``table``'s rows inside ``scope``."""
    if not scope:
        return ""
    tests = " AND ".join(
        vetted_sql('"', column, '" IN (', scope, ")")
        for column in _SCOPE_COLUMNS[table]
    )
    return vetted_sql(" WHERE ", tests)


async def _read_table(
    conn: Conn,
    table: str,
    columns: tuple[tuple[str, bool], ...],
    scope: str = "",
    params: Sequence[object] = (),
) -> tuple[dict[str, object], ...]:
    """Every in-scope row of ``table``, in its export order, as plain Python values."""
    select = ", ".join(f'"{name}"' for name, _ in columns)
    records = await conn.fetch(
        vetted_sql(
            "SELECT ",
            select,
            " FROM ",
            table,
            _where(table, scope),
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
                row[name] = loads(cast(str, text))
        rows.append(row)
    return tuple(rows)
