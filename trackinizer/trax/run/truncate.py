"""Bound one captured record to what an upload can carry.

Every record is uploaded inside an HTTP request, and the proxy in front of the
server refuses a body over 8 MB with HTTP 413 -- so one huge record failed its
whole batch, on every retry. A tool result is the usual culprit, and it is
rarely stored once: a codex shell result states its output as ``stdout``, again
as ``aggregated_output`` and ``formatted_output``, and capture adds a search
copy. ~1 MB of printed logs measured 11.39 MB on the wire.

So capture cuts what will not fit, here rather than in the session readers:
they are lossless and also serve conversion, and only an upload has a limit.
Each string longer than the record's share keeps its beginning and its end
around a marker saying how many bytes went, the shape Codex gives the model
for a long output:

  https://github.com/openai/codex/blob/main/codex-rs/utils/output-truncation/src/lib.rs

A tool result gets :data:`MAX_TOOL_RESULT_BYTES`; any other kind
:data:`MAX_RECORD_BYTES`, a backstop that ordinary prose never reaches. A record
that fits is returned as it came, byte for byte.

A cut record is a transcript the model never saw, and Trackinizer rebuilds
sessions from stored records, so a cut record says so: under :data:`NOTE` in its
``extra``, or in an uncategorized record's ``payload``, and in the marker alone
where a record is bare text. Resuming such a session needs ``--lossy``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final
from uuid import UUID

import json
import re

from treekle import immutable, mutable

from trackinizer.lib.agent.types.sessions import (
    IncompleteRecord,
    ToolResult,
    UncategorizedRecord,
)
from trackinizer.lib.agent.types.stored import from_stored, to_stored
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stderr, Stdin, Stdout


if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel
    from treekle import MutablePlainTree, PlainTree

    from trackinizer.types.streams import TraxRecord
    from trackinizer.wire.wire_session_ir import RecordBody


__all__ = [
    "MAX_RECORD_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "NOTE",
    "bounded",
    "encoded_size",
    "was_truncated",
    "without_note",
]


MAX_TOOL_RESULT_BYTES: Final = 256 << 10
"""Wire bytes one tool result may take, its search-text copy included.

Large enough that an ordinary file read is stored whole, so its session still
resumes without ``--lossy``; small enough that a 4 MiB batch holds sixteen."""

MAX_RECORD_BYTES: Final = 1 << 20
"""Wire bytes any other record may take: a backstop against the proxy's limit,
not a budget prose is expected to reach."""

NOTE: Final = "$trackinizer_truncated"
"""The key a cut record carries, mapping to ``{"omitted_bytes": N}``.

``$``-prefixed like the session readers' own notes on a record, which the
writers never emit as a provider field. Resume strips it regardless: a writer
replays an uncategorized record's payload verbatim."""

# Below this a cut string is mostly marker. A record still over its budget here is
# held up by what no cut shrinks -- ciphertext, attachment bytes, or thousands of
# short strings -- and goes as it is rather than being dropped.
_MIN_KEPT_BYTES: Final = 1 << 10

# Matches what :func:`_marker` writes.
_MARKER: Final = re.compile(
    r"\[Trackinizer capture truncated: kept beginning/end; omitted \d+ UTF-8 bytes\]",
)


