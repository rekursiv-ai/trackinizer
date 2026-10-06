"""Copies of a JSON record with one field swapped to another JSON type.

    mistyped    # record -> (path, copy) for every field and every other type
    without     # (record, path) -> copy with that field missing
    unread      # records -> how many a reader kept as text or as uncategorized

A reader of a third-party log treats a field of the wrong type the way it
treats a missing one, as absent: one malformed field must not abort the read of
a whole session. These copies are the inputs that check it -- one per field,
for each JSON type the field does not have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import copy

from trackinizer.lib.agent.types.sessions import IncompleteRecord, UncategorizedRecord


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from trackinizer.lib.agent.types.sessions import SessionRecord


__all__ = ["mistyped", "unread", "without"]


_SAMPLES: Final = ("x", 7, 1.5, True, ["x"], {"x": 1})
"""One value of each JSON type: string, integer, number, boolean, array, object."""


def mistyped(record: object) -> Iterator[tuple[str, object]]:
    """Yield a copy of ``record`` for every field and every type it is not.

    Args:
      record: A parsed JSON value, usually one log line's object.

    Yields:
      path: The swapped field, as dotted keys; a list member is its index.
      copy: ``record`` with that one field replaced; the input is not touched.

    """
    for path, value in _fields(record, ()):
        for sample in _SAMPLES:
            if type(sample) is not type(value):
                yield ".".join(map(str, path)), _replaced(record, path, sample)


def without(record: object, path: str) -> object:
    """Return a deep copy of ``record`` with the field at dotted ``path`` removed.

    Args:
      record: A parsed JSON value.
      path: Dotted keys, as :func:`mistyped` yields; a list member is its index.

    Returns:
      copy: ``record`` without that field; the input is not touched.

    """
    out = copy.deepcopy(record)
    holder = out
    keys = path.split(".")
    for key in keys[:-1]:
        holder = (
            cast(list[object], holder)[int(key)]
            if isinstance(holder, list)
            else cast(dict[str, object], holder)[key]
        )
    if isinstance(holder, list):
        del cast(list[object], holder)[int(keys[-1])]
    else:
        del cast(dict[str, object], holder)[keys[-1]]
    return out


def unread(records: Iterable[SessionRecord]) -> int:
    """Count the records a reader could not type, kept as text or as uncategorized.

    Args:
      records: What a reader returned for one session.

    Returns:
      count: Records that are an ``IncompleteRecord`` or ``UncategorizedRecord``.

    """
    return sum(isinstance(r, IncompleteRecord | UncategorizedRecord) for r in records)


def _fields(
    value: object,
    path: tuple[str | int, ...],
) -> Iterator[tuple[tuple[str | int, ...], object]]:
    """Yield every field beneath ``value``, depth first, with its path."""
    if isinstance(value, dict):
        for key, member in cast(dict[str, object], value).items():
            yield (*path, key), member
            yield from _fields(member, (*path, key))
    elif isinstance(value, list):
        for index, member in enumerate(cast(list[object], value)):
            yield (*path, index), member
            yield from _fields(member, (*path, index))


def _replaced(record: object, path: tuple[str | int, ...], leaf: object) -> object:
    """Return a deep copy of ``record`` with the field at ``path`` set to ``leaf``."""
    out = copy.deepcopy(record)
    holder = out
    for key in path[:-1]:
        holder = (
            cast(list[object], holder)[int(key)]
            if isinstance(holder, list)
            else cast(dict[str, object], holder)[str(key)]
        )
    if isinstance(holder, list):
        cast(list[object], holder)[int(path[-1])] = leaf
    else:
        cast(dict[str, object], holder)[str(path[-1])] = leaf
    return out
