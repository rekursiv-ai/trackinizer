"""The Row protocol and the per-column metadata that drives codegen.

The Inquiry hierarchy is the single source of truth for the data model.
:class:`ColumnSpec` lets each dataclass field carry the facts -- its change
kind, which kinds it applies to, its SQL shape -- that schema generation,
the wire bodies, and the Store's setter dispatch must all agree on.
"""

from __future__ import annotations

from collections import UserDict
from dataclasses import dataclass, field, fields
from functools import cache
from typing import TYPE_CHECKING, Any, Protocol, get_type_hints, overload


if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class Row(Protocol):
    """The minimal mapping surface a ``from_row`` reader depends on.

    It needs only subscripting, membership, and ``get`` with a default.
    Both ``asyncpg.Record`` in production and a plain ``dict`` in tests
    satisfy this, so the row mappers never care which one they got.
    """

    def __getitem__(self, key: Any, /) -> Any: ...
    def __contains__(self, x: object, /) -> bool: ...
    @overload
    def get(self, key: str) -> Any | None: ...
    @overload
    def get(self, key: str, default: Any) -> Any: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ColumnSpec(UserDict[str, "ColumnSpec"]):
    """Everything one editable column needs to declare about itself.

    You attach it to a field with ``field(metadata=ColumnSpec(...))``: the
    field keeps its real annotation while the spec rides along in
    ``metadata``. The trick is that ``ColumnSpec`` is itself a
    :class:`~collections.UserDict` holding ``{"colspec": self}``, so it
    satisfies the mapping that ``field(metadata=)`` expects without a
    wrapper dict at each call site. Setter dispatch, SQL codegen, and the
    route table all read it back through :func:`column_specs`.
    """

    applies_to_inquiry_kinds: frozenset[str] | None = None
    """Inquiry kinds where the column may be non-NULL; ``None`` means all."""

    applies_to_edge_kinds: frozenset[str] | None = None
    """Edge kinds where the column may be non-NULL; ``None`` means all."""

    supports_reason: bool = False
    """The edit can carry a ``reason`` onto the change row (status,
    marginal_cost, judgement, confidence)."""

    sql_type: str = ""
    """PostgreSQL column type, e.g. ``"TEXT"`` or ``"NUMERIC(14, 6)"``.
    Used by SQL codegen only."""

    sql_check: str = ""
    """A boolean fragment for a ``CHECK(...)`` clause, using bare column
    names."""

    min_items: int = 0
    """Minimum ``cardinality(column)`` for an array column; ``0`` disables.
    We use ``cardinality`` rather than ``array_length`` so an empty array
    counts as 0 instead of NULL. The wire check rejects early, but this DB
    check is the one that holds under concurrency."""

    references: frozenset[str] = frozenset()
    """Inquiry kinds the ids in this list column may point at. When set, a
    write-time validator locks the referenced rows ``FOR SHARE`` -- the
    JSONB / ``UUID[]`` shape rules out a real foreign key. Each element is
    either a ``(target_id, kind)`` pair or a bare target id."""

    sql_default: str = ""
    """Body of the SQL ``DEFAULT`` clause; used by codegen."""

    required: bool = False
    """Absence is a programmer error, not a meaningful state. Defaults
    ``False``, where ``None`` / NULL is a legitimate value."""

    immutable: bool = False
    """The column is set once at submit and only corrected by supersession;
    setter dispatch raises :class:`ConflictError` on any later edit."""

    flatten: type[DataclassInstance] | None = None
    """A dataclass whose fields are stored as separate scalar columns
    rather than one composite. ``None`` stores the column as-is. When set,
    each field becomes ``<flatten_prefix><field>`` with that field's scalar
    type -- :class:`Cost`, for instance, becomes ``marginal_cost_agent_usd``
    and ``marginal_cost_resource_usd``. :func:`flat_column_specs` expands
    these; :func:`column_specs` keeps the composite whole."""

    flatten_prefix: str = ""
    """Name prefix for the flattened columns; only meaningful with
    ``flatten``."""

    compare_and_set: bool = False
    """``PUT`` accepts a compare-and-set ``expected`` guard (owner, status,
    judgement). The route rejects ``expected`` on any other column."""

    list_verb_stem: str = ""
    """Singular stem for a list column's atomic Store methods, e.g.
    ``labels`` -> ``"label"`` yields ``add_label`` / ``remove_label``. Only
    set on list columns, and its presence is what marks a column
    ``PATCH``-augmentable in the route table."""

    is_byline: bool = False
    """The list is an ordered byline where duplicates are significant
    (``paper_authors``), not a canonical set. Atomic add always appends and
    atomic remove drops only the first match, instead of the set-collapsing
    add/remove every other list column uses."""

    route_editable: bool = True
    """The column gets a generic per-field ``PUT`` / ``DELETE`` / ``PATCH``
    route. ``False`` for a column written only through a dedicated method whose
    invariant a blind field write would break: ``agentsession_ended`` is
    stamped solely by ``Store.end_session`` (together with ``status``), so a
    standalone ``PUT .../ended`` would desync the lifecycle CHECK. The column
    is still stored and read; it just has no field route."""

    # The UserDict payload, filled in __post_init__ with {"colspec": self}
    # so the spec doubles as its own field(metadata=). init=False hides it
    # from the constructor; column_specs finds it by scanning metadata
    # values, so the key name never appears at call sites.
    data: dict[str, ColumnSpec] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        # A default_factory cannot reference self, so set the payload here
        # via the frozen-dataclass escape hatch.
        object.__setattr__(self, "data", {"colspec": self})


