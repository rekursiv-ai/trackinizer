"""Low-level DB primitives shared by ``Store`` methods.

At import, binds a metadata-driven ``references`` validator into
:data:`~trackinizer.server.setter_dispatch.RUNTIME_HOOKS`
for every list column with a non-empty :class:`ColumnSpec.references`.
``store.py`` imports this at top level, so the binding runs before any Store
is built and adding a new reference list takes one metadata edit, not a new
validator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, cast, get_args
from uuid import UUID

from trackinizer.lib.postgres import Conn
from trackinizer.server.schema_gen import SEQ_FOR_KIND
from trackinizer.server.setter_dispatch import (
    COLUMN_SPECS,
    NO_HOOKS,
    RUNTIME_HOOKS,
    TargetValidator,
)
from trackinizer.server.values import (
    canonical_strs,
    empty_optional_to_none,
    vec_to_text,
    vetted_sql,
)
from trackinizer.types.columns import column_specs
from trackinizer.types.edges import (
    EDGE_POLICIES,
    PRODUCED_INFERENCE_PRECEDENCE,
    PRODUCED_INFERENCE_SUPPRESSED,
    Edge,
)
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.types.inquiries import (
    CITATION_VALENCE_DEFAULT,
    Inquiry,
    Issue,
)


# Columns ``insert_inquiry`` writes explicitly, so the derived body skips them:
# the identity/seq columns and ``status`` (COALESCE default), plus the composite
# ``marginal_cost`` (never a row column -- its flat axes are set by emit_change).
# ``title`` / ``account`` are ordinary required columns and flow through
# ``values`` like any other; only ``status`` needs bespoke SQL (the COALESCE).
_INSERT_EXPLICIT_COLUMNS: frozenset[str] = frozenset(
    {"id", "kind", "seq", "status", "marginal_cost"}
)


def _normalize_for_insert(column: str, value: object) -> object:
    """Normalize one column value for storage, from its spec + runtime hooks.

    The single "unset is NULL" contract the edit path (``Store._set_field``)
    already applies, reused here so create and edit agree: run the column's
    ``normalize`` hook (canonical / byline for list columns; identity
    otherwise), then collapse an empty result to SQL NULL for a nullable,
    non-``min_items`` column. A required column, a ``min_items`` column
    (``issue_kind`` -- an empty value must reach the DB CHECK, not collapse),
    and a falsy-but-valid scalar (``0`` / ``0.0`` / ``{}``) are left intact.
    """
    if value is None:
        return None
    hooks = RUNTIME_HOOKS.get(column, NO_HOOKS)
    normalized = hooks.normalize(value)
    spec = COLUMN_SPECS[column]
    if not spec.required and spec.min_items == 0:
        collapsed = empty_optional_to_none(normalized)
        if collapsed is None:
            return None
    return hooks.encode(normalized)


async def insert_inquiry(
    conn: Conn,
    row_id: UUID,
    kind: Inquiry.InquiryKind,
    *,
    status: Inquiry.Status | None = None,
    values: Mapping[str, object],
) -> None:
    """Insert one ``inquiries`` row, drawing ``seq`` from the per-kind sequence.

    ``values`` holds every non-identity column by its flat storage name (base
    columns bare -- ``title``, ``account``, ``description``, ``labels``, ... --
    and ``<kind>_<field>`` for kind-specific ones); an absent column stores NULL.
    The column list, placeholders, and per-column normalization all derive from
    :data:`COLUMN_SPECS` + ``RUNTIME_HOOKS`` (the same "unset is NULL" contract
    the edit path uses), so a new column needs no edit here -- decorate the
    dataclass attribute and it flows through. Only ``id`` / ``kind`` / ``seq`` /
    ``status`` are handled explicitly (identity + the ``status`` COALESCE
    default); ``marginal_cost`` is never a row column. Embeddings land
    separately via :func:`upsert_embedding`.
    """
    # Deterministic derived column order: COLUMN_SPECS is built by walking the
    # Inquiry hierarchy in a fixed order, minus the explicitly-handled columns.
    derived_columns = [c for c in COLUMN_SPECS if c not in _INSERT_EXPLICIT_COLUMNS]
    # Restore the safety the enumerated signature gave for free: an open Mapping
    # would otherwise silently DROP a typo'd key (``paper_soruce`` -> the real
    # column binds NULL) and let a missing required column reach the DB as a raw
    # NOT NULL 500. Reject both at the call site instead, before any SQL runs.
    if unknown := set(values) - set(derived_columns):
        raise ValueError(f"insert_inquiry: unknown column(s) {sorted(unknown)}")
    if missing := [
        c for c in derived_columns if COLUMN_SPECS[c].required and values.get(c) is None
    ]:
        raise ValueError(f"insert_inquiry: missing required column(s) {missing}")
    # ``id`` ($1), ``kind`` ($2), ``seq`` (nextval), ``status`` (COALESCE'd $3);
    # the derived columns follow as $4.. in bind order.
    derived_placeholders = ", ".join(
        f"${i}" for i in range(4, 4 + len(derived_columns))
    )
    await conn.execute(
        vetted_sql(
            "INSERT INTO inquiries (id, kind, seq, status, "
            + ", ".join(derived_columns)
            + ") "
            # ``status`` ($3): an unset status (``NULL``) falls back to the
            # column default 'active' via COALESCE, so a bare submit is born
            # active while an explicit create-time status is honored. The DB
            # CHECK still validates the value.
            "VALUES ($1, $2, nextval('",
            SEQ_FOR_KIND[kind],
            "'), COALESCE($3, 'active'), " + derived_placeholders + ")",
        ),
        row_id,
        kind,
        status,
        *(_normalize_for_insert(c, values.get(c)) for c in derived_columns),
    )


async def upsert_embedding(
    conn: Conn,
    inquiry_id: UUID,
    model: str,
    embedding: Sequence[float],
) -> None:
    """Upsert one ``inquiry_embeddings`` row.

    Called on submit and on title re-embed. The PK ``(inquiry_id, model)``
    plus ``ON CONFLICT DO UPDATE`` keeps the vector aligned with the current
    title across edits.
    """
    await conn.execute(
        "INSERT INTO inquiry_embeddings (inquiry_id, model, embedding) "
        "VALUES ($1, $2, $3::vector) "
        "ON CONFLICT (inquiry_id, model) DO UPDATE "
        "SET embedding = EXCLUDED.embedding, created = now()",
        inquiry_id,
        model,
        vec_to_text(list(embedding)),
    )


async def lookup_kind(conn: Conn, target_id: UUID) -> Inquiry.InquiryKind:
    """Resolve an inquiry id to its discriminator ``kind``.

    Raises:
      NotFoundError: ``target_id`` is not in ``inquiries`` (a 404, so every
        not-found mutation path -- add_edge, add_cost -- surfaces one rule).

    """
    kind = await conn.fetchval("SELECT kind FROM inquiries WHERE id = $1", target_id)
    if kind is None:
        raise NotFoundError(f"inquiry {target_id} not found")
    return cast(Inquiry.InquiryKind, kind)


async def lookup_kinds(
    conn: Conn,
    ids: Sequence[UUID],
    *,
    for_share: bool = False,
) -> dict[UUID, Inquiry.InquiryKind]:
    """Batch ``id -> kind`` lookup; ids absent from the result don't exist.

    One query regardless of list size. ``for_share=True`` locks the referenced
    rows until the transaction commits -- the write-time stand-in for an FK on
    columns whose JSONB / ``UUID[]`` shape rules out a real one.
    """
    if not ids:
        return {}
    lock = " FOR SHARE" if for_share else ""
    rows = await conn.fetch(
        # ``lock`` is one of two fixed literals chosen by the bool, not input.
        vetted_sql("SELECT id, kind FROM inquiries WHERE id = ANY($1::uuid[])", lock),
        list(ids),
    )
    return {cast(UUID, r["id"]): cast(Inquiry.InquiryKind, r["kind"]) for r in rows}


async def validate_list_references(
    conn: Conn,
    value: Sequence[UUID | tuple[UUID, str]],
    *,
    column: str,
) -> None:
    """Verify the inquiry refs in a list-valued column are live and well-typed.

    :attr:`ColumnSpec.references` gives the permitted target kinds. Each item
    is either a ``(target_id, declared_kind)`` 2-tuple or a bare target id.
    Referenced rows are locked ``FOR SHARE`` so they can't be purged between
    the kind check and the committing UPDATE -- the write-time stand-in for
    the FK the storage shape rules out.
    """
    if not value:
        return
    permitted = COLUMN_SPECS[column].references
    if not permitted:  # pragma: no cover -- defensive: callers gate on metadata.
        raise AssertionError(
            f"validate_list_references called for {column!r} with no references"
        )
    pairs: list[tuple[UUID, str | None]] = []
    for elt in value:
        if isinstance(elt, tuple):
            pairs.append(elt)
        else:
            pairs.append((elt, None))
    found = await lookup_kinds(conn, [p[0] for p in pairs], for_share=True)
    permitted_str = " or ".join(sorted(permitted))
    for uid, declared in pairs:
        actual = found.get(uid)
        if actual is None:
            raise NotFoundError(f"{column} target {uid} not found")
        if actual not in permitted:
            raise ConflictError(
                f"{column} target {uid} is a {actual}; entries must be {permitted_str}"
            )
        if declared is not None and declared != actual:
            raise ConflictError(
                f"{column} target {uid} declared as {declared} but is a {actual}"
            )


def _bind_reference_validators() -> None:
    """Bind :func:`validate_list_references` to every column with references.

    Lives here because the validator shares ``lookup_kinds`` with the
    edge-insert path; binding it in ``setter_dispatch`` would pull ``Conn``
    into that module. Each closure captures its own ``column`` name.
    """

    def make(column: str) -> TargetValidator:
        async def validate(conn: Conn, value: Any) -> None:
            await validate_list_references(conn, value, column=column)

        return validate

    for col, spec in COLUMN_SPECS.items():
        if spec.references:
            RUNTIME_HOOKS[col] = replace(
                RUNTIME_HOOKS.get(col, NO_HOOKS), validate=make(col)
            )


_bind_reference_validators()


async def insert_edge(
    conn: Conn,
    *,
    from_id: UUID,
    from_kind: Inquiry.InquiryKind,
    to_id: UUID,
    edge_kind: Edge.Kind,
    priority: Issue.Priority | None = None,
    note: str = "",
    valence: float | None = None,
    labels: Sequence[str] = (),
) -> tuple[bool, Inquiry.InquiryKind]:
    """Insert one acyclic ``edges`` row; return ``(inserted, to_kind)``.

    ``valence`` is normalized through :func:`validate_edge_valence`: a citation
    (proves / favors) is stored with a concrete in-range value (an unset value
    defaults to :data:`CITATION_VALENCE_DEFAULT`, never NULL); a structural edge
    stores NULL regardless of the argument. This is the single boundary the
    citation-valence invariant is enforced at.
    """
    to_kind = await lookup_kind(conn, to_id)
    validate_edge_priority(edge_kind, priority)
    valence = validate_edge_valence(edge_kind, valence)
    await _reject_edge_cycle(conn, from_id=from_id, to_id=to_id, edge_kind=edge_kind)
    inserted_from = await conn.fetchval(
        "INSERT INTO edges "
        "(from_id, from_kind, to_id, to_kind, edge_kind, priority, "
        "note, valence, labels) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
        "ON CONFLICT (from_id, to_id, edge_kind) DO NOTHING "
        "RETURNING from_id",
        from_id,
        from_kind,
        to_id,
        to_kind,
        edge_kind,
        priority,
        # "Unset is NULL" for note/labels: an absent note / empty label set
        # stores NULL on the live edges table, not '' / '{}'. The change-log
        # mirror coerces these back for its presence CHECK.
        empty_optional_to_none(note),
        valence,
        # Canonicalize here (strip/drop-blanks/dedup) so labels are normalized
        # regardless of caller, then collapse the empty result to NULL.
        empty_optional_to_none(list(canonical_strs(labels))),
    )
    return (inserted_from is not None, to_kind)


async def infer_produced_endpoints(
    conn: Conn,
    *,
    from_id: UUID,
    to_id: UUID,
) -> tuple[UUID, UUID, Edge.Kind] | None:
    """Order ``{from_id, to_id}`` as ``(producer, produced, winner)`` or ``None``.

    Implements the definition of provenance (see :attr:`Inquiry.produced_by`):
    the YOUNGER vertex was produced by the OLDER, inferred from the first edge
    between the pair. The age order is always ``older`` (producer) vs ``younger``
    (produced) by :attr:`Inquiry.created` (a created-time tie breaks by id, so
    the choice is stable per pair); this function decides only *whether* to
    stamp. The caller stamps the ``produced_by`` edge child -> parent
    (from=produced, to=producer).

    Inference is universal: every edge kind stores younger -> older, so a first
    edge of any kind implies ``younger produced_by older``. The whole pair-edge
    set is read (not just the triggering edge) so the outcome is independent of
    insert order within a create-time batch:

    * Idempotency: if the pair already carries a ``produced_by`` (it ranks first
      in :data:`PRODUCED_INFERENCE_PRECEDENCE` and is the lone member of
      :data:`PRODUCED_INFERENCE_SUPPRESSED`), nothing is inferred -- a second one
      would add nothing.
    * Precedence otherwise only selects WHICH present kind labels the audit
      reason (:data:`PRODUCED_INFERENCE_PRECEDENCE`); every kind yields the same
      younger -> older edge, so the choice is cosmetic, not directional.

    Args:
      conn: The open transaction the triggering edge was inserted on.
      from_id: One endpoint of the just-inserted edge.
      to_id: The other endpoint.

    Returns:
      endpoints: ``(producer_id, produced_id, winning_kind)`` -- the older
        producer, the younger produced, and the kind that drove the inference
        (for the audit reason) -- or ``None`` when the pair already carries a
        ``produced_by`` (idempotent skip).

    """
    kinds = {
        cast(Edge.Kind, r["edge_kind"])
        for r in await conn.fetch(
            "SELECT DISTINCT edge_kind FROM edges "
            "WHERE (from_id = $1 AND to_id = $2) OR (from_id = $2 AND to_id = $1)",
            from_id,
            to_id,
        )
    }
    winner: Edge.Kind | None = next(
        (kind for kind in PRODUCED_INFERENCE_PRECEDENCE if kind in kinds), None
    )
    if winner is None or winner in PRODUCED_INFERENCE_SUPPRESSED:
        return None
    rows = await conn.fetch(
        "SELECT id, created FROM inquiries WHERE id = ANY($1::uuid[])",
        [from_id, to_id],
    )
    created = {cast(UUID, r["id"]): cast(datetime, r["created"]) for r in rows}
    # Older first; a created-time tie breaks by id so the order is deterministic.
    older, younger = sorted((from_id, to_id), key=lambda i: (created[i], i.bytes))
    return older, younger, winner


# The edge kinds each annotation column may be non-NULL on, read ONCE from the
# ``Edge`` field metadata so the validators, the schema CHECK generator, and the
# dataclass never drift: ``applies_to_edge_kinds`` is the single source of truth.
_EDGE_ANNOTATION_KINDS: dict[str, frozenset[str]] = {
    name: spec.applies_to_edge_kinds
    for name, spec in column_specs(Edge).items()
    if spec.applies_to_edge_kinds is not None
}

_VALID_EDGE_KINDS: frozenset[str] = frozenset(get_args(Edge.Kind.__value__))


def _reject_unknown_edge_kind(edge_kind: Edge.Kind) -> None:
    """Reject an ``edge_kind`` outside the closed :data:`Edge.Kind` set.

    ``edge_kind`` is typed as a closed Literal, but the runtime does not enforce
    it. Validating membership here, before any annotation guard, stops a bogus
    kind from silently clearing the priority / valence guards (which only
    *positively* gate the known kinds) and surfaces it as a clean 4xx.
    """
    if edge_kind not in _VALID_EDGE_KINDS:
        raise ValidationError(f"unknown edge kind {edge_kind!r}")


def validate_edge_priority(
    edge_kind: Edge.Kind,
    priority: Issue.Priority | None,
) -> None:
    """Reject a priority on an edge kind that cannot carry one.

    Contextual priority is carried only by the kinds in
    ``Edge.priority``'s ``applies_to_edge_kinds`` (the two Issue-to-Issue kinds).
    A value on any other edge is a caller error; the app-layer guard turns it
    into a clean :class:`ValidationError` (4xx) instead of a raw mid-transaction
    CHECK violation (500). Sibling of :func:`validate_edge_valence`.
    """
    _reject_unknown_edge_kind(edge_kind)
    if priority is None or edge_kind in _EDGE_ANNOTATION_KINDS["priority"]:
        return
    raise ValidationError(f"{edge_kind} edges cannot carry priority")


def validate_edge_valence(
    edge_kind: Edge.Kind,
    valence: float | None,
) -> float | None:
    """Return the storage-normalized valence for ``edge_kind``, or raise.

    The single storage-boundary guard for the citation-valence invariant
    (:attr:`Edge.valence`), called by both the create path (:func:`insert_edge`)
    and the annotation-edit path so every entry point obeys one rule:

    * A citation kind (``proves`` / ``favors``, from ``applies_to_edge_kinds``)
      always carries a concrete in-range valence. An unset (``None``) value
      defaults to :data:`CITATION_VALENCE_DEFAULT` -- a citation is never stored
      NULL. An out-of-range or non-finite value is a caller error.
    * A structural kind carries no valence: a non-``None`` value is a caller
      error, and the stored column is ``None``.

    Returning the normalized value (rather than only raising) lets callers store
    exactly what the invariant requires without duplicating the defaulting rule.
    All errors are :class:`ValidationError` (clean 4xx) instead of a raw
    mid-transaction DB CHECK violation (500). Mirrors
    :func:`validate_edge_priority`.

    Returns:
      stored_valence: ``None`` for a structural edge; an in-range float in
        ``[-1, 1]`` for a citation (the default when unset).

    """
    _reject_unknown_edge_kind(edge_kind)
    if edge_kind not in _EDGE_ANNOTATION_KINDS["valence"]:
        if valence is not None:
            raise ValidationError(f"{edge_kind} edges cannot carry valence")
        return None
    if valence is None:
        return CITATION_VALENCE_DEFAULT
    # ``not -1 <= v <= 1`` also rejects NaN (every comparison with NaN is False).
    if not -1.0 <= valence <= 1.0:
        raise ValidationError(f"valence must be in [-1, 1]; got {valence}")
    return valence


async def _reject_edge_cycle(
    conn: Conn,
    *,
    from_id: UUID,
    to_id: UUID,
    edge_kind: Edge.Kind,
) -> None:
    """Reject ``from_id -> to_id`` when it closes a cycle within one edge kind.

    Acyclicity is per-edge-kind: a ``produces`` and a ``supersedes``
    between the same nodes are disjoint relations, not a cycle. The advisory
    lock is per-kind too, so writers on unrelated kinds don't serialize.

    A kind whose :attr:`EdgeKindPolicy.enforces_acyclicity` is ``False`` (a
    graph we do not own, e.g. ``cites_paper`` bibliographies) skips the cycle
    walk entirely -- mutual citation is valid data. The self-loop bar still
    applies to every kind.
    """
    if from_id == to_id:
        raise ValidationError(f"{edge_kind} edge {from_id} -> {to_id} is a self-loop")
    # ``edge_kind`` is typed as ``Edge.Kind`` (a closed Literal), but Python's
    # runtime doesn't enforce that. An out-of-set value would still hash and
    # lock, partitioning the DAG namespace into a kind nobody else can reach.
    # Reject membership with a raise (not a bare ``assert``, which ``python -O``
    # strips) so a future internal caller mistake surfaces at the call site
    # rather than silently as a leaked lock.
    if edge_kind not in get_args(Edge.Kind.__value__):
        raise ValidationError(f"_reject_edge_cycle: unknown edge_kind {edge_kind!r}")
    if not EDGE_POLICIES[edge_kind].enforces_acyclicity:
        return
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"trackinizer.edge_dag.{edge_kind}",
    )
    cycle = await conn.fetchval(
        "WITH RECURSIVE descendants(id) AS ("
        "    SELECT $1::uuid "
        "    UNION "
        "    SELECT e.to_id FROM edges e "
        "    JOIN descendants d ON e.from_id = d.id "
        "    WHERE e.edge_kind = $3"
        ") SELECT EXISTS (SELECT 1 FROM descendants WHERE id = $2)",
        to_id,
        from_id,
        edge_kind,
    )
    if cycle:
        raise ConflictError(
            f"{edge_kind} edge {from_id} -> {to_id} would create a cycle"
        )
