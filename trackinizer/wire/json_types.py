"""Recursive JSON aliases for pydantic models and FastAPI routes.

pydantic can type a self-referencing alias, and needs to: with a flat alias
one level deep it passes a nested frozen mapping or tuple through unvalidated
and then cannot dump it, and the published OpenAPI schema stops describing
anything below the first level. These aliases recurse, and keep the names
``JSON`` and ``JSONValue`` so every schema component keeps its name.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence


__all__ = ["JSON", "JSONValue", "MutableJSON", "MutableJSONValue"]


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
