"""The code store: each record's code, kept once per owner, and the diff of any two.

A blob is one text file, keyed by its owner and the sha256 of its UTF-8 bytes, which
:func:`put_blobs` checks before it stores the bytes zstd-compressed. Dedupe never
crosses owners: a store shared between owners would let anyone learn whether a file
exists. A snapshot maps a record's paths to blob hashes; :func:`put_snapshot` files
one only when every blob is stored for its owner and its parent record holds one of
the same owner, replacing the record's snapshot.

A reader reads a snapshot that is not private when it reads the record: any record
but a session, and a session whose transcript it reads. A private snapshot is read
by its owner alone; no role reads another person's. Every read names its reader,
with no default: any reader, even
:data:`~trackinizer.server.audience.NOBODY`, reads a snapshot that is not
private of a record that is not a session, so a default would not fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import asyncio
import difflib
import hashlib
import itertools

from treekle import from_plain

from trackinizer.lib import zstd_compat
from trackinizer.server.audience import visible_sql
from trackinizer.server.notify import tx
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.wire.wire_code import (
    MAX_BLOB_BYTES,
    MAX_BLOBS_PER_REQUEST,
    MAX_DIFF_CHARS,
    MAX_MISSING_HASHES,
    MAX_PATH_CHARS,
    MAX_REQUEST_TEXT_BYTES,
    MAX_SNAPSHOT_FILES,
    MISSING_BLOBS_CODE,
    NO_PARENT_SNAPSHOT_CODE,
    CodeDiff,
    CodeFile,
    CodeFileDiff,
    CodeSnapshot,
    CodeSnapshotFile,
    CodeSnapshotHead,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from uuid import UUID

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.audience import SessionReader
    from trackinizer.wire.wire_code import CodeBlob, CodeSnapshotPut


__all__ = [
    "MissingBlobsError",
    "NoParentSnapshotError",
    "SnapshotOwnerError",
    "diff_snapshots",
    "missing_blobs",
    "put_blobs",
    "put_snapshot",
    "read_file",
    "read_snapshot",
]


_BLOBS_PER_FETCH: Final = 64
"""Changed paths whose blobs a diff reads in one query; the connection is released
between fetches, so a long diff never holds PGlite's one connection."""


class MissingBlobsError(ConflictError):
    """A snapshot names a blob that is not stored for its owner."""

    code = MISSING_BLOBS_CODE


class NoParentSnapshotError(ConflictError):
    """A snapshot's parent record holds no snapshot of the same owner."""

    code = NO_PARENT_SNAPSHOT_CODE


class SnapshotOwnerError(Exception):
    """A write would replace a snapshot filed under another owner."""


async def missing_blobs(
    engine: DatabaseEngine,
    *,
    owner: str,
    hashes: Sequence[str],
) -> list[str]:
    """Return the hashes not stored for ``owner``, each once, in the order given.

    Args:
      engine: The store's engine.
      owner: The lowercase email the blobs are filed under.
      hashes: Sha256 hex of each blob asked about.

    Returns:
      missing: The hashes of ``hashes`` the store lacks for ``owner``.

    Raises:
      ValidationError: More than ``MAX_MISSING_HASHES`` hashes.

    """
    if len(hashes) > MAX_MISSING_HASHES:
        raise ValidationError(
            f"one request asks about at most {MAX_MISSING_HASHES} hashes, "
            f"not {len(hashes)}",
        )
    wanted = list(dict.fromkeys(hashes))
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sha256 FROM code_blobs WHERE owner = $1 AND sha256 = ANY($2)",
            owner,
            [bytes.fromhex(sha) for sha in wanted],
        )
    held = {from_plain(row["sha256"], bytes).hex() for row in rows}
    return [sha for sha in wanted if sha not in held]


