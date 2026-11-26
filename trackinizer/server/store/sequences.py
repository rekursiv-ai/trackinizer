"""Shared sequence maintenance for the inquiry store."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.schema_gen import SEQ_FOR_KIND
from trackinizer.server.values import vetted_sql


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = ["_reconcile_sequences"]


async def _reconcile_sequences(conn: Conn) -> None:
    """Advance each per-kind ref sequence to the maximum ``seq`` in rows.

    The update is monotonic: an already healthy sequence is not lowered, while
    a sequence lagging behind literal rows restored from a logical export is
    advanced so the next normal write cannot reuse an existing reference.
    """
    for kind, sequence in SEQ_FOR_KIND.items():
        max_seq = await conn.fetchval(
            "SELECT MAX(seq) FROM inquiries WHERE kind = $1",
            kind,
        )
        if max_seq is None:
            continue
        await conn.execute(
            vetted_sql(
                "SELECT setval('",
                sequence,
                "', GREATEST((SELECT last_value FROM ",
                sequence,
                "), $1))",
            ),
            max_seq,
        )
