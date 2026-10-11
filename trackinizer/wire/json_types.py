"""Shared pydantic aliases for wire models: recursive JSON, and UTC instants.

pydantic can type a self-referencing alias, and needs to: with a flat alias
one level deep it passes a nested frozen mapping or tuple through unvalidated
and then cannot dump it, and the published OpenAPI schema stops describing
anything below the first level. These aliases recurse, and keep the names
``JSON`` and ``JSONValue`` so every schema component keeps its name.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence
from datetime import UTC, datetime
from typing import Annotated

import json

from pydantic import AfterValidator


__all__ = [
    "JSON",
    "JSONValue",
    "MutableJSON",
    "MutableJSONValue",
    "UtcDatetime",
    "storable_object",
]


def _naive_is_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_naive_is_utc)]
"""A request-body instant; a naive value reads as UTC, an aware one is kept.

asyncpg encodes a naive datetime bound to a ``TIMESTAMPTZ`` column with the
server process's local zone, so ``2024-12-10`` would be stored as a different
instant on every host. The schema stays ``date-time``.
"""


type JSONValue = (
    str | int | float | bool | Sequence[JSONValue] | Mapping[str, JSONValue] | None
)
type JSON = Mapping[str, JSONValue]

type MutableJSONValue = (
    str
    | int
    | float
    | bool
    | MutableSequence[MutableJSONValue]
    | MutableMapping[str, MutableJSONValue]
    | None
)
type MutableJSON = MutableMapping[str, MutableJSONValue]


# A NUL or a non-finite number reaches Postgres as text a ``jsonb`` column refuses,
# which answers 500, and an object past the column's size CHECK answers 409; both
# are the caller's error, so the model refuses them with 422 first.
def storable_object(value: JSON, *, max_bytes: int) -> JSON:
    """Return ``value`` when a ``jsonb`` column can hold it, else raise ``ValueError``.

    Args:
      value: A validated JSON object.
      max_bytes: Its largest size, as compact UTF-8 JSON.

    Returns:
      value: ``value`` unchanged.

    """
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ValueError:
        raise ValueError("a number is not finite") from None
    if len(text.encode()) > max_bytes:
        raise ValueError(f"the object exceeds {max_bytes} bytes")
    if _holds_nul(value):
        raise ValueError("a string contains a NUL character")
    return value


def _holds_nul(value: JSONValue) -> bool:
    """Return True when any key or string under ``value`` holds a NUL."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, Mapping):
        return any("\x00" in key or _holds_nul(item) for key, item in value.items())
    if isinstance(value, Sequence):
        return any(_holds_nul(item) for item in value)
    return False
