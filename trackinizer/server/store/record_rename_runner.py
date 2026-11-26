"""Rename three session record classes in stored rows, sharded, while the server serves.

A stored row names its class twice: in ``kind`` and in each ``py/object`` tag of
its payload. These classes were renamed:

  AgentToAgentMessage -> AgentMessage
  ToolResult          -> ToolMessage
  TurnContext         -> SessionContext

The payload is JSON, not JSONB, and is read back byte for byte, so the tags are
renamed as text: no other byte of a payload changes. A tag in ``py/dict`` data, as a
call's arguments, tags no record, and stays as the server serves it
(``session_records.served_payload``). ``text`` does not change, so
no row is restamped and nothing is embedded again. Running it again changes
nothing: no old name is left.

Not a numbered migration: it would run at startup, as downtime. It runs AFTER the
deploy, while the new server serves, which departs from
``docs/db_schema_migration.md`` Roadmap A step 5 ("A backfill runs BEFORE the
restart"). That step's precondition is a rewrite safe against the OLD code; this
one is not, since the old code reads only the old names, and running it before
would break the serving build. The new code reads both names (sagent's
``stored_names``), so it is the build this rewrite is safe against: deploy it
first, then run this. Until it ends, a row keeps its former kind, which every
reader takes.

Measured on the production database, read-only, 2026-11-20: ``session_records``
held 11,345,178 rows in 44 GB (11 GB heap); of a 0.05% TABLESAMPLE (5,921 rows)
3.7% matched the WHERE (3.2% by kind). ``schema.067.sql`` rewrote a like share
(4.3% of ~10M rows) in ~33 minutes on one connection. The scan the WHERE does,
timed over 1/16 of the key space: 131k rows/s on 1 connection, 368k on 4, 315k on
8, 403k on 16 (``/opt/scratch/artifacts/trackinizer/record_rename_scaling.txt``).
The write's scaling is not measured; each shard prints its rows and seconds.

Sharding: ``session_id`` ranges on the primary key's leading column, one per
shard, so each shard's scan is an index range. Each shard commits on its own; a
failed or cancelled run resumes by running again.

Maintenance run-hook::

    uv --quiet run --frozen python -m \
        trackinizer.server.store.record_rename_runner "$TRACKINIZER_DSN" 16

Arguments: the DSN and the shard count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import json
import re
import time

from trackinizer.sagent.journal.types import renamed_records


if TYPE_CHECKING:
    from trackinizer.lib.postgres import DatabaseEngine


__all__ = [
    "RenameStats",
    "rename_shard",
    "renamed_text",
]


# One scan finds each row a former name may tag; which of its tags name a record is
# worked out in Python (``renamed_text``), which no SQL pattern can say.
_CANDIDATES: Final = """
SELECT session_id, part, idx, kind, payload::text AS payload
FROM session_records
WHERE session_id >= $1 AND ($2::uuid IS NULL OR session_id < $2)
AND (kind = ANY($3::text[]) OR payload::text ~ $4)
FOR UPDATE
"""

# ``unnest`` arrays, not executemany: one round trip for the shard's rows.
_RENAME: Final = """
UPDATE session_records AS r
SET kind = t.kind, payload = t.payload::json
FROM unnest($1::uuid[], $2::int[], $3::int[], $4::text[], $5::text[])
    AS t(session_id, part, idx, kind, payload)
WHERE r.session_id = t.session_id AND r.part = t.part AND r.idx = t.idx
"""


# A tag as a stored row spells it, spaced any way; one in a string is escaped, so its
# quotes do not match.
_TAG: Final = re.compile(
    r'("py/object"\s*:\s*)"(' + "|".join(map(re.escape, renamed_records())) + ')"',
)
_PLAIN: Final = re.compile(r'"py/dict"\s*:\s*')


# ``py/dict`` holds plain data, as a call's arguments, which no tag in it makes a
# record: the server serves it as it is (``session_records.served_payload``).
def renamed_text(text: str) -> str:
    """Return a payload's text with each record's former class name renamed.

    Args:
      text: A stored payload, JSON.

    Returns:
      renamed: ``text`` with each tag of a renamed class, outside ``py/dict``
        data, naming its new name; no other byte changes.

    """
    decoder = json.JSONDecoder()
    parts: list[str] = []
    at = 0
    # Each search starts past the data before it, so one nested in it is never found.
    while (plain := _PLAIN.search(text, at)) is not None:
        end = decoder.raw_decode(text, plain.end())[1]
        parts += [
            _TAG.sub(_renamed_tag, text[at : plain.end()]),
            text[plain.end() : end],
        ]
        at = end
    parts.append(_TAG.sub(_renamed_tag, text[at:]))
    return "".join(parts)


@dataclass(frozen=True, slots=True, kw_only=True)
class RenameStats:
    """What one shard did.

    Attributes:
      rows: Rows rewritten.
      seconds: How long its statement ran.

    """

    rows: int
    seconds: float


# Audience: internal -- an operator tool run on the server; no caller sees it.
async def rename_shard(
    engine: DatabaseEngine,
    *,
    shards: int = 1,
    shard: int = 0,
) -> RenameStats:
    """Rename the old class names in the rows of one ``session_id`` range.

    Args:
      engine: The store's engine; one connection per call.
      shards: Total shard count across concurrent callers.
      shard: This caller's shard, in ``[0, shards)``; the last runs to the end.

    Returns:
      stats: The rows it rewrote, and how long it took.

    Raises:
      ValueError: ``shard`` is outside ``[0, shards)``.

    """
    if shard < 0 or shard >= shards:
        raise ValueError(f"shard {shard} outside [0, {shards})")
    # The range splits the 128-bit key space evenly; uuid4 ids spread across it.
    start = UUID(int=(shard << 128) // shards)
    end = None if shard == shards - 1 else UUID(int=((shard + 1) << 128) // shards)
    names = renamed_records()
    began = time.monotonic()
    changed: list[tuple[object, object, object, str, str]] = []
    async with engine.acquire() as conn, conn.transaction():
        for r in await conn.fetch(_CANDIDATES, start, end, list(names), _TAG.pattern):
            kind, payload = cast(str, r["kind"]), cast(str, r["payload"])
            renamed = (names.get(kind, kind), renamed_text(payload))
            if renamed != (kind, payload):
                changed.append((r["session_id"], r["part"], r["idx"], *renamed))
        if changed:
            _ = await conn.execute(_RENAME, *map(list, zip(*changed, strict=True)))
    return RenameStats(rows=len(changed), seconds=time.monotonic() - began)


def _renamed_tag(found: re.Match[str]) -> str:
    """Return the tag ``found`` matched, naming the class's new name."""
    return f'{found.group(1)}"{renamed_records()[found.group(2)]}"'


if __name__ == "__main__":
    import asyncio
    import sys

    from trackinizer.lib.postgres import PostgresEngine
    from trackinizer.server.notify import NOTIFY_CHANNEL

    async def _run(dsn: str, shards: int) -> None:
        async with PostgresEngine(
            dsn=dsn,
            listen_channel=NOTIFY_CHANNEL,
            # One pooled connection per shard, or gather serializes on the
            # default pool cap and the fan-out buys nothing.
            max_size=shards,
        ) as engine:
            results = await asyncio.gather(
                *(
                    rename_shard(engine, shards=shards, shard=shard)
                    for shard in range(shards)
                ),
            )
        for shard, stats in enumerate(results):
            print(f"shard {shard}: {stats}")  # noqa: T201 -- Run-hook output.

    asyncio.run(_run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 16))
