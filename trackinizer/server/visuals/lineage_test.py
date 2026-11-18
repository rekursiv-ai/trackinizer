"""Section-bound cursors and distinct-peer quotas for research lineage."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from trackinizer.server.visuals.lineage import (
    decode_cursor,
    encode_cursor,
    load_lineage_page,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


def test_cursor_round_trip_preserves_timestamp_and_uuid_tie_break() -> None:
    created = datetime(2026, 1, 1, 0, 0, 0, 123_456, tzinfo=UTC)
    record_id = UUID(int=12)
    cursor = encode_cursor(
        "target|narrows,produced_by|in|Issue",
        created=created,
        record_id=record_id,
    )
    assert decode_cursor(cursor, context="target|narrows,produced_by|in|Issue") == (
        created,
        record_id,
    )
    assert decode_cursor("", context="another") == (None, None)


@pytest.mark.parametrize("cursor", ["bad", "not-base64", "====", "aGVsbG8="])
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(ValueError, match=r"^Invalid lineage cursor\.$"):
        decode_cursor(cursor, context="target|produced_by|in|")


def test_a_cursor_cannot_change_section_or_use_a_naive_timestamp() -> None:
    cursor = encode_cursor(
        "one",
        created=datetime(2026, 1, 1, tzinfo=UTC),
        record_id=UUID(int=1),
    )
    with pytest.raises(
        ValueError,
        match=r"^Lineage cursor does not match this section\.$",
    ):
        decode_cursor(cursor, context="two")
    naive = encode_cursor(
        "one",
        created=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        record_id=UUID(int=1),
    )
    with pytest.raises(
        ValueError,
        match=r"^Lineage cursor does not match this section\.$",
    ):
        decode_cursor(naive, context="one")


def test_cursor_uses_url_safe_alphabet_and_rejects_embedded_garbage() -> None:
    context = "࿿|target"
    created = datetime(2026, 1, 1, tzinfo=UTC)
    cursor = encode_cursor(context, created=created, record_id=UUID(int=1))
    assert "-" in cursor
    assert "_" in cursor
    assert decode_cursor(cursor, context=context) == (created, UUID(int=1))
    with pytest.raises(ValueError, match=r"^Invalid lineage cursor\.$"):
        decode_cursor(f"!{cursor}", context=context)


@pytest.mark.asyncio
async def test_a_page_pins_the_target_and_limits_peers_before_loading_their_edges() -> (
    None
):
    conn = AsyncMock()
    rows = [_row(index) for index in range(4)]
    conn.fetchrow.return_value = rows[0]
    conn.fetch.side_effect = [
        rows[1:],
        [
            {
                "from_id": UUID(int=1),
                "to_id": UUID(int=2),
                "edge_kind": "produced_by",
                "note": None,
                "valence": None,
            },
        ],
    ]
    page = await load_lineage_page(
        cast("Conn", conn),
        target_id=UUID(int=1),
        page_size=2,
    )
    assert page is not None
    assert page["records"] == [{**row, "id": str(row["id"])} for row in rows[1:3]]
    assert page["target"] == {**rows[0], "id": str(UUID(int=1))}
    assert isinstance(page["next_cursor"], str)
    assert decode_cursor(
        page["next_cursor"],
        context=f"{UUID(int=1)}|narrows,produced_by|in|",
    ) == (rows[2]["created"], rows[2]["id"])
    assert conn.fetch.call_args.args[3] == [UUID(int=2), UUID(int=3)]


@pytest.mark.asyncio
async def test_an_empty_section_does_not_query_edges_and_a_missing_record_is_none() -> (
    None
):
    conn = AsyncMock()
    conn.fetchrow.return_value = _row(0)
    conn.fetch.return_value = list[dict[str, object]]()
    page = await load_lineage_page(
        cast("Conn", conn),
        target_id=UUID(int=1),
        direction="out",
    )
    assert page is not None
    assert page["records"] == page["edges"] == []
    assert page["next_cursor"] is None
    assert conn.fetch.call_count == 1
    conn.fetchrow.return_value = None
    assert await load_lineage_page(cast("Conn", conn), target_id=UUID(int=1)) is None
    with pytest.raises(ValueError, match="Page size"):
        await load_lineage_page(cast("Conn", conn), target_id=UUID(int=1), page_size=51)


def _row(index: int) -> dict[str, object]:
    return {
        "id": UUID(int=index + 1),
        "kind": "Issue",
        "seq": index + 1,
        "title": f"Question {index}",
        "status": "active",
        "created": datetime(2026, 1, 1, tzinfo=UTC),
        "modified": datetime(2026, 1, 1, tzinfo=UTC),
        "description": None,
        "outcome": None,
        "labels": None,
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