def column_specs(cls: type[DataclassInstance]) -> dict[str, ColumnSpec]:
    """Map each spec-carrying field name to its :class:`ColumnSpec`.

    Found by scanning every field's metadata for a ``ColumnSpec``; fields
    without one are skipped. Keyed by the bare dataclass field name -- the
    view the wire route table, CLI, and wire bodies read, where the kind is
    still in scope. Setter dispatch and SQL codegen key by the storage name
    instead; see :func:`storage_name` / :func:`storage_column_specs`.
    """
    out: dict[str, ColumnSpec] = {}
    for f in fields(cls):
        for meta in f.metadata.values():
            if isinstance(meta, ColumnSpec):
                out[f.name] = meta
    return out


def storage_name(field: str, spec: ColumnSpec) -> str:
    """Flat storage/audit name for one column.

    Bare for a base field (applies to every kind, no owner to name).
    ``<owner>_<field>`` for a kind-specific column, folding the owning
    kind into the name the way ``marginal_cost_`` folds the ``Cost``
    parent. This is the name used by the ``inquiries`` column, the
    ``change_log`` ``old_*``/``new_*`` mirrors, and the ``Change.Kind``
    value -- every surface where all kinds share one namespace and the
    kind is no longer in scope.
    """
    owners = spec.applies_to_inquiry_kinds
    if owners is None or len(owners) != 1:
        return field
    (owner,) = owners
    prefix = f"{owner.lower()}_"
    # A field whose name already leads with its owner (issue_kind) is
    # left alone rather than stuttered to issue_issue_kind.
    return field if field.startswith(prefix) else f"{prefix}{field}"


def storage_column_specs(cls: type[DataclassInstance]) -> dict[str, ColumnSpec]:
    """Like :func:`column_specs` but keyed by the flat storage name.

    Setter dispatch and SQL codegen read this so the column key matches
    the physical ``inquiries`` column and the emitted ``Change.Kind``.
    """
    return {storage_name(name, spec): spec for name, spec in column_specs(cls).items()}


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatColumn:
    """A single storage/wire column: its spec plus the value type."""

    spec: ColumnSpec
    """The owning column's spec, shared across all axes of a flattened
    column."""

    value_type: object
    """The Python type seen on the wire -- the field's annotation, or for a
    flattened axis, that axis field's annotation."""


@cache
def flat_column_specs(cls: type[DataclassInstance]) -> dict[str, FlatColumn]:
    """Like :func:`column_specs`, but expand flattened columns into scalars.

    A ``flatten=<dataclass>`` column becomes one :class:`FlatColumn` per
    field of that dataclass, named ``<flatten_prefix><field>``; everything
    else passes through unchanged. This is the storage- and wire-faithful
    view the route table and filter whitelist read, so neither has to spell
    out the flattened ``marginal_cost_*`` names by hand.
    """
    annotations = _resolved_annotations(cls)
    out: dict[str, FlatColumn] = {}
    for name, spec in column_specs(cls).items():
        if spec.flatten is None:
            out[name] = FlatColumn(spec=spec, value_type=annotations.get(name, object))
            continue
        for axis, axis_type in _resolved_annotations(spec.flatten).items():
            out[f"{spec.flatten_prefix}{axis}"] = FlatColumn(
                spec=spec, value_type=axis_type
            )
    return out


@cache
def _resolved_annotations(cls: type[DataclassInstance]) -> dict[str, object]:
    """Map each dataclass field name to its resolved Python type."""
    hints = get_type_hints(cls)
    return {f.name: hints.get(f.name, object) for f in fields(cls)}
