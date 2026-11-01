""":class:`_ExportMixin` -- the read side of the logical export.

:meth:`export_graph` reads every table in
:data:`~trackinizer.wire.wire_export.EXPORT_TABLES` and returns the rows as
plain Python values; the route owns turning them into JSON lines. It reads
through ``self.engine`` and inherits :class:`_ReadMixin` for
:meth:`~_ReadMixin.change_read_filter`.

A selector narrows the export to a subgraph. It picks the inquiries, and
:data:`_SCOPE_COLUMNS` carries that choice to every other table: a row rides
along when the inquiry it hangs off was selected. ``edges`` needs BOTH ends,
since an edge to a row outside the selection would arrive dangling.
Other IDs, including audit causes and embedded inquiry references, remain
unchanged and may name rows outside the slice. They do not expand selection.

Filters reuse :mod:`trackinizer.wire.filters` rather than a selector language
of this module's own. The example in the request, ``org:rekursiv AND NOT
machine:*``, is already two clauses the list queries can say: ``labels is
org:rekursiv`` and ``labels nre ^machine:``. They AND, which is what a
partition asks for and all the request asks for; OR would need a grammar, and
no caller has needed one yet.

Four choices worth knowing before changing this:

* **One snapshot for every table.** Under ``--engine pg`` a write landing
  between two tables' reads would leave, say, an edge whose inquiry the export
  never saw. ``REPEATABLE READ`` pins every read to one snapshot.
* **The access policy decides after the snapshot commits.** An auditing policy
  writes a note as it filters each row it would hide. A read-only snapshot
  refuses that write, and a repeatable-read one fails with a serialization error
  when a concurrent read noted the same row first. So the snapshot reads no
  grant, and one read after it asks the policy which of the read inquiries and
  changes the reader may keep.
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
from uuid import UUID

import json

from trackinizer.server.access_policy import inquiry_read_filter
from trackinizer.server.audience import (
    NOBODY,
    SessionReader,
    hidden_exists_sql,
    visible_sql,
)
from trackinizer.server.notify import tx
from trackinizer.server.regex_timeout import apply_regex_statement_timeout
from trackinizer.server.store.read import _lower_filter, _ReadMixin
from trackinizer.server.values import vetted_sql
from trackinizer.wire.filters import Filter
from trackinizer.wire.row_filter import reject_inadmissible
from trackinizer.wire.wire_export import EXPORT_TABLES


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import Principal


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
    # Both ends: an edge whose peer was not selected
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
      audiences: The audiences the export was read for, sorted. A session outside
        them has no transcript in ``tables``, so a file that names fewer audiences
        than the deployment has is not a whole backup.
      withheld_sessions: How many sessions have no transcript in ``tables`` because
        the reader may not read it, as the transcript test of
        :mod:`~trackinizer.server.audience` says. A file that counts any is not a
        whole backup.
      tables: ``(table, rows)`` in :data:`EXPORT_TABLES` order; each row maps
        column name to value in the table's column order.
      selector: The filters that narrowed this export, empty for a whole
        graph. Carried so the header can say which subgraph this is: the rows
        alone cannot tell a reader whether a row is absent or excluded.

    """

    migrations: tuple[str, ...]
    audiences: tuple[str, ...]
    withheld_sessions: int
    tables: tuple[tuple[str, tuple[dict[str, object], ...]], ...]
    selector: tuple[Filter, ...] = ()


