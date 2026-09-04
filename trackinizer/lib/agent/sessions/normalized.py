"""Normalize and denormalize sessions as the provider-neutral JSON form.

The adapter whose wire format is the IR itself: one tagged JSON array of
records, encoded by ``trackinizer.lib.custom_json``, so a session converted to this
format and back carries every semantic record rather than a provider
projection of one.

Unlike the native formats this one is a DOCUMENT -- a JSON array is not
readable a line at a time -- so its reader consumes the whole stream before
yielding. The signature is the same either way, which is what lets a caller
convert between formats without knowing which it holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TextIO, cast

import json

from trackinizer.lib.agent.types.sessions import SessionRecord
from trackinizer.lib.custom_json import (
    DataclassCodec,
    decode,
    json_unfreeze,
)


__all__ = ["denormalize", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a session JSON stream into its records.

    Args:
      stream: Session JSON text stream.

    Yields:
      record: Each record the document holds, in stream order.

    """
    # Each record carries its own ``py/object`` tag, which is what selects the
    # union member -- so the whole list decodes as the annotated type rather
    # than one class named up front.
    decoded = decode(list[SessionRecord], json.loads(stream.read()))
    assert isinstance(decoded, list)
    yield from cast("list[SessionRecord]", decoded)


def denormalize(records: Iterable[SessionRecord], stream: TextIO) -> None:
    """Denormalize records as provider-neutral JSON.

    Args:
      records: Provider-neutral records, in stream order.
      stream: Destination text stream.

    """
    # Compact, not indented: this is a storage and transport form, and
    # indenting a 273 MB session spent 33 MB on whitespace alone.
    json.dump(
        [json_unfreeze(DataclassCodec.to_json(record)) for record in records],
        stream,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    stream.write("\n")
