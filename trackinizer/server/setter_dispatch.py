"""Per-column behavioral hooks consumed by ``Store._set_field``.

The Inquiry hierarchy in ``types/inquiries.py`` declares each editable
column via ``field(metadata=column(...))``. That metadata carries the
*declarative* facts -- which kinds the column applies to, whether the
public setter forwards a ``reason`` -- in one place (the dataclass), in
the shape autosql codegen consumes. The change kind is the column's flat
storage name (see :func:`storage_name`), derived rather than declared.

:data:`RUNTIME_HOOKS` below holds the *behavioral* per-column hooks
(normalize, encode, decode_old, validate, notify_old_subscribers).
Hooks live here rather than on the dataclass because they reference
trackinizer-private helpers and importing those into ``types/`` would
invert the dependency direction.

``Store._set_field`` composes the two -- one ColumnSpec from the
dataclass plus zero-or-one matching hook entry -- and drives the
whole mutation pipeline (lock, read, compare, validate, update,
audit, cascade, notify) from that pair. Adding a new editable field
now means decorating one dataclass attribute; no separate Store edit
is required unless the field needs a non-default hook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from _typeshed import DataclassInstance

from trackinizer.lib.postgres import Conn
from trackinizer.server.values import (
    byline_strs,
    canonical_strs,
)
from trackinizer.types.columns import (
    ColumnSpec,
    column_specs,
    storage_name,
)
from trackinizer.types.inquiries import (
    AgentSession,
    Artifact,
    Belief,
    CodeChange,
    Experiment,
    Inquiry,
    Issue,
    Paper,
    WebResult,
    WebSearch,
)


_Encoder = Callable[[Any], Any]
"""Transform a normalized Python value into the form ``UPDATE`` accepts.

Used for list columns where the Python value is a tuple but storage
wants a plain list asyncpg can bind to the array column.
"""

_Decoder = Callable[[Any], Any]
"""Transform a stored value back into the canonical Python form.

Used so the old-vs-new equality check operates in canonical space
(e.g. a stored array decoding to ``tuple[...]``).
"""

TargetValidator = Callable[[Conn, Any], Awaitable[None]]
"""Validate a new value against the live database before writing.

Used for columns that reference other inquiries (``codechanges``)
where PG's CHECK constraints can't reach the target rows.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class _RuntimeHooks:
    """Per-column behavioral hooks consumed by ``Store._set_field``.

    Pure-default entries can be omitted from the hook table; only
    columns that need at least one non-default behavior require an
    entry. Splitting these out from the dataclass-attached
    :class:`ColumnSpec` keeps ``types/`` free of references to
    Store-private helpers.
    """

    normalize: _Encoder = field(default=lambda v: v)
    encode: _Encoder = field(default=lambda v: v)
    decode_old: _Decoder = field(default=lambda v: v)
    validate: TargetValidator | None = None
    notify_old_subscribers: bool = False


def _canonical_tuple(value: Iterable[str]) -> tuple[str, ...]:
    """Adapter so ``canonical_strs`` matches the ``_Encoder`` shape."""
    return canonical_strs(value)


def _tuple_or_none(value: Iterable[object] | None) -> tuple[object, ...] | None:
    """Decode a stored list column, preserving NULL as ``None``.

    A NULL list column stays ``None`` ("value was absent") rather than
    collapsing to ``()`` ("explicitly cleared"), so an edit from an unset
    column records a NULL old side in the audit log -- the distinction the
    change-log snapshot relies on (see ``types/change_log.py``).
    """
    return None if value is None else tuple(value)


NO_HOOKS = _RuntimeHooks()
"""Sentinel: identity normalize/encode/decode, no validator,
post-edit-subscribers-only routing. Used when a column has no entry
in ``RUNTIME_HOOKS`` -- the common case."""


RUNTIME_HOOKS: dict[str, _RuntimeHooks] = {
    "issue_kind": _RuntimeHooks(
        normalize=_canonical_tuple,
        decode_old=_tuple_or_none,
        encode=list,
    ),
    "labels": _RuntimeHooks(
        normalize=_canonical_tuple,
        decode_old=_tuple_or_none,
        encode=list,
    ),
    "agentsession_rooms": _RuntimeHooks(
        normalize=_canonical_tuple,
        decode_old=_tuple_or_none,
        encode=list,
    ),
    "subscribers": _RuntimeHooks(
        normalize=_canonical_tuple,
        decode_old=_tuple_or_none,
        encode=list,
        # Route the audit row to the pre-edit subscriber set too, so a
        # just-removed subscriber sees the change documenting their
        # removal.
        notify_old_subscribers=True,
    ),
    "experiment_codechanges": _RuntimeHooks(
        # Canonical Python form is tuple[UUID, ...] to match the
        # Snapshot typing. Storage column is UUID[]; asyncpg encodes
        # either tuple or list.
        normalize=tuple,
        decode_old=_tuple_or_none,
    ),
    "paper_authors": _RuntimeHooks(
        # A byline: order and duplicates are significant, so NOT canonicalized
        # (unlike labels/subscribers). The wire value is a tuple (Paper.authors
        # is tuple[str, ...]) but PG returns a list for TEXT[]; without these
        # hooks the no-op dedup compares tuple != list and every identical PUT
        # writes a phantom change + audit row. ``byline_strs`` strips each
        # element and drops blanks (so "Smith " and "Smith" are one byline
        # entry) while keeping order and legitimate duplicates -- the same
        # normalizer the submit path uses, so create and edit agree.
        normalize=byline_strs,
        decode_old=_tuple_or_none,
    ),
}
"""``storage_name -> _RuntimeHooks`` for the columns that need
non-default behavior. Keyed by the flat storage name (the same key the
Store's ``_set_field`` resolves), so the list column is
``experiment_codechanges``, not its bare field name. Late-bound
validators are attached by ``primitives.py`` at import time once their
helpers are defined."""


def _column_specs_for(*classes: type[DataclassInstance]) -> dict[str, ColumnSpec]:
    """Merge ``ColumnSpec`` metadata across a tuple of dataclass classes.

    Keyed by the flat storage name (:func:`storage_name`), so a
    kind-specific column lands under ``paper_source`` -- matching its
    physical ``inquiries`` column and emitted ``Change.Kind`` -- while
    base columns stay bare. Several columns appear on more than one
    Inquiry subclass (``status`` on ``Inquiry``, inherited everywhere);
    one pass over the hierarchy gives the Store one mapping to dispatch
    from. Asserts that duplicate columns agree on their specs.
    """
    merged: dict[str, ColumnSpec] = {}
    for cls in classes:
        for field_name, spec in column_specs(cls).items():
            col = storage_name(field_name, spec)
            if (
                col in merged and merged[col] != spec
            ):  # pragma: no cover -- defensive consistency check across the hierarchy.
                raise AssertionError(
                    f"inconsistent ColumnSpec for {col!r}: {merged[col]!r} vs {spec!r}"
                )
            merged[col] = spec
    return merged


COLUMN_SPECS: dict[str, ColumnSpec] = _column_specs_for(
    Inquiry,
    Issue,
    Artifact,
    Experiment,
    Paper,
    Belief,
    CodeChange,
    WebResult,
    WebSearch,
    AgentSession,
)
"""``storage_name -> ColumnSpec`` aggregated from every Inquiry subclass.

Built from the dataclass-attached metadata at module load, keyed by the
flat storage name. The Store's setter dispatch and the SQL codegen read
from here; editing the dataclass is the single point of change.
"""