class _ExportMixin(_ReadMixin):
    """The logical export for :class:`Store`."""

    async def export_graph(
        self,
        *,
        reader: SessionReader = NOBODY,
        selector: Sequence[Filter] = (),
        principal: Principal | None = None,
    ) -> GraphExport:
        """Read the graph, or one selector's subgraph, in one read-only snapshot.

        Every table keyed by a session leaves out the rows of a session whose
        transcript ``reader`` may not read. The session's own row stays, as it does in
        the graph a caller reads: only the transcript is gated.

        Every row hangs off inquiries ``principal`` may read, and a ``change_log`` row
        stays as :meth:`change_read_filter` keeps it, as the access policy says once
        the snapshot has committed. A row purged since the snapshot is left out.

        Args:
          reader: Who reads.
          selector: Filters on ``inquiries``, ANDed. Empty exports everything.
          principal: Request reader; None for a trusted internal export.

        Returns:
          graph: The applied migrations, every exported table's rows, and how many
            sessions' transcripts it left out.

        """
        scope, scope_params = _scope(selector)
        async with self.engine.acquire() as conn, tx(conn):
            await conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
            )
            if selector:
                await apply_regex_statement_timeout(conn)
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
                    await _read_table(
                        conn,
                        table,
                        columns[table],
                        reader=reader,
                        scope=scope,
                        scope_params=scope_params,
                    ),
                )
                for table in EXPORT_TABLES
            ]
            # Counted inside the selection: a session the selector left out is
            # not missing from this file, it was never asked for.
            params: list[object] = list(scope_params)
            withheld = [
                cast(UUID, row["id"])
                for row in await conn.fetch(
                    vetted_sql(
                        "SELECT withheld.id FROM inquiries AS withheld "
                        "WHERE withheld.kind = 'AgentSession' AND NOT ",
                        visible_sql("withheld", params=params, reader=reader),
                        vetted_sql(" AND withheld.id IN (", scope, ")")
                        if scope
                        else "",
                    ),
                    *params,
                )
            ]
        kept = await self._kept(tables, withheld=withheld, principal=principal)
        return GraphExport(
            migrations=tuple(cast(str, row["name"]) for row in migrations),
            audiences=tuple(sorted(reader.audiences)),
            # A session the policy hides is not counted: its count would say it exists.
            withheld_sessions=sum(kept.inquiry(session) for session in withheld),
            tables=tuple(
                (table, tuple(row for row in rows if kept.row(table, row)))
                for table, rows in tables
            ),
            selector=tuple(selector),
        )

    async def _kept(
        self,
        tables: Sequence[tuple[str, Sequence[Mapping[str, object]]]],
        *,
        withheld: Sequence[UUID],
        principal: Principal | None,
    ) -> _Kept:
        """Ask the policy which read inquiries and changes ``principal`` may keep."""
        if (
            inquiry_read_filter(
                "inquiries.id",
                policy=self.access_policy,
                principal=principal,
                params=[],
            )
            == "TRUE"
        ):
            return _Kept()
        inquiry_params: list[object] = [
            list(
                {
                    *withheld,
                    *(
                        row[column]
                        for table, rows in tables
                        if table != "change_log"
                        for row in rows
                        for column in _SCOPE_COLUMNS[table]
                    ),
                },
            ),
        ]
        change_params: list[object] = [
            [
                row["id"]
                for table, rows in tables
                if table == "change_log"
                for row in rows
            ],
        ]
        readable = inquiry_read_filter(
            "inquiries.id",
            policy=self.access_policy,
            principal=principal,
            params=inquiry_params,
        )
        changed = self.change_read_filter(
            "change_log",
            params=change_params,
            principal=principal,
        )
        async with self.engine.acquire() as conn:
            kept_inquiries = await conn.fetch(
                vetted_sql(
                    "SELECT id FROM inquiries WHERE id = ANY($1::uuid[]) AND (",
                    readable,
                    ")",
                ),
                *inquiry_params,
            )
            kept_changes = await conn.fetch(
                vetted_sql(
                    "SELECT id FROM change_log WHERE id = ANY($1::uuid[]) AND (",
                    changed,
                    ")",
                ),
                *change_params,
            )
        return _Kept(
            inquiries=frozenset(cast(UUID, row["id"]) for row in kept_inquiries),
            changes=frozenset(cast(UUID, row["id"]) for row in kept_changes),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Kept:
    """The read inquiries and changes the reader may keep; None keeps every one."""

    inquiries: frozenset[UUID] | None = None
    changes: frozenset[UUID] | None = None

    def inquiry(self, inquiry_id: object) -> bool:
        """Say whether the reader may keep the inquiry ``inquiry_id``."""
        return self.inquiries is None or inquiry_id in self.inquiries

    def row(self, table: str, row: Mapping[str, object]) -> bool:
        """Say whether the reader may keep ``row`` of ``table``."""
        if table == "change_log":
            return self.changes is None or row["id"] in self.changes
        return self.inquiries is None or all(
            row[column] in self.inquiries for column in _SCOPE_COLUMNS[table]
        )


async def _exported_columns(conn: Conn) -> dict[str, tuple[tuple[str, bool], ...]]:
    """Each exported table's columns in order, flagged when stored as ``json``."""
    # pragma: no mutate start -- SQL is judged by a database: export_pglite_test.
    rows = await conn.fetch(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[]) "
        "ORDER BY table_name, ordinal_position",
        list(EXPORT_TABLES),
    )
    # pragma: no mutate end
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
        reject_inadmissible(filt)
        clause = _lower_filter(filt, params)
        if clause is None:
            # Every filter the route admits lowers, so this is a programming
            # error rather than bad input: a Python-evaluated clause would
            # silently widen the subgraph to the whole graph.
            raise ValueError(f"export selector does not lower to SQL: {filt}")
        clauses.append(clause)
    return vetted_sql("SELECT id FROM inquiries WHERE ", " AND ".join(clauses)), params


async def _read_table(
    conn: Conn,
    table: str,
    columns: tuple[tuple[str, bool], ...],
    *,
    reader: SessionReader,
    scope: str = "",
    scope_params: Sequence[object] = (),
) -> tuple[dict[str, object], ...]:
    """Every in-scope row of ``table`` the caller may read, in export order, as plain values."""
    select = ", ".join(f'"{name}"' for name, _ in columns)
    # The scope's operands go first: its placeholders were numbered from ``$1``, and
    # the audience test numbers its own after whatever is already here.
    params: list[object] = list(scope_params)
    conditions: list[str] = []
    if scope:
        conditions.append(
            " AND ".join(
                vetted_sql('"', column, '" IN (', scope, ")")
                for column in _SCOPE_COLUMNS[table]
            ),
        )
    # Audience: gated -- a table keyed by session leaves out the rows of a session
    # ``reader`` may not read. Found from the columns, so a session table the export
    # gains is gated without an edit here.
    if any(name == "session_id" for name, _ in columns):
        conditions.append(
            hidden_exists_sql(
                f"{table}.session_id",
                params=params,
                reader=reader,
            ),
        )
    where = vetted_sql(" WHERE ", " AND ".join(conditions)) if conditions else ""
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
