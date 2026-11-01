"""Normalize and denormalize sessions as the provider-neutral JSON form.

The adapter whose wire format is the IR itself: one JSON array of records,
each as :func:`~trackinizer.sagent.session.types.stored_plain` writes it -- its class
by name, its JSON fields as the plain objects the provider sent -- so a session
converted to this format and back carries every semantic record rather than a
provider projection of one. A file written before records carried codec tags
reads too, and so does one that named each class by its import path.

Unlike the native formats this one is a DOCUMENT -- a JSON array is not
readable a line at a time -- so its reader consumes the whole stream before
yielding. The signature is the same either way, which is what lets a caller
convert between formats without knowing which it holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TextIO

import json

from treekle import MutablePlainTree, json_loads

from trackinizer.sagent.session.types import record_classes, stored_plain, stored_record


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from trackinizer.sagent.session.types import SessionRecord


__all__ = ["denormalize", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a session JSON stream into its records.

    Args:
      stream: Session JSON text stream.

    Yields:
      record: Each record the document holds, in stream order.

    """
    named = {
        f"{kind.__module__}.{name}": name for name, kind in record_classes().items()
    }
    for plain in json_loads(stream.read(), list[MutablePlainTree]):
        yield stored_record(_named(plain, named=named))


def denormalize(records: Iterable[SessionRecord], stream: TextIO) -> None:
    """Denormalize records as provider-neutral JSON.

    Args:
      records: Provider-neutral records, in stream order.
      stream: Destination text stream.

    """
    # Compact, not indented: this is a storage and transport form, and
    # indenting a 273 MB session spent 33 MB on whitespace alone.
    json.dump(
        [stored_plain(record) for record in records],
        stream,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    )
    stream.write("\n")


# A document written before classes were stored by name tags each by the import path
# this package gives it, which a reader of names refuses; and a union member is found
# by its tag alone, so a nested one must be renamed too, not only the record's own.
def _named(value: MutablePlainTree, *, named: dict[str, str]) -> MutablePlainTree:
    """Return ``value`` with each class tag ``named`` lists as an import path renamed."""
    if isinstance(value, list):
        return [_named(item, named=named) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            named.get(item, item)
            if key == "py/object" and isinstance(item, str)
            else _named(item, named=named)
        )
        for key, item in value.items()
    }