async def put_blobs(
    engine: DatabaseEngine,
    *,
    owner: str,
    blobs: Sequence[CodeBlob],
) -> int:
    """Store each blob not yet stored for ``owner``.

    Args:
      engine: The store's engine.
      owner: The lowercase email the blobs are filed under.
      blobs: Each file's text and the sha256 hex of its UTF-8 bytes.

    Returns:
      stored: How many blobs were new; one stored before, or sent twice, is a no-op.

    Raises:
      ValidationError: More than ``MAX_BLOBS_PER_REQUEST`` blobs or
        ``MAX_REQUEST_TEXT_BYTES`` of text, a blob over ``MAX_BLOB_BYTES``, text with
        no UTF-8 form, or a hash that is not the sha256 of the text.

    """
    if len(blobs) > MAX_BLOBS_PER_REQUEST:
        raise ValidationError(
            f"one request carries at most {MAX_BLOBS_PER_REQUEST} blobs, "
            f"not {len(blobs)}",
        )
    sent = [(blob.sha256, _verified(blob)) for blob in blobs]
    total = sum(len(data) for _, data in sent)
    if total > MAX_REQUEST_TEXT_BYTES:
        raise ValidationError(
            f"one request carries at most {MAX_REQUEST_TEXT_BYTES} bytes of text, "
            f"not {total}",
        )
    encoded = dict(sent)
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "INSERT INTO code_blobs (owner, sha256, size, data_zst) "
            "SELECT $1, b.sha256, b.size, b.data_zst "
            "FROM unnest($2::bytea[], $3::int[], $4::bytea[]) "
            "AS b(sha256, size, data_zst) "
            "ON CONFLICT (owner, sha256) DO NOTHING RETURNING sha256",
            owner,
            [bytes.fromhex(sha) for sha in encoded],
            [len(data) for data in encoded.values()],
            [zstd_compat.compress(data) for data in encoded.values()],
        )
    return len(rows)


async def put_snapshot(
    engine: DatabaseEngine,
    *,
    record_id: UUID,
    body: CodeSnapshotPut,
    created_by: UUID | None,
    replace_others: bool,
) -> CodeSnapshotHead:
    """File a record's code under ``body.owner``, replacing the snapshot it had.

    Args:
      engine: The store's engine.
      record_id: The record the code belongs to.
      body: The snapshot.
      created_by: The user filing it.
      replace_others: Replace a snapshot filed under another owner, as staff may.

    Returns:
      head: The snapshot as stored.

    Raises:
      ValidationError: A path that is not a plain relative POSIX path, more than
        ``MAX_SNAPSHOT_FILES`` files, or a parent that is the record itself.
      NotFoundError: No record ``record_id``.
      SnapshotOwnerError: The record's snapshot is another owner's, and
        ``replace_others`` is False.
      NoParentSnapshotError: The parent record holds no snapshot of ``body.owner``.
      MissingBlobsError: A blob of ``body.files`` is not stored for ``body.owner``.

    """
    _check_files(body.files)
    if body.parent_record_id == record_id:
        raise ValidationError("a snapshot cannot build on its own record")
    hashes = sorted(set(body.files.values()))
    async with engine.acquire() as conn, tx(conn):
        if not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM inquiries WHERE id = $1)",
            record_id,
        ):
            raise NotFoundError(f"unknown record {record_id}")
        if body.parent_record_id is not None and body.owner != await conn.fetchval(
            "SELECT owner FROM code_snapshots WHERE record_id = $1",
            body.parent_record_id,
        ):
            raise NoParentSnapshotError(
                f"parent record {body.parent_record_id} holds no snapshot of this "
                "owner",
            )
        sizes = await _sizes(conn, owner=body.owner, hashes=hashes)
        if len(sizes) < len(hashes):
            raise MissingBlobsError(
                f"{len(hashes) - len(sizes)} of the snapshot's {len(hashes)} blobs "
                "are not stored for this owner",
            )
        # The owner test sits in the upsert, so a concurrent filing cannot slip a
        # snapshot of another owner in between a check and the write.
        row = await conn.fetchrow(
            "INSERT INTO code_snapshots (record_id, parent_id, owner, private, files, "
            "file_count, total_bytes, partial, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            "ON CONFLICT (record_id) DO UPDATE SET parent_id = EXCLUDED.parent_id, "
            "owner = EXCLUDED.owner, private = EXCLUDED.private, "
            "files = EXCLUDED.files, file_count = EXCLUDED.file_count, "
            "total_bytes = EXCLUDED.total_bytes, partial = EXCLUDED.partial, "
            "created_by = EXCLUDED.created_by, updated_at = clock_timestamp() "
            "WHERE code_snapshots.owner = EXCLUDED.owner OR $10 "
            "RETURNING record_id, parent_id AS parent_record_id, private, file_count, "
            "total_bytes, partial, updated_at",
            record_id,
            body.parent_record_id,
            body.owner,
            body.private,
            dict(body.files),
            len(body.files),
            sum(sizes[sha] for sha in body.files.values()),
            body.partial,
            created_by,
            replace_others,
        )
        if row is None:
            raise SnapshotOwnerError(f"record {record_id}'s code is another owner's")
    return CodeSnapshotHead.model_validate(dict(row))


