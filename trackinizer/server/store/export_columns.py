"""Catalog metadata shared by logical export and restore."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from trackinizer.wire.wire_export import EXPORT_TABLES


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = ["ExportColumn", "exported_columns"]


_SKIPPED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    # A generated tsvector; the source text is portable and the index rebuilds.
    "session_records": frozenset({"search"}),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportColumn:
    """One portable table column and its database type metadata."""

    name: str
    data_type: str
    udt_name: str

    @property
    def is_json(self) -> bool:
        """Whether the column uses PostgreSQL's text-encoded JSON type."""
        return self.data_type == "json"


async def exported_columns(conn: Conn) -> dict[str, tuple[ExportColumn, ...]]:
    """Return portable columns in catalog order for every exported table."""
    rows = await conn.fetch(
        "SELECT table_name, column_name, data_type, udt_name "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[]) "
        "ORDER BY table_name, ordinal_position",
        list(EXPORT_TABLES),
    )
    columns: dict[str, list[ExportColumn]] = {table: [] for table in EXPORT_TABLES}
    for row in rows:
        table = str(row["table_name"])
        column = str(row["column_name"])
        if column in _SKIPPED_COLUMNS.get(table, frozenset()):
            continue
        columns[table].append(
            ExportColumn(
                name=column,
                data_type=str(row["data_type"]),
                # Older test doubles and old catalogs may not expose udt_name;
                # conversion only needs it for array element typing.
                udt_name=str(row.get("udt_name", "")),
            ),
        )
    return {table: tuple(cols) for table, cols in columns.items()}
