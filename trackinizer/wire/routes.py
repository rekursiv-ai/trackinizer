"""The inquiry and edge route tables, derived from the column specs.

Every inquiry field route is derived from the ``Annotated[T,
ColumnSpec(...)]`` declarations in :mod:`types.inquiries`; there is no
hand-maintained list. The server registers handlers by iterating the
table, the client builds requests from it, and the API doc is generated
from it, so none of the three can drift from the column definitions.

Which verbs a column exposes is computed from its own metadata:

* PUT    -- every mutable column; ``compare_and_set`` columns also accept
  ``expected``.
* PATCH  -- list columns (those with a ``list_verb_stem``) and the
  numeric ``marginal_cost_*`` axes.
* DELETE -- every mutable, non-required column (clear to NULL / 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Final, Literal, get_args, get_origin

from trackinizer.types.columns import (
    FlatColumn,
    flat_column_specs,
)
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
    Inquiry,
)


# Prefix of the flattened cost axes. These are numeric deltas routed
# through the Store's ``add_cost`` and have no ``set_<column>`` setter, so
# the route table marks them specially instead of deriving a method name.
_COST_PREFIX: Final = "marginal_cost_"

# Every inquiry class, so the table covers each kind's own columns plus the
# shared base: ``Inquiry`` first (base columns), then each concrete kind from
# the canonical ``KIND_TO_CLASS`` registry. The registry's stable declaration
# order fixes route-registration order; no parallel hand-list to keep in sync.
_INQUIRY_CLASSES: tuple[type[Inquiry], ...] = (Inquiry, *KIND_TO_CLASS.values())


@dataclass(frozen=True, slots=True, kw_only=True)
class InquiryFieldRoute:
    """One mutable inquiry column and the verbs it exposes.

    Derived from a :class:`FlatColumn`; consumers never build it by hand.
    The Store-method names are computed from the column name and
    ``list_verb_stem`` so they stay in lockstep with the dataclass.
    """

    column: str
    """SQL column name; the ``{field}`` URL segment."""

    value_type: object
    """The column's annotated type, used as ``FieldSet[value_type]`` for
    PUT."""

    put: bool
    """Whether PUT (overwrite) applies -- true for every mutable column."""

    delete: bool
    """Whether DELETE (unset) applies -- mutable and not required."""

    compare_and_set: bool
    """Whether PUT accepts ``expected`` for compare-and-set."""

    supports_reason: bool
    """Whether the setter takes an audit ``reason``."""

    element_type: object | None
    """PATCH element type (``FieldOp[element_type]``) for a list column;
    ``None`` when the column is not list-augmentable."""

    set_method: str | None
    """Store method for PUT / DELETE; ``None`` for cost axes, which clear
    via the cost path rather than a ``set_`` setter."""

    patch: bool
    """Whether PATCH (add/sub) applies -- true for list columns and the
    numeric ``marginal_cost_*`` axes. Set in :func:`_route_for` rather
    than inferred from ``add_method``, since a cost axis is PATCH-able yet
    dispatches through ``Store.add_cost`` rather than a named method."""

    add_method: str | None
    """Store method for PATCH ``op=add`` on a list column (``add_<stem>``);
    ``None`` otherwise. Cost axes route through ``Store.add_cost``."""

    sub_method: str | None
    """Store method for PATCH ``op=sub`` on a list column
    (``remove_<stem>``); ``None`` otherwise."""

    cost_axis: str | None
    """The ``Cost`` field this column maps to (``agent_usd`` /
    ``resource_usd``) when it is a cost axis; else ``None``."""

    owner_kind: str | None
    """Lowercased owning kind (``paper``) for a kind-specific column, so
    its route is ``/api/<owner_kind>/<id>/<field>``. ``None`` for a base
    column (applies to every kind) or a cost axis, which stay under
    ``/api/inquiries/<id>/<field>``."""


def _element_type(value_type: object) -> object | None:
    """Return the element type of a ``tuple[X, ...] | None`` column.

    A list column is stored as ``tuple[X, ...]`` (optionally ``| None``),
    and its PATCH element is one ``X``. Returns ``None`` when
    ``value_type`` is not a homogeneous tuple.
    """
    for member in get_args(value_type) or (value_type,):
        if get_origin(member) is tuple:
            args = get_args(member)
            if len(args) == 2 and args[1] is Ellipsis:
                return args[0]
    return None


def _owner_kind(spec: FlatColumn) -> str | None:
    """Lowercased sole owning kind of a column, or ``None`` for a base column."""
    owners = spec.spec.applies_to_inquiry_kinds
    if owners is None or len(owners) != 1:
        return None
    (owner,) = owners
    return owner.lower()


def _route_for(column: str, flat: FlatColumn) -> InquiryFieldRoute:
    """Derive the route for one flat column from its spec."""
    spec = flat.spec
    if column.startswith(_COST_PREFIX):
        axis = column[len(_COST_PREFIX) :]
        return InquiryFieldRoute(
            column=column,
            value_type=flat.value_type,
            put=True,
            delete=True,
            compare_and_set=False,
            supports_reason=spec.supports_reason,
            element_type=flat.value_type,
            set_method=None,
            patch=True,
            add_method=None,
            sub_method=None,
            cost_axis=axis,
            owner_kind=None,
        )
    stem = spec.list_verb_stem
    return InquiryFieldRoute(
        column=column,
        value_type=flat.value_type,
        put=not spec.immutable,
        delete=not spec.immutable and not spec.required,
        compare_and_set=spec.compare_and_set,
        supports_reason=spec.supports_reason,
        element_type=_element_type(flat.value_type) if stem else None,
        set_method=f"set_{column}" if not spec.immutable else None,
        patch=bool(stem),
        add_method=f"add_{stem}" if stem else None,
        sub_method=f"remove_{stem}" if stem else None,
        cost_axis=None,
        owner_kind=_owner_kind(flat),
    )


@cache
def inquiry_field_routes() -> tuple[InquiryFieldRoute, ...]:
    """Every mutable inquiry-field route, derived from ``types/``.

    One entry per distinct flat column across the Inquiry hierarchy, in a
    stable order. Cached, since the table is fixed once the dataclasses
    are defined.
    """
    seen: dict[str, InquiryFieldRoute] = {}
    for cls in _INQUIRY_CLASSES:
        for column, flat in flat_column_specs(cls).items():
            # A non-route-editable column (``agentsession_ended``) is written
            # only through its dedicated method, so it gets no field route.
            if column in seen or not flat.spec.route_editable:
                continue
            seen[column] = _route_for(column, flat)
    return tuple(seen.values())


@cache
def field_owner_kind() -> dict[str, str]:
    """Map each kind-specific field name to its lowercased owning kind.

    Base fields and cost axes are absent (they route under
    ``/api/inquiries``). Lets the client and SPA build the kind-scoped
    field URL from the field name alone, with no parallel table.
    """
    return {
        route.column: route.owner_kind
        for route in inquiry_field_routes()
        if route.owner_kind is not None
    }


def inquiry_field_path(field: str) -> str:
    """Return the field-route path template for ``field``.

    Kind-specific fields route under their owning kind
    (``/api/<kind>/{target_id}/<field>``); base fields and cost axes stay
    under ``/api/inquiries/{target_id}/<field>``. The kind segment mirrors
    the Python ``paper.source`` and CLI ``trax paper`` structure.
    """
    owner = field_owner_kind().get(field)
    prefix = f"/api/{owner}" if owner is not None else "/api/inquiries"
    return f"{prefix}/{{target_id}}/{field}"


@dataclass(frozen=True, slots=True, kw_only=True)
class EdgeFieldRoute:
    """One edge annotation column and the verbs it exposes.

    Edge identity lives in the URL path, so the route carries only the
    annotation column. Every annotation is PUT-settable and
    DELETE-clearable; only list annotations (``labels``) are
    PATCH-augmentable.
    """

    column: str
    """Edge annotation column name; the ``{field}`` URL segment."""

    value_type: object
    """The annotation's type, used as ``FieldSet[value_type]`` for PUT."""

    element_type: object | None
    """PATCH element type for a list annotation; ``None`` otherwise."""

    @property
    def patch(self) -> bool:
        """Whether PATCH (add/sub) applies."""
        return self.element_type is not None


