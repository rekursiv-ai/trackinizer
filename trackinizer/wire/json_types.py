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

from pydantic import AfterValidator


__all__ = ["JSON", "JSONValue", "MutableJSON", "MutableJSONValue", "UtcDatetime"]


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
