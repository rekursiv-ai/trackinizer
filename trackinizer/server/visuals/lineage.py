"""Read bounded typed relationship pages for historical and launched research."""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.types.edges import Edge
    from trackinizer.types.inquiries import Inquiry


async def load_lineage_page(
    conn: Conn,
    *,
    target_id: UUID,
    edge_kinds: tuple[Edge.Kind, ...] = ("narrows", "produced_by"),
    direction: Literal["in", "out"] = "in",
    peer_kind: Inquiry.InquiryKind | None = None,
    page_size: int = 20,
    cursor: str = "",
) -> dict[str, object] | None:
    """Read distinct peers and their typed edges without choosing an ancestry.

    Args:
      conn: The caller's database connection.
      target_id: Record that remains selected, independent of kind or labels.
      edge_kinds: Stored relationship kinds to read together.
      direction: Incoming edges point to the target; outgoing edges start at it.
      peer_kind: Restrict peers to one inquiry kind, or include every kind.
      page_size: Distinct peer quota, between one and fifty.
      cursor: Continuation bound to the record and relationship filters.

    Returns:
      page: Target, distinct peers, typed edges and next cursor, or None when
        the target does not exist.

    Raises:
      ValueError: A cursor or page size is invalid.

    """
    if page_size < 1 or page_size > 50:
        raise ValueError("Page size must be between 1 and 50.")
    context = "|".join(
        (str(target_id), ",".join(sorted(set(edge_kinds))), direction, peer_kind or ""),
    )
    after_created, after_id = decode_cursor(cursor, context=context)
    target = await conn.fetchrow(
        "SELECT i.id,i.kind,i.seq,LEFT(i.title,2000) AS title,i.status,i.created,"
        "i.modified,LEFT(i.description,2000) AS description,"
        "LEFT(i.experiment_outcome,2000) AS outcome,i.labels FROM inquiries i WHERE i.id=$1",
        target_id,
    )
    if target is None:
        return None
    rows = await conn.fetch(
        "SELECT DISTINCT i.id,i.kind,i.seq,LEFT(i.title,2000) AS title,i.status,"
        "i.created,i.modified,LEFT(i.description,2000) AS description,"
        "LEFT(i.experiment_outcome,2000) AS outcome,i.labels FROM edges e "
        "JOIN inquiries i ON i.id=CASE WHEN $7::text='in' THEN e.from_id ELSE e.to_id END "
        "WHERE (($7='in' AND e.to_id=$1) OR ($7='out' AND e.from_id=$1)) "
        "AND e.edge_kind=ANY($2::text[]) "
        "AND ($3::text IS NULL OR i.kind=$3) "
        "AND ($4::timestamptz IS NULL OR (i.created,i.id)>($4,$5::uuid)) "
        "ORDER BY i.created,i.id LIMIT $6",
        target_id,
        list(edge_kinds),
        peer_kind,
        after_created,
        after_id,
        page_size + 1,
        direction,
    )
    selected = rows[:page_size]
    peer_ids = [UUID(str(row["id"])) for row in selected]
    edges = []
    if peer_ids:
        edges = await conn.fetch(
            "SELECT e.from_id,e.to_id,e.edge_kind,e.valence,LEFT(e.note,2000) AS note "
            "FROM edges e WHERE e.edge_kind=ANY($2::text[]) AND "
            "(($4::text='in' AND e.to_id=$1 AND e.from_id=ANY($3::uuid[])) OR "
            "($4='out' AND e.from_id=$1 AND e.to_id=ANY($3::uuid[]))) "
            "ORDER BY e.from_id,e.edge_kind,e.to_id",
            target_id,
            list(edge_kinds),
            peer_ids,
            direction,
        )
    next_cursor = None
    if len(rows) > page_size:
        last = selected[-1]
        created = last["created"]
        assert isinstance(created, datetime)
        next_cursor = encode_cursor(
            context,
            created=created,
            record_id=UUID(str(last["id"])),
        )
    return {
        "target": _record(target),
        "records": [_record(row) for row in selected],
        "edges": [
            {
                "from_id": str(edge["from_id"]),
                "to_id": str(edge["to_id"]),
                "edge_kind": edge["edge_kind"],
                "valence": edge["valence"],
                "note": edge["note"],
            }
            for edge in edges
        ],
        "next_cursor": next_cursor,
    }


def encode_cursor(context: str, *, created: datetime, record_id: UUID) -> str:
    """Encode a section-bound timestamp and UUID tie-break position."""
    return urlsafe_b64encode(
        f"{context}|{created.isoformat()}|{record_id}".encode(),
    ).decode()


def decode_cursor(cursor: str, *, context: str) -> tuple[datetime | None, UUID | None]:
    """Reject malformed positions and cursors from another relationship section.

    Args:
      cursor: Encoded continuation, or empty for the first page.
      context: Target and filters that must match the cursor's original section.

    Returns:
      created: Filing timestamp, or None for the first page.
      after_id: UUID tie-break position, or None for the first page.

    """
    if not cursor:
        return None, None
    try:
        prefix, timestamp, record_id = (
            b64decode(cursor, altchars=b"-_", validate=True).decode().rsplit("|", 2)
        )
        created = datetime.fromisoformat(timestamp)
        after_id = UUID(record_id)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid lineage cursor.") from error
    if prefix != context or created.tzinfo is None:
        raise ValueError("Lineage cursor does not match this section.")
    return created, after_id


def _record(row: object) -> dict[str, object]:
    values = cast(Mapping[str, object], row)
    return {
        "id": str(values["id"]),
        "kind": values["kind"],
        "seq": values["seq"],
        "title": values["title"],
        "status": values["status"],
        "created": values["created"],
        "modified": values["modified"],
        "description": values["description"],
        "outcome": values["outcome"],
        "labels": values.get("labels"),
    }
