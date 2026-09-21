"""Offload heavy record bodies to ``session_bodies``; splice back on replay.

The cold half of the hot/cold split (``docs/private/session_indexing.md``):
tool-result and file bodies are ~90% of ``session_records``' content but are
read only by replay, so they move off the hot row into the zstd sidecar and
the hot row keeps a searchable head.

Which kinds offload is the :data:`HEAVY_KINDS` policy here -- deliberately
the same set the :class:`~trackinizer.server.semantic_mapper_footprint.
FootprintMapper` heads plus the never-indexed body kinds, because "body is
cold" and "only the head is searchable" are one decision, not two.

The hot row after offload:

- ``payload`` becomes :data:`STUB_PAYLOAD` -- a fixed marker object whose
  presence tells the read path to splice. It is JSON so ``payload::jsonb``
  probes still work, and fixed so the stub compresses to nothing.
- ``text`` becomes its first :data:`HEAD_TEXT_CHARS` characters, which is
  what the tsvector then indexes -- the 1.7 GB GIN shrinks to the heads.

Splice contract: ``read_session_records(plaintext_only=False)`` COALESCEs the
sidecar over the stub exactly as it splices ciphertext, so replay is
byte-exact; ``plaintext_only=True`` readers (search, feed) never touch the
sidecar. A record and its body write in ONE transaction -- the invariant the
ciphertext write already keeps ("a record can never be readable while its
ciphertext is missing", ``session_ir.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from trackinizer.lib import zstd_compat
from trackinizer.server.notify import tx


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine


__all__ = [
    "HEAD_TEXT_CHARS",
    "HEAVY_KINDS",
    "STUB_PAYLOAD",
    "OffloadStats",
    "offload_session_bodies",
    "spliced_payload",
]


HEAVY_KINDS: Final = frozenset(
    {
        "ShellCommandResult",
        "UncategorizedToolResult",
        "FileReadResult",
        "FileWriteResult",
        "FileEditResult",
    },
)
"""Kinds whose bodies live in the sidecar. Messages/tool-call args stay hot
(measured 2026-09-19: all messages together are ~520 MB against ~11 GB of
these bodies)."""

HEAD_TEXT_CHARS: Final = 2_000
"""Head kept hot for search/feed, matching ``FootprintMapper``'s
``HEAD_CHARS`` -- the two constants agree by test, not by import, so the
mapper stays policy-swappable without dragging storage behavior with it."""

STUB_PAYLOAD: Final = '{"$body":"offloaded"}'
"""The hot ``payload`` after offload, verbatim JSON text. Fixed-string so
detection is equality, not parsing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OffloadStats:
    """What one offload pass did.

    Attributes:
      records_offloaded: Rows whose bodies moved to the sidecar.
      bytes_before: Uncompressed payload+text bytes moved.
      bytes_after: Compressed sidecar bytes written.

    """

    records_offloaded: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


async def offload_session_bodies(
    engine: DatabaseEngine,
    *,
    shards: int = 1,
    shard: int = 0,
    batch: int = 200,
) -> OffloadStats:
    """Move every heavy body not yet offloaded into the sidecar.

    Idempotent by content: a row whose ``payload`` is already
    :data:`STUB_PAYLOAD` is skipped, so a cancelled run resumes by re-running
    -- the retype runner's recovery discipline.

    Args:
      engine: The store's engine.
      shards: Total shard count across concurrent callers.
      shard: This caller's shard, in ``[0, shards)``.
      batch: Rows per transaction; each batch offloads atomically.

    Returns:
      stats: Aggregated over the shard.

    """
    if shard < 0 or shard >= shards:
        raise ValueError(f"shard {shard} outside [0, {shards})")
    offloaded = before = after = 0
    async with engine.acquire() as conn:
        while True:
            batch_stats = await _offload_batch(conn, shards, shard, batch)
            if batch_stats.records_offloaded == 0:
                break
            offloaded += batch_stats.records_offloaded
            before += batch_stats.bytes_before
            after += batch_stats.bytes_after
    return OffloadStats(
        records_offloaded=offloaded,
        bytes_before=before,
        bytes_after=after,
    )


def decode_body(body_zst: bytes) -> str:
    """Decompress and UTF-8-decode a sidecar body; corrupt bytes are a ValueError.

    The stored body is always the UTF-8 JSON/text the offload wrote, so a decode
    failure is a corrupt row -- surfaced as a handled ValueError, never a bare
    ``UnicodeDecodeError`` bubbling to a 500.

    Args:
      body_zst: The zstd-compressed sidecar bytes.

    Returns:
      text: The decompressed UTF-8 text.

    Raises:
      ValueError: The decompressed bytes are not valid UTF-8.

    """
    try:
        return zstd_compat.decompress(body_zst).decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError(f"offloaded body is not valid UTF-8: {err}") from err


