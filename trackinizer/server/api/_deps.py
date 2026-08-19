"""Shared dependencies for API route modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import dataclasses
import datetime
import uuid

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import Inquiry


if TYPE_CHECKING:
    from fastapi import Request


def get_store(request: Request) -> Store:
    """Return the Store held on the FastAPI app state."""
    return cast(Store, request.app.state.store)


def get_inbound(request: Request) -> InboundQueue:
    """Return the inbound-message queue held on the FastAPI app state."""
    return cast(InboundQueue, request.app.state.inbound)


def tag_row(inquiry: Inquiry) -> MutableJSON:
    """Serialize one Inquiry that is known to exist.

    The total variant. Callers holding a ``list[Inquiry]`` used to route
    through :func:`tag_kind` and then filter its ``None`` back out -- a guard
    for a case the type already excluded, kept alive only because the
    signature was wider than any caller needed.
    """
    payload = cast(MutableJSON, _jsonable(inquiry))
    payload["kind"] = type(inquiry).__name__
    return payload


def tag_kind(inquiry: Inquiry | None) -> MutableJSON | None:
    """Serialize one Inquiry, adding an explicit ``kind`` discriminator field.

    Walks the dataclass tree directly instead of calling ``jsonable_encoder``,
    which measured 5.3ms of a 9ms 50-row listing -- more than the SQL, the
    edge fetch, and model construction combined. Every Inquiry is a frozen
    dataclass of JSON-shaped fields, so the encoder's general type dispatch is
    cost without benefit; :func:`_jsonable` does the same job in 2.6ms.

    ``dataclasses.asdict`` is NOT the shortcut it appears to be: it cannot
    convert the leaves, so the scalars come back as ``UUID`` and ``datetime``
    and need a ``dict_factory`` -- which restores the per-field Python call
    that made the encoder slow (measured 3.9ms, only 1.3x). One pass that both
    flattens and converts is the whole win.

    ``_deps_test`` pins this output against ``jsonable_encoder`` for every
    kind, building each fixture from ``dataclasses.fields`` so a newly added
    field is compared without an edit there. An earlier version of that test
    passed only the base kwargs, which compared nine all-``None`` payloads and
    proved nothing about the per-kind fields it named.
    """
    if inquiry is None:
        return None
    return tag_row(inquiry)


def _jsonable(value: object) -> object:
    """Convert one value -- and everything under it -- to JSON-shaped data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple | list):
        # Tuples become lists: the projection fields (edges, ``labels``,
        # ``issue_kind``) are tuples on the model, and a caller comparing
        # structures in-process would see the type differ even though both
        # dump to the same JSON array.
        return [_jsonable(item) for item in cast("tuple[object, ...]", value)]
    if isinstance(value, dict):
        return {
            key: _jsonable(item)
            for key, item in cast("dict[str, object]", value).items()
        }
    if isinstance(value, datetime.datetime | datetime.date):
        # ISO-8601, never ``str(datetime)``: that yields a space separator
        # which Python re-parses happily and other languages reject.
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str | int | float | bool | None):
        return value
    # Everything reachable is handled above, so an unrecognized type means a
    # new field type arrived without a branch here. Raising names it at the
    # seam; returning it unchanged would put a non-JSON object in the payload
    # and blow up inside ``json.dumps`` as FastAPI writes the response -- a
    # 500 whose traceback points at the encoder instead of the field.
    #
    # Only ``Experiment.config`` can carry a caller-chosen type: it is the
    # one JSONB column, and jsonb round-trips through ``json.loads``, so a
    # read yields nothing exotic. The guard is for the next field, not this
    # one.
    raise TypeError(f"cannot serialize {type(value).__name__} to JSON: {value!r}")
