"""Read and validate a Trackinizer logical export for restoration.

The export endpoint writes a deliberately small, line-oriented contract. This
module owns the file boundary for restore: it understands plain JSONL and gzip
JSONL, validates the document-level invariants, and yields rows without holding
the file in memory. Database validation belongs to ``store.restore`` because it
needs the destination catalog and transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, TextIO

import gzip
import json

from trackinizer.wire.wire_export import (
    EXPORT_FORMAT,
    EXPORT_TABLES,
    EXPORT_VERSION,
)


if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = [
    "ExportHeader",
    "ExportRow",
    "GraphExportReader",
    "RestoreError",
]


class RestoreError(ValueError):
    """The input is not a restorable logical export."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportHeader:
    """Document metadata from the first export line."""

    migrations: tuple[str, ...]
    audiences: tuple[str, ...]
    withheld_sessions: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportRow:
    """One table row from a logical export."""

    table: str
    row: dict[str, object]
    line_number: int


class GraphExportReader:
    """Stream and validate a complete logical export file.

    The reader validates format-level invariants while yielding. It does not
    decide whether the destination database can accept a row; that is the
    Store's responsibility.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def __iter__(self) -> Iterator[ExportHeader | ExportRow]:
        """Yield the header followed by rows in export order."""
        opener = gzip.open if self.path.suffix == ".gz" else open
        try:
            with opener(self.path, "rt", encoding="utf-8") as stream:
                yield from self._read(stream)
        except OSError as error:
            raise RestoreError(f"cannot read {self.path}: {error}") from error

    def _read(self, stream: TextIO) -> Iterator[ExportHeader | ExportRow]:
        """Parse a text stream, keeping line numbers in all failures."""
        iterator = iter(stream)
        try:
            first = next(iterator)
        except StopIteration:
            raise RestoreError("export is empty; expected a header line") from None
        header = self._header(first, 1)
        yield header

        previous_index = -1
        for line_number, raw in enumerate(iterator, start=2):
            if not raw.strip():
                raise RestoreError(f"line {line_number}: blank lines are not allowed")
            payload = self._json(raw, line_number)
            if not isinstance(payload, dict):
                raise RestoreError(f"line {line_number}: expected a JSON object")
            table = payload.get("table")
            row = payload.get("row")
            if not isinstance(table, str) or not isinstance(row, dict):
                raise RestoreError(
                    f"line {line_number}: expected {{\"table\", \"row\"}}",
                )
            try:
                table_index = EXPORT_TABLES.index(table)
            except ValueError:
                raise RestoreError(
                    f"line {line_number}: unknown export table {table!r}",
                ) from None
            if table_index < previous_index:
                raise RestoreError(
                    f"line {line_number}: table {table!r} is out of export order",
                )
            previous_index = table_index
            yield ExportRow(
                table=table,
                row=dict(row),
                line_number=line_number,
            )

    @staticmethod
    def _json(raw: str, line_number: int) -> object:
        """Decode one JSON line with a restore-specific diagnostic."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise RestoreError(
                f"line {line_number}: invalid JSON at column {error.colno}: "
                f"{error.msg}",
            ) from error

    @classmethod
    def _header(cls, raw: str, line_number: int) -> ExportHeader:
        """Validate the header and reject partial exports up front."""
        payload = cls._json(raw, line_number)
        if not isinstance(payload, dict):
            raise RestoreError("line 1: expected the export header object")
        if payload.get("format") != EXPORT_FORMAT:
            raise RestoreError(
                f"line 1: unsupported export format {payload.get('format')!r}",
            )
        if payload.get("version") != EXPORT_VERSION:
            raise RestoreError(
                f"line 1: unsupported export version {payload.get('version')!r}",
            )
        if "selector" in payload:
            raise RestoreError("line 1: filtered exports cannot be restored")
        migrations = cls._string_tuple(payload, "migrations")
        if not migrations or migrations[0] != "schema.sql":
            raise RestoreError("line 1: migrations must begin with 'schema.sql'")
        audiences = cls._string_tuple(payload, "audiences")
        withheld = payload.get("withheld_sessions")
        if type(withheld) is not int or withheld < 0:
            raise RestoreError(
                "line 1: withheld_sessions must be a non-negative integer",
            )
        if withheld:
            raise RestoreError(
                "line 1: exports with withheld sessions are not complete restores",
            )
        return ExportHeader(
            migrations=migrations,
            audiences=audiences,
            withheld_sessions=withheld,
        )

    @staticmethod
    def _string_tuple(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
        """Read a required JSON array of strings."""
        value = payload.get(name)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RestoreError(f"line 1: {name} must be an array of strings")
        return tuple(value)
