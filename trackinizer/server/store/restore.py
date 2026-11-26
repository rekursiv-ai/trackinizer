"""Transactional restoration of a complete logical Trackinizer export."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import json

from trackinizer.server.notify import tx
from trackinizer.server.restore import ExportHeader, ExportRow, RestoreError
from trackinizer.server.store.export_columns import ExportColumn, exported_columns
from trackinizer.server.store.sequences import _reconcile_sequences
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.wire.wire_export import EXPORT_TABLES


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = ["RestoreReport", "_RestoreMixin"]


_BATCH_SIZE: Final = 256
_DERIVED_COLUMNS: Final[frozenset[str]] = frozenset({"changed_xid"})
_AUTH_REFERENCE_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    # The logical export deliberately omits credentials. Keep the audit event,
    # actor and snapshots, but do not manufacture a local credential identity.
    "change_log": frozenset({"api_key_id"}),
    "session_manifests": frozenset({"opened_by_api_key_id"}),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreReport:
    """Summary of one committed logical restore."""

    rows_by_table: tuple[tuple[str, int], ...]
    redacted_auth_references: int
    source_schema: str


class _RestoreMixin(_StoreShared):
    """Write-side counterpart to :class:`server.store.export._ExportMixin`."""

    async def restore_export(
        self: _StoreShared,
        source: Iterable[ExportHeader | ExportRow],
    ) -> RestoreReport:
        """Restore a complete logical export into an empty database.

        The operation is deliberately lower-level than submit/edit/edge APIs:
        those APIs create new activity, while this operation reconstructs
        historical rows with their original IDs, timestamps and audit events.
        Every write is one transaction; a malformed or rejected later line
        rolls back all earlier batches.

        Args:
          source: Header followed by rows from :class:`GraphExportReader`.

        Returns:
          report: Counts and explicit auth-reference normalizations.

        Raises:
          RestoreError: The document or destination is not restorable.
        """
        iterator = iter(source)
        try:
            header = next(iterator)
        except StopIteration:
            raise RestoreError("export is empty; expected a header") from None
        if not isinstance(header, ExportHeader):
            raise RestoreError("export source did not begin with a validated header")

        async with self.engine.acquire() as conn, tx(conn):
            await self._require_empty_restore_target(conn)
            columns = await exported_columns(conn)
            counts = {table: 0 for table in EXPORT_TABLES}
            causes: list[tuple[UUID, UUID]] = []
            redacted = 0
            current_table: str | None = None
            pending: list[tuple[object, ...]] = []

            for item in iterator:
                if not isinstance(item, ExportRow):
                    raise RestoreError(
                        "export source yielded an unexpected document item",
                    )
                if current_table != item.table:
                    if current_table is not None:
                        await _insert_batch(
                            conn,
                            current_table,
                            columns[current_table],
                            pending,
                        )
                        pending.clear()
                    current_table = item.table
                values, cause, removed_auth = _prepare_row(
                    item,
                    columns[item.table],
                )
                pending.append(values)
                counts[item.table] += 1
                redacted += removed_auth
                if cause is not None:
                    causes.append(cause)
                if len(pending) >= _BATCH_SIZE:
                    await _insert_batch(
                        conn,
                        item.table,
                        columns[item.table],
                        pending,
                    )
                    pending.clear()

            if current_table is not None and pending:
                await _insert_batch(
                    conn,
                    current_table,
                    columns[current_table],
                    pending,
                )
            await _restore_change_causes(conn, causes)
            await _reconcile_sequences(conn)

        return RestoreReport(
            rows_by_table=tuple((table, counts[table]) for table in EXPORT_TABLES),
            redacted_auth_references=redacted,
            source_schema=header.migrations[-1],
        )

    async def _require_empty_restore_target(self, conn: Conn) -> None:
        """Reject a destination containing any logical export table row."""
        for table in EXPORT_TABLES:
            exists = await conn.fetchval(
                vetted_sql("SELECT EXISTS (SELECT 1 FROM ", table, ")"),
            )
            if exists:
                raise RestoreError(
                    f"restore requires an empty database; table {table!r} is not empty",
                )

async def _insert_batch(
    conn: Conn,
    table: str,
    columns: Sequence[ExportColumn],
    rows: Sequence[tuple[object, ...]],
) -> None:
    """Insert one bounded batch using the destination catalog order."""
    if not rows:
        return
    names = [column.name for column in columns if column.name not in _DERIVED_COLUMNS]
    positions = [
        index
        for index, column in enumerate(columns)
        if column.name not in _DERIVED_COLUMNS
    ]
    sql = vetted_sql(
        "INSERT INTO ",
        table,
        " (",
        ", ".join(f'"{name}"' for name in names),
        ") VALUES (",
        ", ".join(f"${index}" for index in range(1, len(names) + 1)),
        ")",
    )
    await conn.executemany(
        sql,
        [tuple(row[index] for index in positions) for row in rows],
    )


def _prepare_row(
    item: ExportRow,
    columns: Sequence[ExportColumn],
) -> tuple[tuple[object, ...], tuple[UUID, UUID] | None, int]:
    """Validate row columns and convert JSON scalars to database values."""
    expected = {column.name for column in columns}
    actual = set(item.row)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RestoreError(
            f"line {item.line_number}: {item.table} columns differ from destination "
            f"(missing={missing!r}, extra={extra!r})",
        )

    row = dict(item.row)
    cause: tuple[UUID, UUID] | None = None
    if item.table == "change_log" and row["caused_by"] is not None:
        cause = (
            _uuid(row["id"], item, "id"),
            _uuid(row["caused_by"], item, "caused_by"),
        )
        row["caused_by"] = None

    redacted = 0
    for column in _AUTH_REFERENCE_COLUMNS.get(item.table, frozenset()):
        if row.get(column) is not None:
            row[column] = None
            redacted += 1

    values = tuple(
        _restore_value(column, row[column.name], item)
        for column in columns
    )
    return values, cause, redacted


def _restore_value(column: ExportColumn, value: object, item: ExportRow) -> object:
    """Convert the export's portable JSON spelling to an asyncpg value."""
    if value is None:
        return None
    try:
        if column.is_json:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if column.data_type == "uuid":
            return UUID(cast(str, value)) if isinstance(value, str) else value
        if column.data_type.startswith("timestamp"):
            return datetime.fromisoformat(cast(str, value))
        if column.data_type == "date":
            return date.fromisoformat(cast(str, value))
        if column.data_type == "xid8":
            return int(cast(str | int, value))
        if column.data_type == "ARRAY" and isinstance(value, list):
            element = column.udt_name.removeprefix("_")
            if element == "uuid":
                return [
                    UUID(cast(str, each)) if isinstance(each, str) else each
                    for each in value
                ]
            if element.startswith("timestamp"):
                return [datetime.fromisoformat(cast(str, each)) for each in value]
        return value
    except (TypeError, ValueError) as error:
        raise RestoreError(
            f"line {item.line_number}: invalid {column.name} value for "
            f"{column.data_type}: {value!r}",
        ) from error