async def read_snapshot(
    engine: DatabaseEngine,
    *,
    record_id: UUID,
    reader: SessionReader,
) -> CodeSnapshot | None:
    """Return a record's snapshot and its files sorted by path; None when unread.

    Args:
      engine: The store's engine.
      record_id: The record.
      reader: Who reads.

    Returns:
      snapshot: The snapshot; ``None`` when there is none or ``reader`` may not read
        it.

    """
    async with engine.acquire() as conn:
        held = await _readable(conn, record_id=record_id, reader=reader)
        if held is None:
            return None
        sizes = await _sizes(conn, owner=held.owner, hashes=held.files.values())
    return CodeSnapshot.model_validate(
        {
            **held.head,
            "files": [
                CodeSnapshotFile(path=path, sha256=sha, size=sizes[sha])
                for path, sha in sorted(held.files.items())
            ],
        },
    )


async def read_file(
    engine: DatabaseEngine,
    *,
    record_id: UUID,
    path: str,
    reader: SessionReader,
) -> CodeFile | None:
    """Return one file of a record's snapshot; None when unread or not in it.

    Args:
      engine: The store's engine.
      record_id: The record.
      path: The file's path in the snapshot.
      reader: Who reads.

    Returns:
      file: Its path, hash and text; ``None`` when the snapshot is missing or
        unreadable, or maps no such path.

    """
    async with engine.acquire() as conn:
        held = await _readable(conn, record_id=record_id, reader=reader)
        if held is None or path not in held.files:
            return None
        sha = held.files[path]
        blobs = await _blobs(conn, keys=[(held.owner, sha)])
    return CodeFile(path=path, sha256=sha, text=_text(blobs, owner=held.owner, sha=sha))


async def diff_snapshots(
    engine: DatabaseEngine,
    *,
    from_id: UUID,
    to_id: UUID,
    context: int,
    reader: SessionReader,
) -> CodeDiff | None:
    """Return how ``to_id``'s snapshot differs from ``from_id``'s, path by path.

    Args:
      engine: The store's engine.
      from_id: The record whose snapshot is the old side.
      to_id: The record whose snapshot is the new side.
      context: Lines of context around each change.
      reader: Who reads.

    Returns:
      diff: Each path that differs, sorted, with its unified diff, at most
        ``MAX_DIFF_CHARS`` of diff in all; ``None`` when either snapshot is missing
        or unreadable.

    """
    async with engine.acquire() as conn:
        old = await _readable(conn, record_id=from_id, reader=reader)
        new = await _readable(conn, record_id=to_id, reader=reader)
    if old is None or new is None:
        return None
    files: list[CodeFileDiff] = []
    budget = MAX_DIFF_CHARS
    for chunk in itertools.batched(
        _changes(old.files, new=new.files),
        _BLOBS_PER_FETCH,
    ):
        keys = [(old.owner, before) for _, before, _ in chunk if before is not None]
        keys += [(new.owner, after) for _, _, after in chunk if after is not None]
        async with engine.acquire() as conn:
            blobs = await _blobs(conn, keys=keys)
        diffs, budget = await asyncio.to_thread(
            _diff_chunk,
            chunk,
            sides=(old.owner, new.owner),
            blobs=blobs,
            context=context,
            budget=budget,
        )
        files += diffs
        if budget < 0:
            return CodeDiff(files=files, more=True)
    return CodeDiff(files=files, more=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Held:
    """A snapshot as a read needs it: whose blobs it maps, to which paths, its head."""

    owner: str
    files: dict[str, str]
    head: dict[str, object]


type _Change = tuple[str, str | None, str | None]
"""A path, its blob on the old side and its blob on the new side."""


def _verified(blob: CodeBlob) -> bytes:
    """Return a blob's UTF-8 bytes, refusing a text the hash does not name."""
    try:
        data = blob.text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValidationError(f"blob {blob.sha256} has no UTF-8 form") from error
    if len(data) > MAX_BLOB_BYTES:
        raise ValidationError(
            f"blob {blob.sha256} holds {len(data)} bytes, over {MAX_BLOB_BYTES}",
        )
    if hashlib.sha256(data).hexdigest() != blob.sha256:
        raise ValidationError(f"blob {blob.sha256} is not the sha256 of its text")
    return data


def _check_files(files: Mapping[str, str]) -> None:
    """Refuse too many files, or a path that is not a plain relative POSIX path."""
    if len(files) > MAX_SNAPSHOT_FILES:
        raise ValidationError(
            f"a snapshot maps at most {MAX_SNAPSHOT_FILES} files, not {len(files)}",
        )
    for path in files:
        if (
            len(path) > MAX_PATH_CHARS
            or "\x00" in path
            or not {"", ".", ".."}.isdisjoint(path.split("/"))
        ):
            raise ValidationError(
                f"{path[:100]!r} is not a relative POSIX path of at most "
                f"{MAX_PATH_CHARS} characters with no empty, '.' or '..' segment",
            )


async def _readable(
    conn: Conn,
    *,
    record_id: UUID,
    reader: SessionReader,
) -> _Held | None:
    """Read a record's snapshot when ``reader`` may; None otherwise."""
    params: list[object] = [record_id, reader.email]
    row = await conn.fetchrow(
        vetted_sql(
            "SELECT s.owner, s.files, s.record_id, s.parent_id AS parent_record_id, "
            "s.private, s.file_count, s.total_bytes, s.partial, s.updated_at "
            "FROM code_snapshots AS s JOIN inquiries AS i ON i.id = s.record_id "
            "WHERE s.record_id = $1 AND (NOT s.private OR s.owner = $2) "
            "AND (i.kind <> 'AgentSession' OR ",
            visible_sql("i", params=params, reader=reader, single=True),
            ")",
        ),
        *params,
    )
    if row is None:
        return None
    head = dict(row)
    owner = from_plain(head.pop("owner"), str)
    files = from_plain(head.pop("files"), dict[str, str])
    return _Held(owner=owner, files=files, head=head)


async def _sizes(conn: Conn, *, owner: str, hashes: Iterable[str]) -> dict[str, int]:
    """Return the size of each of ``hashes`` stored for ``owner``, by hash."""
    rows = await conn.fetch(
        "SELECT sha256, size FROM code_blobs WHERE owner = $1 AND sha256 = ANY($2)",
        owner,
        [bytes.fromhex(sha) for sha in set(hashes)],
    )
    return {
        from_plain(row["sha256"], bytes).hex(): from_plain(row["size"], int)
        for row in rows
    }


async def _blobs(
    conn: Conn,
    *,
    keys: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], bytes]:
    """Return the compressed bytes of each ``(owner, sha256 hex)`` blob."""
    rows = await conn.fetch(
        "SELECT b.owner, b.sha256, b.data_zst FROM code_blobs AS b "
        "JOIN unnest($1::text[], $2::bytea[]) AS k(owner, sha256) "
        "ON b.owner = k.owner AND b.sha256 = k.sha256",
        [owner for owner, _ in keys],
        [bytes.fromhex(sha) for _, sha in keys],
    )
    return {
        (
            from_plain(row["owner"], str),
            from_plain(row["sha256"], bytes).hex(),
        ): from_plain(row["data_zst"], bytes)
        for row in rows
    }


