from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import UUID

import gzip
import json

import pytest

from trackinizer.server.restore import (
    ExportHeader,
    ExportRow,
    GraphExportReader,
    RestoreError,
)
from trackinizer.server.store.export_columns import ExportColumn
from trackinizer.server.store.restore import (
    _prepare_row,
    _restore_change_causes,
    _restore_value,
)


if TYPE_CHECKING:
    from pathlib import Path


def _header(**overrides: object) -> str:
    value: dict[str, object] = {
        "format": "trackinizer-export",
        "version": 1,
        "migrations": ["schema.sql"],
        "audiences": ["open"],
        "withheld_sessions": 0,
    }
    value.update(overrides)
    return json.dumps(value)


def _write(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_reader_yields_header_and_rows_in_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "graph.jsonl",
        _header(),
        '{"table":"inquiries","row":{"id":"one"}}',
        '{"table":"edges","row":{"from_id":"one"}}',
    )

    items = list(GraphExportReader(path))

    assert items == [
        ExportHeader(
            migrations=("schema.sql",),
            audiences=("open",),
            withheld_sessions=0,
        ),
        ExportRow(
            table="inquiries",
            row={"id": "one"},
            line_number=2,
        ),
        ExportRow(
            table="edges",
            row={"from_id": "one"},
            line_number=3,
        ),
    ]


def test_reader_supports_gzip(tmp_path: Path) -> None:
    path = tmp_path / "graph.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(_header() + "\n")

    assert isinstance(next(iter(GraphExportReader(path))), ExportHeader)


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (_header(selector=[]), "filtered exports"),
        (_header(withheld_sessions=1), "withheld sessions"),
    ],
)
def test_reader_rejects_non_restorable_headers(
    tmp_path: Path,
    header: str,
    message: str,
) -> None:
    path = _write(tmp_path / "bad.jsonl", header)

    with pytest.raises(RestoreError, match=message):
        list(GraphExportReader(path))


def test_reader_rejects_rows_out_of_table_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad.jsonl",
        _header(),
        '{"table":"edges","row":{}}',
        '{"table":"inquiries","row":{}}',
    )

    with pytest.raises(RestoreError, match="out of export order"):
        list(GraphExportReader(path))


def test_restore_redacts_unavailable_auth_and_defers_audit_cause() -> None:
    """Restore keeps audit identity while removing credentials absent from export."""
    columns = tuple(
        ExportColumn(name=name, data_type=data_type, udt_name=udt_name)
        for name, data_type, udt_name in (
            ("id", "uuid", "uuid"),
            ("caused_by", "uuid", "uuid"),
            ("api_key_id", "uuid", "uuid"),
            ("actor", "text", "text"),
        )
    )
    item = ExportRow(
        table="change_log",
        row={
            "id": "00000000-0000-0000-0000-000000000001",
            "caused_by": "00000000-0000-0000-0000-000000000002",
            "api_key_id": "00000000-0000-0000-0000-000000000003",
            "actor": "agent@example.com",
        },
        line_number=7,
    )

    values, cause, redacted = _prepare_row(item, columns)

    assert values == (
        UUID("00000000-0000-0000-0000-000000000001"),
        None,
        None,
        "agent@example.com",
    )
    assert cause == (
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("00000000-0000-0000-0000-000000000002"),
    )
    assert redacted == 1


def test_restore_converts_portable_scalar_and_array_values() -> None:
    """JSONL spellings become values accepted by the configured asyncpg codecs."""
    item = ExportRow(table="inquiries", row={}, line_number=3)

    assert _restore_value(
        ExportColumn(name="meta", data_type="json", udt_name="json"),
        {"answer": [42]},
        item,
    ) == '{"answer":[42]}'
    assert _restore_value(
        ExportColumn(name="id", data_type="uuid", udt_name="uuid"),
        "00000000-0000-0000-0000-000000000001",
        item,
    ) == UUID("00000000-0000-0000-0000-000000000001")
    assert _restore_value(
        ExportColumn(
            name="created",
            data_type="timestamp with time zone",
            udt_name="timestamptz",
        ),
        "2026-11-25T12:30:00+00:00",
        item,
    ) == datetime(2026, 11, 25, 12, 30, tzinfo=UTC)
    assert _restore_value(
        ExportColumn(name="refs", data_type="ARRAY", udt_name="_uuid"),
        ["00000000-0000-0000-0000-000000000001"],
        item,
    ) == [UUID("00000000-0000-0000-0000-000000000001")]


@pytest.mark.asyncio
async def test_restore_rejects_missing_audit_cause() -> None:
    """A missing deferred parent cannot silently erase audit ancestry."""
    conn = AsyncMock()
    conn.fetch.return_value = []

    with pytest.raises(RestoreError, match="caused_by references missing rows"):
        await _restore_change_causes(
            conn,
            [
                (
                    UUID("00000000-0000-0000-0000-000000000001"),
                    UUID("00000000-0000-0000-0000-000000000002"),
                ),
            ],
        )

    conn.execute.assert_not_awaited()