def _uuid(value: object, item: ExportRow, column: str) -> UUID:
    """Parse a UUID while retaining the source line in the error."""
    if not isinstance(value, str):
        raise RestoreError(f"line {item.line_number}: {column} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as error:
        raise RestoreError(
            f"line {item.line_number}: {column} is not a UUID: {value!r}",
        ) from error


async def _restore_change_causes(
    conn: Conn,
    causes: Sequence[tuple[UUID, UUID]],
) -> None:
    """Restore self-referential audit links after every change row exists.

    The export normally contains every change row, but the database does not
    require ``caused_by`` to point at a row present in the file. Check the
    deferred parent IDs explicitly so a partial or malformed input cannot
    silently lose audit ancestry.
    """
    for start in range(0, len(causes), _BATCH_SIZE):
        batch = causes[start : start + _BATCH_SIZE]
        parent_ids = list(dict.fromkeys(item[1] for item in batch))
        found = {
            row["id"]
            for row in await conn.fetch(
                "SELECT id FROM change_log WHERE id = ANY($1::uuid[])",
                parent_ids,
            )
        }
        missing = [parent_id for parent_id in parent_ids if parent_id not in found]
        if missing:
            raise RestoreError(
                "change_log caused_by references missing rows: "
                f"{missing!r}",
            )
        await conn.execute(
            "UPDATE change_log AS target SET caused_by = source.caused_by "
            "FROM unnest($1::uuid[], $2::uuid[]) AS source(id, caused_by) "
            "WHERE target.id = source.id",
            [item[0] for item in batch],
            [item[1] for item in batch],
        )