def _changes(old: Mapping[str, str], *, new: Mapping[str, str]) -> list[_Change]:
    """Return each path whose blob differs between the sides, sorted."""
    return [
        (path, old.get(path), new.get(path))
        for path in sorted(old.keys() | new.keys())
        if old.get(path) != new.get(path)
    ]


# A blob is stored only after its sha256 is checked against its UTF-8 bytes, and a
# snapshot only once every blob it names is stored, so a lookup here cannot miss.
def _text(blobs: Mapping[tuple[str, str], bytes], *, owner: str, sha: str) -> str:
    """Return the text of one fetched blob."""
    return zstd_compat.decompress(blobs[owner, sha]).decode("utf-8")


def _diff_chunk(
    chunk: Sequence[_Change],
    *,
    sides: tuple[str, str],
    blobs: Mapping[tuple[str, str], bytes],
    context: int,
    budget: int,
) -> tuple[list[CodeFileDiff], int]:
    """Diff ``chunk``'s changes; return the diffs and the budget left, -1 once cut."""
    old_owner, new_owner = sides
    diffs: list[CodeFileDiff] = []
    for path, before, after in chunk:
        change: Literal["added", "modified", "deleted"] = (
            "added" if before is None else "deleted" if after is None else "modified"
        )
        diff = _unified(
            path,
            old="" if before is None else _text(blobs, owner=old_owner, sha=before),
            new="" if after is None else _text(blobs, owner=new_owner, sha=after),
            context=context,
        )
        cut = len(diff) > budget
        if cut:
            diff = diff[: diff.rfind("\n", 0, budget) + 1]
        if diff or not cut:
            diffs.append(
                CodeFileDiff(
                    path=path,
                    change=change,
                    old_sha256=before,
                    new_sha256=after,
                    diff=diff,
                ),
            )
        if cut:
            return diffs, -1
        budget -= len(diff)
    return diffs, budget


def _unified(path: str, *, old: str, new: str, context: int) -> str:
    """Return the unified diff of two texts, marking a last line with no newline."""
    return "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(
            _lines(old),
            _lines(new),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )


# ``str.splitlines`` also breaks at ``\r``, form feeds and Unicode separators, which a
# diff must keep inside a line, so only ``\n`` ends one here.
def _lines(text: str) -> list[str]:
    """Split ``text`` after each newline; a last line with none stays bare."""
    *whole, tail = text.split("\n")
    lines = [f"{line}\n" for line in whole]
    if tail:
        lines.append(tail)
    return lines
