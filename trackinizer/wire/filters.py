"""Filter shapes for the list endpoint.

The list endpoint accepts zero or more ``filter`` query parameters, each
a JSON object decoded to :class:`Filter`. A filter is a triple: a
canonical column name (matching the Inquiry dataclass in
:mod:`types.inquiries`), one of the ``FilterOp`` literals, and a string
value.

The CLI accepts ergonomic aliases (``kind`` for ``issue_kind``,
``agent-cost`` for ``marginal_cost_agent_usd``, and so on) and translates
them to canonical names before sending, so the server never sees an
alias.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from trackinizer.types.columns import (
    column_specs,
    flat_column_specs,
    storage_name,
)
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
    Inquiry,
)


__all__ = [
    "FILTER_FIELD_ALIASES",
    "FILTER_OPS",
    "MAX_FILTER_VALUE_CHARS",
    "NON_NULLABLE_COLUMNS",
    "VALUELESS_FILTER_OPS",
    "Filter",
    "FilterOp",
    "canonical_filter_field",
    "validate_presence_op",
]


# Upper bound on a filter operand. ``Filter.value`` reaches ``re.compile`` once
# per request for a ``re`` / ``nre`` op (see ``server/api/query.py``); an
# unbounded operand lets a long pathological pattern drive catastrophic
# backtracking in the validation compile. 512 chars comfortably fits any real
# column value or regex while capping that cost (mirrors the message-body caps
# in ``wire_sessions.py``).
MAX_FILTER_VALUE_CHARS = 512


# Every inquiry class, so the derived NOT-NULL set below spans the whole
# hierarchy: base columns once via ``Inquiry``, per-kind columns via each
# concrete subclass from the canonical ``KIND_TO_CLASS`` registry (no parallel
# hand-list -- a new kind registers itself there).
_INQUIRY_KIND_CLASSES: tuple[type[Inquiry], ...] = (Inquiry, *KIND_TO_CLASS.values())


FilterOp = Literal["is", "ne", "re", "nre", "lt", "le", "gt", "ge", "isnull", "notnull"]
FILTER_OPS: tuple[FilterOp, ...] = (
    "is",
    "ne",
    "re",
    "nre",
    "lt",
    "le",
    "gt",
    "ge",
    "isnull",
    "notnull",
)

# Ops that test column presence rather than compare a value. They carry no
# operand: the CLI parser consumes no value token and the server accepts a
# filter object with the ``value`` key absent, materializing ``value=""``.
VALUELESS_FILTER_OPS: frozenset[FilterOp] = frozenset({"isnull", "notnull"})


# Identity/housekeeping columns the schema declares NOT NULL directly; they
# carry no ColumnSpec, so they can't be derived from the spec metadata below.
_NOT_NULL_IDENTITY_COLUMNS: frozenset[str] = frozenset(
    {"id", "kind", "seq", "created", "modified"}
)


def _derive_non_nullable_columns() -> frozenset[str]:
    """Canonical columns that are NOT NULL in ``inquiries``.

    Derived from the column specs rather than hand-listed, so a future
    ``required=True`` field (or a new flattened axis) is covered automatically
    instead of silently breaking presence-op validation. A column is NOT NULL
    when its spec is ``required`` (``status``, ``title``) or ``flatten``-ed
    (the cost axes, ``NOT NULL DEFAULT 0`` in ``schema.sql``); everything else
    is a ``| None`` field on the Inquiry dataclass.
    """
    columns = set(_NOT_NULL_IDENTITY_COLUMNS)
    for source in _INQUIRY_KIND_CLASSES:
        for name, flat in flat_column_specs(source).items():
            if flat.spec.required or flat.spec.flatten is not None:
                columns.add(storage_name(name, flat.spec))
    return frozenset(columns)


# Presence ops (``isnull`` / ``notnull``) on a NOT-NULL column are always-empty
# / always-all -- a silent wrong answer rejected up front by both the CLI and
# the route via :func:`validate_presence_op`.
NON_NULLABLE_COLUMNS: frozenset[str] = _derive_non_nullable_columns()


def validate_presence_op(field: str, op: FilterOp) -> str | None:
    """Return an error message if ``op`` is a presence test on a NOT-NULL field.

    ``isnull`` / ``notnull`` only make sense on a nullable column; on a
    NOT-NULL one they silently match nothing / everything. ``field`` must
    already be canonical (post :func:`canonical_filter_field`). Returns
    ``None`` when the pairing is valid.
    """
    if op in VALUELESS_FILTER_OPS and field in NON_NULLABLE_COLUMNS:
        return f"filter field {field!r} is NOT NULL; {op} does not apply"
    return None


def _kind_specific_aliases() -> dict[str, str]:
    """Bare field name -> flat storage column, for every kind-specific column.

    A kind-specific column stores under a ``<kind>_`` prefix (``paper_source``),
    but the CLI filters by the bare field (``source``), the kind already in
    scope. DERIVED from the specs (the same ``storage_name`` rule the schema and
    routes use), so a new kind-specific field is filterable with no edit here --
    the GSI-02 bug class (a bare filter the CLI accepted but the server 400'd
    because its alias was missing) cannot recur.

    This may emit an alias for a column with no CLI filter token today
    (``config`` -> ``experiment_config``, ``opened_by_api_key_id`` -> ...): the
    alias is harmless -- its target is already in the server whitelist (which
    derives from the same specs), so it can never 400, and with no grammar token
    it is unreachable. Deriving the whole set is deliberately preferred over a
    hand-tuned exclusion, which would re-introduce the drift the derivation kills.
    """
    out: dict[str, str] = {}
    for cls in KIND_TO_CLASS.values():
        for name, spec in column_specs(cls).items():
            storage = storage_name(name, spec)
            if storage != name:
                out[name] = storage
    return out


# CLI-ergonomic filter field -> canonical SQL column on ``inquiries``. The one
# place this aliasing is declared; the CLI parser, the server route, and the row
# evaluator all canonicalize through :func:`canonical_filter_field`. The
# kind-specific bare->storage aliases (``source`` -> ``paper_source``, ...) are
# DERIVED (:func:`_kind_specific_aliases`); only the non-derivable ergonomic
# spellings (alternate names, cost axes) are hand-declared here.
FILTER_FIELD_ALIASES: Mapping[str, str] = {
    "kind": "issue_kind",
    "issuekind": "issue_kind",
    "label": "labels",
    "subscriber": "subscribers",
    "agent-cost": "marginal_cost_agent_usd",
    "resource-cost": "marginal_cost_resource_usd",
    **_kind_specific_aliases(),
}


def canonical_filter_field(field: str) -> str:
    """Resolve a CLI filter-field alias to its canonical SQL column name.

    A canonical column or an unknown field passes through unchanged, so
    callers can canonicalize unconditionally and the server still rejects
    genuinely-unknown fields with a precise error.
    """
    return FILTER_FIELD_ALIASES.get(field, field)


@dataclass(frozen=True, kw_only=True, slots=True)
class Filter:
    """One ``field op value`` clause as the wire carries it.

    ``field`` is the canonical column name from :mod:`types.inquiries`,
    matching the SQL column on the ``inquiries`` table. The CLI translates
    its ergonomic aliases before constructing this.
    """

    field: str
    op: FilterOp
    value: str

    def __post_init__(self) -> None:
        # Cap the operand so a pathological ``re`` / ``nre`` pattern cannot
        # drive catastrophic backtracking in the per-request ``re.compile``.
        # The cap travels with the wire type, so every construction site (the
        # server decode, the CLI) enforces it identically.
        if len(self.value) > MAX_FILTER_VALUE_CHARS:
            raise ValueError(
                f"filter value exceeds {MAX_FILTER_VALUE_CHARS} characters"
            )