@cache
def edge_field_routes() -> tuple[EdgeFieldRoute, ...]:
    """Every edge annotation route, derived from :class:`Edge`'s columns.

    Mirrors :func:`inquiry_field_routes`. ``Edge`` has no ``flatten``
    columns, so the flat view equals the raw column set.
    """
    return tuple(
        EdgeFieldRoute(
            column=column,
            value_type=flat.value_type,
            element_type=(
                _element_type(flat.value_type) if flat.spec.list_verb_stem else None
            ),
        )
        for column, flat in flat_column_specs(Edge).items()
    )


def edge_field_path(field: str) -> str:
    """Return the edge annotation path template for ``field``."""
    return f"/api/edges/{{from_id}}/{{edge_kind}}/{{to_id}}/{field}"


type HttpVerb = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


# List-endpoint pagination policy. Part of the HTTP contract, so every
# consumer reads it instead of hard-coding its own number.
# ``DEFAULT_LIST_LIMIT`` applies when a caller omits ``limit``;
# ``MAX_LIST_LIMIT`` is the ceiling the server enforces on any supplied
# ``limit``.
DEFAULT_LIST_LIMIT = 50  # config-globals: ignore -- shared default; threading would duplicate across N call sites
MAX_LIST_LIMIT: Final = 1000
