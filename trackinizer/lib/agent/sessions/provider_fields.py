"""One field of a provider's session log, read the way a missing one is.

    read_or_default    # (value, target, default=) -> value, or default when it is not a target

A session log is another program's file. One field of the wrong type -- a failed
call's ``toolUseResult`` is a bare string, an MCP tool's a list -- must cost the
record that field, not the record: the reader keeps the rest of the line typed.
"""

from __future__ import annotations

from typing import overload

from trackinizer.lib.codec import ReadError, from_plain


__all__ = ["read_or_default"]


@overload
def read_or_default[T](
    value: object,
    target: type[T],
    *,
    default: None,
) -> T | None: ...


@overload
def read_or_default[T](value: object, target: type[T], *, default: T) -> T: ...


def read_or_default[T](
    value: object,
    target: type[T],
    *,
    default: T | None,
) -> T | None:
    """Return ``value`` as ``target``, or ``default`` when it is absent or mistyped.

    Unlike :func:`convert`, whose ``default`` covers only a missing or null
    value and which raises on any other type, this reads a mistyped value as
    absent. Use it only for a field whose wrong value the record can still
    write back byte for byte; any other field keeps ``convert``, so the line's
    guard stores the whole line as text.

    Args:
      value: Parsed JSON.
      target: The type to produce.
      default: Returned when ``value`` is ``None`` or is not a ``target``.

    Returns:
      result: ``value`` as a ``target``, or ``default``.

    """
    try:
        return from_plain(value, target, default=default)
    except ReadError:
        return default