def spliced_payload(payload_text: str, body_zst: bytes | None) -> str:
    """Return the replay payload: the sidecar body when offloaded, else hot.

    The read-path helper ``read_session_records`` applies after its LEFT
    JOIN: a row whose ``payload`` is :data:`STUB_PAYLOAD` MUST have a sidecar
    row, and serving the stub as conversation would hand the CLI a transcript
    that never existed.

    Args:
      payload_text: The hot row's ``payload`` column text.
      body_zst: The joined ``session_bodies.payload_zst``, or ``None``.

    Returns:
      payload: The original payload JSON text, byte-exact.

    Raises:
      ValueError: The payload is the stub but no body row exists (a torn
        offload the one-transaction write forbids), or the sidecar body is not
        valid UTF-8 (a corrupt row, surfaced as a handled fault not a 500).

    """
    if payload_text != STUB_PAYLOAD:
        return payload_text
    if body_zst is None:
        raise ValueError(
            "payload is offloaded but its body row is missing; "
            "the one-transaction offload forbids this state",
        )
    return decode_body(body_zst)


# Batches claim rows with ``FOR UPDATE SKIP LOCKED`` so concurrent shards (or a
# sweep racing live ingest) never deadlock on the same record; the shard
# predicate makes overlap rare, the lock makes it harmless.
async def _offload_batch(
    conn: Conn,
    shards: int,
    shard: int,
    batch: int,
) -> OffloadStats:
    """Offload up to ``batch`` heavy rows in one transaction."""
    async with tx(conn):
        rows = await conn.fetch(
            # ``payload::text`` -- the column is ``json``, which has no ``<>``
            # operator; the text form compares byte-exactly, which is the
            # stub-detection contract.
            # ``& 2147483647`` (mask the sign bit), NOT ``abs()``: hashtext can
            # return INT_MIN (-2147483648), whose abs() overflows int4 and raises
            # "integer out of range". Masking is a total function and gives the
            # same uniform shard spread.
            "SELECT session_id, part, idx, payload, text FROM session_records "
            "WHERE kind = ANY($1::text[]) AND payload::text <> $2 "
            "AND (hashtext(session_id::text) & 2147483647) % $3 = $4 "
            "ORDER BY session_id, part, idx "
            "LIMIT $5 FOR UPDATE SKIP LOCKED",
            sorted(HEAVY_KINDS),
            STUB_PAYLOAD,
            shards,
            shard,
            batch,
        )
        if not rows:
            return OffloadStats()
        session_ids: list[UUID] = []
        parts: list[int] = []
        idxs: list[int] = []
        payloads: list[bytes] = []
        texts: list[bytes] = []
        heads: list[str] = []
        bytes_before = 0
        for row in rows:
            session_id = row["session_id"]
            assert isinstance(session_id, UUID)
            part = row["part"]
            assert isinstance(part, int)
            idx = row["idx"]
            assert isinstance(idx, int)
            payload = row["payload"]
            assert isinstance(payload, str)
            text = row["text"]
            assert isinstance(text, str)
            encoded_payload = payload.encode()
            encoded_text = text.encode()
            bytes_before += len(encoded_payload) + len(encoded_text)
            session_ids.append(session_id)
            parts.append(part)
            idxs.append(idx)
            payloads.append(zstd_compat.compress(encoded_payload))
            texts.append(zstd_compat.compress(encoded_text))
            heads.append(text[:HEAD_TEXT_CHARS])
        await conn.execute(
            "INSERT INTO session_bodies (session_id, part, idx, payload_zst, "
            "text_zst) "
            "SELECT * FROM unnest($1::uuid[], $2::int[], $3::int[], "
            "$4::bytea[], $5::bytea[]) "
            "ON CONFLICT (session_id, part, idx) DO UPDATE SET "
            "payload_zst = EXCLUDED.payload_zst, text_zst = EXCLUDED.text_zst",
            session_ids,
            parts,
            idxs,
            payloads,
            texts,
        )
        await conn.execute(
            "UPDATE session_records r SET payload = $4::json, text = t.head "
            "FROM unnest($1::uuid[], $2::int[], $3::int[], $5::text[]) "
            "AS t(session_id, part, idx, head) "
            "WHERE r.session_id = t.session_id AND r.part = t.part "
            "AND r.idx = t.idx",
            session_ids,
            parts,
            idxs,
            STUB_PAYLOAD,
            heads,
        )
        return OffloadStats(
            records_offloaded=len(rows),
            bytes_before=bytes_before,
            bytes_after=sum(
                len(p) + len(t) for p, t in zip(payloads, texts, strict=True)
            ),
        )