def bounded(body: RecordBody) -> RecordBody:
    """Return ``body`` cut to fit its kind's budget, or ``body`` itself when it fits.

    Every string longer than one shared length keeps its beginning and end, and
    the length shrinks until the whole body fits, so short strings stay whole.
    Bytes and ciphertext are never cut, so a body held up by them stays over.

    Args:
      body: One record's wire body, already redacted: a cut can leave the end
        of a secret at the start of the kept tail, where no redactor finds it.

    Returns:
      bounded: ``body`` when it fits, else a copy whose record carries
        :data:`NOTE` and whose search text is derived from what was kept.

    """
    size = encoded_size(body)
    if size <= MAX_TOOL_RESULT_BYTES:
        return body
    record = body.row(UUID(int=0), 0).record()
    max_bytes = (
        MAX_TOOL_RESULT_BYTES if isinstance(record, ToolResult) else MAX_RECORD_BYTES
    )
    if size <= max_bytes:
        return body
    cap = max_bytes
    while True:
        cut = _cut(body, record, cap=cap)
        size = encoded_size(cut)
        if size <= max_bytes or cap == _MIN_KEPT_BYTES:
            return cut
        # Proportional, with an eighth to spare: escapes make a string's wire size
        # a multiple of its UTF-8 size that no single cap predicts.
        cap = max(_MIN_KEPT_BYTES, cap * max_bytes * 7 // (size * 8))


def encoded_size(body: BaseModel) -> int:
    """Return the bytes ``body`` takes in a request, encoded as the client sends it.

    The client hands ``httpx2`` the dumped model as ``json=``, which encodes it
    compactly with non-ASCII kept raw -- the bytes the proxy counts.

    Args:
      body: A wire model, alone or as one element of a request.

    Returns:
      size: Its length in bytes, as the request carries it.

    """
    return len(
        json.dumps(
            body.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
    )


def was_truncated(record: TraxRecord) -> bool:
    """Return whether capture cut ``record`` to bound it.

    Args:
      record: A record as stored, read back.

    Returns:
      truncated: Whether it carries :data:`NOTE`, or, being bare text, the marker.

    """
    match record:
        case IncompleteRecord() | Stdin() | Stdout() | Stderr():
            return _MARKER.search(record.text) is not None
        case UncategorizedRecord():
            return NOTE in record.payload
        case _:
            return NOTE in record.extra


def without_note[R: TraxRecord](record: R) -> R:
    """Return ``record`` without :data:`NOTE`; its text keeps the marker."""
    if not was_truncated(record):
        return record
    stripped = _with_note(record, None)
    assert isinstance(stripped, type(record))
    return stripped


def _cut(body: RecordBody, record: TraxRecord, *, cap: int) -> RecordBody:
    """Return ``body`` holding ``record`` with every string over ``cap`` bytes cut."""
    omitted: list[int] = []
    cut = from_stored(
        _cut_tree(to_stored(record), cap=cap, omitted=omitted),
        type(record),
    )
    if not omitted:
        return body
    row = SessionRecordRow.of(
        session_id=UUID(int=0),
        part=0,
        idx=body.idx,
        record=_with_note(cut, immutable({"omitted_bytes": sum(omitted)})),
    )
    # Through the row, so the search text is derived from what was kept. The rest
    # of the envelope is the original's: the row knows no model or ciphertext.
    # Thawed, because a copy is not validated and pydantic cannot dump a frozen one.
    return body.model_copy(update={"payload": mutable(row.payload), "text": row.text})


def _cut_tree(
    tree: MutablePlainTree,
    *,
    cap: int,
    omitted: list[int],
) -> MutablePlainTree:
    """Return ``tree`` with each string over ``cap`` bytes cut, tallying the loss."""
    if isinstance(tree, str):
        return _cut_text(tree, cap=cap, omitted=omitted)
    if isinstance(tree, list):
        return [_cut_tree(item, cap=cap, omitted=omitted) for item in tree]
    # The codec stores bytes as base64 under this tag: a cut would leave neither the
    # bytes nor readable text.
    if isinstance(tree, dict) and "py/b64" not in tree:
        return {
            key: _cut_tree(item, cap=cap, omitted=omitted) for key, item in tree.items()
        }
    return tree


def _cut_text(text: str, *, cap: int, omitted: list[int]) -> str:
    """Return ``text`` as its beginning, the marker and its end, in ``cap`` bytes."""
    encoded = text.encode()
    if len(encoded) <= cap:
        return text
    # Room for the longest count the marker could state, and its two newlines.
    keep = cap - len(_marker(len(encoded))) - 2
    head = _char_start(encoded, keep // 2, step=-1)
    tail = _char_start(encoded, len(encoded) - (keep - keep // 2), step=1)
    omitted.append(tail - head)
    return (
        f"{encoded[:head].decode()}\n{_marker(tail - head)}\n{encoded[tail:].decode()}"
    )


def _char_start(encoded: bytes, index: int, *, step: int) -> int:
    """Move ``index`` by ``step`` to where a character starts, so no cut splits one."""
    # A UTF-8 continuation byte is ``0b10xxxxxx``; any other byte starts a character.
    while encoded[index] & 0xC0 == 0x80:
        index += step
    return index


def _marker(omitted: int) -> str:
    """Return the text standing where ``omitted`` bytes were cut out."""
    return (
        "[Trackinizer capture truncated: kept beginning/end; "
        f"omitted {omitted} UTF-8 bytes]"
    )


def _with_note(record: TraxRecord, note: PlainTree) -> TraxRecord:
    """Return ``record`` carrying ``note`` under :data:`NOTE`, or none for ``None``."""
    match record:
        case IncompleteRecord() | Stdin() | Stdout() | Stderr():
            # Bare text has no field for a note; the marker in it is the note.
            return record
        case UncategorizedRecord():
            return replace(record, payload=_noted(record.payload, note))
        case _:
            return replace(record, extra=_noted(record.extra, note))


def _noted(
    fields: Mapping[str, PlainTree],
    note: PlainTree,
) -> Mapping[str, PlainTree]:
    """Return ``fields`` with :data:`NOTE` set to ``note``, or removed for ``None``."""
    kept = {key: value for key, value in fields.items() if key != NOTE}
    return immutable(kept if note is None else kept | {NOTE: note})
