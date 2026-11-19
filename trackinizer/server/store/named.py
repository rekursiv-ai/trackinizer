"""Records named by id or by kind and number, read as one reader may read them.

A route, a focus or a visual names a record by its UUID or by ``Kind/seq``, the
form a page's address takes (``#/ref/Issue/3``); a number is a
:class:`~trackinizer.wire.refs.SeqRef`, as the client and the CLI hold one.
This reads the ones that exist and the reader may read, in one query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import re
import uuid

from trackinizer.server.access_policy import inquiry_read_filter
from trackinizer.server.values import vetted_sql
from trackinizer.types.inquiries import KIND_TO_CLASS
from trackinizer.wire.refs import SeqRef
from trackinizer.wire.wire_sessions import CanvasRecordContext


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import AccessPolicy, Principal
    from trackinizer.types.inquiries import Inquiry


__all__ = ["Named", "parse_named", "read_named"]


type Named = uuid.UUID | SeqRef
"""A record named by its id or by its kind and per-kind number."""


def parse_named(text: str) -> Named | None:
    """Return the record ``text`` names, a UUID or ``Kind/seq``, or None."""
    return _uuid(text) or _seq(text)


async def read_named(
    conn: Conn,
    *,
    wanted: Sequence[Named],
    policy: AccessPolicy | None = None,
    principal: Principal | None = None,
) -> dict[Named, CanvasRecordContext]:
    """Read the records ``wanted`` names that exist and ``principal`` may read.

    Args:
      conn: Connection to read with.
      wanted: Records by id or by kind and number.
      policy: Deployment policy; None preserves trusted reads.
      principal: Request reader; None preserves trusted reads.

    Returns:
      found: Each record read, under both its id and its kind and number; a name
        that is missing or unreadable is absent, so neither says it exists.

    """
    if not wanted:
        return {}
    seqs: dict[tuple[str, int], SeqRef] = {
        (each.kind, each.seq): each for each in wanted if isinstance(each, SeqRef)
    }
    params: list[object] = [
        [each for each in wanted if isinstance(each, uuid.UUID)],
        [kind for kind, _seq in seqs],
        [seq for _kind, seq in seqs],
    ]
    predicate = inquiry_read_filter(
        "inquiries.id",
        policy=policy,
        principal=principal,
        params=params,
    )
    found: dict[Named, CanvasRecordContext] = {}
    for row in await conn.fetch(
        vetted_sql(
            "SELECT id, kind, seq, title FROM inquiries "
            "WHERE (id = ANY($1::uuid[]) "
            "OR (kind, seq) IN (SELECT * FROM unnest($2::text[], $3::bigint[])))",
            "" if predicate == "TRUE" else f" AND ({predicate})",
        ),
        *params,
    ):
        record = CanvasRecordContext.model_validate(
            {**dict(row), "title": str(row["title"])[:512]},
        )
        found[record.id] = record
        if (asked := seqs.get((record.kind, record.seq))) is not None:
            found[asked] = record
    return found


def _uuid(text: str) -> uuid.UUID | None:
    """Return the id ``text`` spells, or None."""
    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text):
        return uuid.UUID(text)
    return None


def _seq(text: str) -> SeqRef | None:
    """Return the kind, as the server spells it, and number of ``Kind/seq``, or None."""
    named = re.fullmatch(r"([^/]+)/([0-9]{1,15})", text)
    if named is None:
        return None
    kinds: dict[str, Inquiry.InquiryKind] = {
        each.lower(): each for each in KIND_TO_CLASS
    }
    spelled = kinds.get(named[1].lower())
    return None if spelled is None else SeqRef(kind=spelled, seq=int(named[2]))
