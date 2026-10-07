"""Keep what a typed provider record does not model, so a round trip is exact.

A session reader decodes each provider line into a typed record. Three states
of a modeled field must survive that decode: absent, ``null``, and malformed.
:func:`read_field_keeping_invalid` reads one field into a :data:`FieldState`.
:func:`extract_unmodeled_fields` stores the rest in the record's ``extra``, as
an envelope under :data:`_FIELD_STATE_TAG` that also remembers key order and
which modeled fields were present. :func:`restore_unmodeled_fields` rebuilds
the provider object from that ``extra`` and the record's current values.

The envelope is stored in sessions already on disk and in golden files, so its
key and structure are a format, not an implementation detail.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, TypeGuard, cast

import math

from trackinizer.lib.absent import ABSENT, Absent
from trackinizer.lib.codec import (
    Invalid,
    PlainTree,
    ReadError,
    from_plain,
    immutable,
)


__all__ = [
    "FieldState",
    "extract_unmodeled_fields",
    "read_field_keeping_invalid",
    "restore_unmodeled_fields",
    "same_json_value",
]


type FieldState[T] = Absent | T | Invalid | None


_FIELD_STATE_TAG: Final = "$__custom_json_fields__"


def same_json_value(value: object, member: object) -> bool:
    """Whether two JSON values are recursively equal by JSON type.

    Args:
      value: The first JSON value to compare.
      member: The second JSON value to compare.

    Returns:
      result: True if both values are recursively equal by JSON type.

    """
    if isinstance(value, bool) != isinstance(member, bool):
        return False
    if (
        isinstance(value, float)
        and isinstance(member, float)
        and math.isnan(value)
        and math.isnan(member)
    ):
        return True
    if isinstance(value, Mapping) and isinstance(member, Mapping):
        left = cast(Mapping[object, object], value)
        right = cast(Mapping[object, object], member)
        return left.keys() == right.keys() and all(
            same_json_value(item, right[key]) for key, item in left.items()
        )
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and isinstance(member, Sequence)
        and not isinstance(member, (str, bytes, bytearray))
    ):
        left_items = cast(Sequence[object], value)
        right_items = member
        return len(left_items) == len(right_items) and all(
            same_json_value(left, right)
            for left, right in zip(left_items, right_items, strict=True)
        )
    return value == member


def read_field_keeping_invalid[T](
    source: Mapping[str, object],
    key: str,
    target: type[T],
) -> FieldState[T]:
    """Read one field without collapsing absence, null, or malformed data.

    Args:
      source: Provider JSON object.
      key: Field to read.
      target: Expected runtime type.

    Returns:
      state: Absent, decoded (including null), or invalid field state.

    """
    if key not in source:
        return ABSENT
    raw = source[key]
    if raw is None:
        return None
    checked = _provider_json_value(key, raw)
    value = _decode_or_none(target, raw)
    if value is None:
        return Invalid(raw=checked)
    return value


def extract_unmodeled_fields(
    source: Mapping[str, object],
    consumed: Iterable[str] = (),
    *,
    fields: Mapping[str, FieldState[object]] | None = None,
) -> dict[str, PlainTree]:
    """Return the fields of ``source`` the typed record does not model.

    The result goes into a record's ``extra``. With ``fields``, it is an
    envelope that also remembers key order and which modeled fields were
    present, so :func:`restore_unmodeled_fields` can rebuild ``source``
    exactly; a malformed field stays verbatim among the unmodeled ones.
    ``consumed`` only drops keys, keeping no envelope.

    Args:
      source: Provider JSON object.
      consumed: Modeled field names to drop without envelope state.
      fields: States returned by :func:`read_field_keeping_invalid`.

    Returns:
      extra: JSON-safe unmodeled fields.

    """
    checked: dict[str, PlainTree] = {}
    for key, value in source.items():
        if not isinstance(key, str):  # pyright: ignore[reportUnnecessaryIsInstance] -- Untyped provider data still requires this runtime boundary guard.
            raise TypeError(f"provider object key must be str, got {key!r}")
        checked[key] = _provider_json_value(key, value)
    if fields is None:
        dropped = set(consumed)
        kept = {key: value for key, value in checked.items() if key not in dropped}
        if _unmodeled_envelope(kept) is None:
            return kept
        return {
            _FIELD_STATE_TAG: {
                "version": 1,
                "order": list(kept),
                "states": {},
                "residual": kept,
            },
        }
    represented = {
        key: "null" if state is None else "value"
        for key, state in fields.items()
        if state is not ABSENT and not isinstance(state, Invalid)
    }
    dropped = set(consumed)
    spare = {
        key: value
        for key, value in checked.items()
        if key not in represented and key not in dropped
    }
    return {
        _FIELD_STATE_TAG: {
            "version": 1,
            "order": list(source),
            "states": represented,
            # Only a value whose SPELLING the round trip can change: a source
            # ``1.0`` decodes to a float that re-encodes as ``1``, so the
            # original is the only way back. A container or a string has one
            # spelling, and storing it was 0.124 MB of pure duplication
            # across 40 captured sessions.
            "raw": {
                key: checked[key]
                for key in represented
                if key in checked
                and isinstance(checked[key], (int, float))
                and not isinstance(checked[key], bool)
            },
            "residual": spare,
        },
    }


def restore_unmodeled_fields(
    stored: Mapping[str, object],
    values: Mapping[str, object],
) -> dict[str, object]:
    """Rebuild a provider object from its unmodeled fields and modeled values.

    Args:
      stored: ``extra`` as returned by :func:`extract_unmodeled_fields`. A
        plain mapping, including an envelope with unknown field-state labels,
        is returned unchanged because it records no modeled fields.
      values: Current value of every modeled field. Ignored when ``stored``
        is not a recognized envelope.

    Returns:
      object_: Provider object in its original key order.

    Raises:
      KeyError: A represented field has no current semantic value.

    """
    envelope = _unmodeled_envelope(stored)
    if envelope is None:
        return dict(stored)
    order = [key for key in _listed(envelope["order"]) if isinstance(key, str)]
    states = _str_keyed(envelope["states"])
    original = _str_keyed(envelope.get("raw"))
    spare = _str_keyed(envelope["residual"])
    result: dict[str, object] = {}
    for key in order:
        if key in states:
            if key not in values:
                raise KeyError(f"missing value for modeled field {key!r}")
            current = values[key]
            result[key] = (
                original[key]
                if key in original and same_json_value(current, original[key])
                else current
            )
        elif key in spare:
            result[key] = spare[key]
    result.update({key: value for key, value in spare.items() if key not in result})
    return result


def _provider_json_value(key: str, value: object) -> PlainTree:
    """Validate one provider field and name it in failures."""
    try:
        immutable(value)
    except TypeError as exc:
        raise TypeError(f"field {key!r}: {exc}") from exc
    return cast(PlainTree, value)


def _unmodeled_envelope(stored: Mapping[str, object]) -> dict[str, object] | None:
    """Return a valid unmodeled-fields envelope, if present."""
    raw = stored.get(_FIELD_STATE_TAG)
    if not isinstance(raw, Mapping):
        return None
    envelope = {
        key: value
        for key, value in cast(Mapping[object, object], raw).items()
        if isinstance(key, str)
    }
    if not _is_version_one(envelope.get("version")):
        return None
    if not isinstance(envelope.get("order"), list):
        return None
    states = envelope.get("states")
    if not isinstance(states, Mapping):
        return None
    if any(
        not isinstance(key, str) or label not in ("null", "value")
        for key, label in cast(Mapping[object, object], states).items()
    ):
        return None
    if not isinstance(envelope.get("residual"), Mapping):
        return None
    if "raw" in envelope and not isinstance(envelope["raw"], Mapping):
        return None
    return envelope


# A malformed provider field means "invalid", not "abort". The codec reads no
# number from a string, nor a string from a number, so either is a mismatch.
def _decode_or_none[T](target: type[T], value: object) -> T | None:
    """Read ``value`` as ``target``, or ``None`` when it is not one."""
    try:
        return from_plain(value, target)
    except ReadError:
        return None


def _str_keyed(value: object) -> dict[str, object]:
    """Return an envelope mapping with string keys, or an empty one."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): member for key, member in cast(Mapping[object, object], value).items()
    }


def _is_version_one(version: object) -> bool:
    """Return whether an envelope declares version 1."""
    if isinstance(version, bool):
        return False
    if isinstance(version, str):
        try:
            return int(version.strip()) == 1
        except ValueError:
            return False
    return isinstance(version, (int, float)) and version == 1


def _listed(value: object) -> list[object]:
    """Return an iterable's items, raising the builtin message otherwise."""
    if _is_iterable(value):
        return list(value)
    raise TypeError(f"{type(value).__name__!r} object is not iterable")


def _is_iterable(value: object) -> TypeGuard[Iterable[object]]:
    return isinstance(value, Iterable)
