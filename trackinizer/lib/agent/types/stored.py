"""A session record as stored plain data: codec tags, with ``JSON`` fields left plain.

Session rows and normalized session files store each record through the codec,
so a tuple field is written ``{"py/tuple": [...]}`` and bytes ``{"py/b64": ...}``.
A record's ``JSON`` fields (``Mapping[str, PlainTree]``: ``extra``,
``arguments``, ...) hold provider JSON, frozen in memory; they are thawed
before encoding, so they store as the plain objects the provider wrote rather
than as ``py/mappingproxy`` and ``py/tuple`` wrappers, and SQL and the web
client can read their keys directly.

Reading reverses it: the codec decodes the record, and each ``JSON`` field is
frozen again, as the provider readers build it, so a stored record equals the
record its source file normalizes to.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
from typing import Final, cast, get_type_hints

import weakref

from trackinizer.lib.codec import (
    MutablePlainTree,
    PlainTree,
    from_plain,
    immutable,
    mutable,
    to_plain,
)


__all__ = ["from_stored", "to_stored"]


def to_stored(record: object) -> dict[str, MutablePlainTree]:
    """Return ``record`` as stored plain data.

    Args:
      record: A session record.

    Returns:
      plain: Codec-tagged plain data, with each ``JSON`` field untagged.

    Raises:
      TypeError: ``record`` does not store as a JSON object, as a row's
        payload must.

    """
    plain = to_plain(_with_json_fields(record, convert=mutable))
    if not isinstance(plain, dict):
        raise TypeError(f"{type(record).__name__} does not store as a JSON object")
    return plain


def from_stored[T](data: object, target: type[T]) -> T:
    """Read stored plain data as ``target``, each ``JSON`` field frozen.

    Args:
      data: Plain data in the stored shape, or the plain shape written before
        records were stored through the codec.
      target: The record class, or a union of them.

    Returns:
      record: The decoded record.

    Raises:
      ReadError: ``data`` does not read as ``target``.

    """
    return cast(T, _with_json_fields(from_plain(data, target), convert=immutable))


def _with_json_fields(value: object, *, convert: Callable[[object], object]) -> object:
    """Return ``value`` with ``convert`` applied to each record's ``JSON`` fields."""
    if isinstance(value, tuple):
        return tuple(
            _with_json_fields(item, convert=convert)
            for item in cast(tuple[object, ...], value)
        )
    if not is_dataclass(value) or isinstance(value, type):
        return value
    json_fields = _json_field_names(type(value))
    changes: dict[str, object] = {}
    for field in fields(value):
        if field.init:
            member = cast(object, getattr(value, field.name))
            changes[field.name] = (
                convert(member)
                if field.name in json_fields
                else _with_json_fields(member, convert=convert)
            )
    return replace(value, **changes)


_JSON_FIELD_NAMES: Final[weakref.WeakKeyDictionary[type, frozenset[str]]] = (
    weakref.WeakKeyDictionary()
)


def _json_field_names(target: type) -> frozenset[str]:
    """Return the names of ``target``'s fields annotated ``Mapping[str, PlainTree]``."""
    cached = _JSON_FIELD_NAMES.get(target)
    if cached is None:
        cached = frozenset(
            name
            for name, hint in get_type_hints(target).items()
            # ``==``, not ``is``: each subscription builds a new alias object.
            if hint == Mapping[str, PlainTree]
        )
        _JSON_FIELD_NAMES[target] = cached
    return cached
