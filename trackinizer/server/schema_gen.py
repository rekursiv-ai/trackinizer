"""Schema-time SQL generators driven by ``ColumnSpec`` metadata.

The ``change_log`` table mirrors every editable column of ``inquiries``
with paired ``old_X`` / ``new_X`` columns plus matching CHECK
constraints. Hand-written, every new editable column took three
correlated edits (inquiries column, change_log mirror, populated-iff
CHECK), with high drift risk. Now the mirror block is generated from
the same :class:`ColumnSpec` metadata that drives the setter dispatch
-- ``schema.sql`` contains a single ``{change_log_mirror}`` placeholder
and ``bootstrap()`` substitutes the generated body.
"""

from __future__ import annotations

from typing import Final, Literal, cast, get_args

import re

from trackinizer.server.setter_dispatch import COLUMN_SPECS
from trackinizer.types.agent_session_events import Kind
from trackinizer.types.change_log import Change
from trackinizer.types.columns import ColumnSpec, column_specs
from trackinizer.types.edges import Edge, kind_group_members
from trackinizer.types.inquiries import Artifact, Inquiry


def _bare_field(col: str, spec: ColumnSpec) -> str:
    """Recover the bare field name from a storage column name.

    Inverse of :func:`storage_name`: strips the ``<kind>_`` prefix a
    kind-specific column carries, so a ``sql_check`` authored against
    the bare field (``source_kind IN (...)``) can be rewritten to the
    storage column (``paper_source_kind``).
    """
    owners = spec.applies_to_inquiry_kinds
    if owners is None or len(owners) != 1:
        return col
    (owner,) = owners
    prefix = f"{owner.lower()}_"
    return col.removeprefix(prefix)


def column_check_body(col: str, spec: ColumnSpec) -> str:
    """Compose the full ``CHECK`` predicate body for one column.

    Concatenates :attr:`ColumnSpec.sql_check` (membership / range) with
    a ``cardinality(col) >= N`` clause derived from
    :attr:`ColumnSpec.min_items`. Returns ``""`` when neither
    constraint applies. ``cardinality`` (not ``array_length(col, 1)``)
    because the latter returns NULL on empty arrays, and CHECK
    treats NULL as pass -- so the cardinality bound would silently
    accept empty. Both inquiry-side per-kind columns and change_log
    ``old_/new_`` mirrors derive their value-shape CHECKs from this
    helper so cardinality lives in one place.

    ``sql_check`` bodies are authored with the bare field name (e.g.
    ``"source_kind IN (...)"``); when the storage ``col`` carries a kind
    prefix, the bare token is rewritten to ``col`` by word boundary so
    unrelated tokens stay intact.
    """
    parts: list[str] = []
    if spec.sql_check:
        check = spec.sql_check
        bare = _bare_field(col, spec)
        if bare != col:
            check = re.sub(rf"\b{re.escape(bare)}\b", col, check)
        parts.append(check)
    if spec.min_items > 0:
        parts.append(f"cardinality({col}) >= {spec.min_items}")
    return " AND ".join(parts)


# Per-kind sequence name. Derived so the naming convention
# (``seq_<lowercased_kind>``) is declared once.
SEQ_FOR_KIND: dict[Inquiry.InquiryKind, str] = {
    kind: f"seq_{kind.lower()}"
    for kind in cast(
        tuple[Inquiry.InquiryKind, ...], get_args(Inquiry.InquiryKind.__value__)
    )
}


# Canonical column ordering for the change_log mirror block. Matches
# the order in the original hand-written schema so a diff between
# old/new is empty.
CHANGE_LOG_COLUMN_ORDER: Final[tuple[str, ...]] = (
    "title",
    "description",
    "labels",
    "owner",
    "account",
    "subscribers",
    "status",
    "belief_judgement",
    "belief_confidence",
    "issue_kind",
    "issue_validation",
    "issue_priority",
    "experiment_outcome",
    "experiment_config",
    "paper_abstract",
    "paper_authors",
    "paper_publication_type",
    "paper_venue",
    "paper_subvenue",
    "paper_publish_date",
    "paper_source",
    "paper_google_scholar_cluster_id",
    "paper_google_scholar_cites_id",
    "codechange_sha",
    "webresult_url",
    "websearch_query",
    "websearch_provider",
    "experiment_codechanges",
    "agentsession_cli",
    "agentsession_cli_session_id",
    "agentsession_started",
    "agentsession_ended",
    "agentsession_rooms",
)

_EDGE_METADATA_COLUMN_ORDER: Final[tuple[str, ...]] = (
    "priority",
    "note",
    "valence",
    "labels",
)

_EDGE_METADATA_SPECS: dict[str, ColumnSpec] = column_specs(Edge)


INQUIRY_KIND_ORDER: Final[tuple[Inquiry.InquiryKind, ...]] = (
    "Issue",
    "Artifact",
    "Belief",
    "Experiment",
    "Paper",
    "CodeChange",
    "WebResult",
    "WebSearch",
    "AgentSession",
)
"""Canonical order for emitting per-kind column blocks. Covers every
``Inquiry.InquiryKind`` value -- a future column with
``applies_to_inquiry_kinds={"Artifact"}`` would otherwise ``KeyError`` at
module load. ``generate_inquiry_kind_columns`` asserts this set
matches the InquiryKind Literal so adding a new kind without
updating the order is caught early.
"""


def substitute_schema_placeholders(body: str) -> str:
    """Replace generated-block placeholders in a migration body with the
    matching SQL generated from :data:`COLUMN_SPECS` metadata.

    Migration files keep a single ``{name}`` token per generated section
    so the metadata-driven blocks live exactly once in Python. New
    placeholders are added here when the schema grows new generated
    sections.
    """
    return (
        body.replace("{change_log_mirror}", generate_change_log_mirror())
        .replace("{edge_metadata_columns}", generate_edge_metadata_columns())
        .replace("{edge_metadata_mirror_old}", generate_edge_metadata_mirror_old())
        .replace("{edge_metadata_mirror_new}", generate_edge_metadata_mirror_new())
        .replace("{inquiry_kind_columns}", generate_inquiry_kind_columns())
        .replace("{change_log_kind_matrix}", generate_change_log_kind_matrix())
        .replace("{per_kind_sequences}", generate_per_kind_sequences())
        # Literal-set placeholders -- derived from the closed-set
        # ``Literal`` types so the SQL CHECK lists can't drift from
        # the Python type when the set grows. ``Edge.Kind`` has grown
        # several times during this project; this catches it.
        .replace("{edge_kinds}", quote_literal(Edge.Kind))
        .replace("{inquiry_kinds}", quote_literal(Inquiry.InquiryKind))
        .replace("{artifact_kinds}", quote_literal(Artifact.Kind))
        # The citation-target set {Belief, Experiment}; the proves/favors
        # to-side. Single-sourced from the ``claimable`` kind-group so it
        # cannot drift from the EdgeKindPolicy topology.
        .replace(
            "{claimable_kinds}",
            ", ".join(f"'{k}'" for k in kind_group_members("claimable")),
        )
        # The historical-citation endpoint set {Paper}; the cites_paper from/to
        # side. Single-sourced from the ``paper`` kind-group so it cannot drift
        # from the EdgeKindPolicy topology.
        .replace(
            "{paper_kinds}",
            ", ".join(f"'{k}'" for k in kind_group_members("paper")),
        )
        .replace("{change_kinds}", quote_literal(Change.Kind))
        .replace("{agent_session_event_kinds}", quote_literal(Kind))
    )


def _quote_values(values: frozenset[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


def quote_literal(literal_alias: object) -> str:
    """Render a ``Literal[...]`` type alias's members as a SQL ``IN (...)`` body.

    The PEP 695 ``type X = Literal[...]`` syntax wraps the underlying
    ``Literal`` in a ``TypeAliasType``; ``get_args`` on the alias is
    empty, so we resolve the alias's ``__value__`` first. Plain
    ``Literal[...]`` (no alias) is also accepted -- ``get_args``
    returns the members directly.

    Raises ``AssertionError`` for a non-Literal target -- bootstrap
    would otherwise emit ``CHECK (... IN ())`` and Postgres would
    syntax-error with no pointer back to the wrong type alias.
    """
    target = getattr(literal_alias, "__value__", literal_alias)
    args = get_args(target)
    if not args:
        raise AssertionError(
            f"quote_literal({literal_alias!r}): no Literal members; "
            "either pass a Literal[...] or a PEP-695 type alias wrapping one"
        )
    if not all(isinstance(a, str) for a in args):
        raise AssertionError(
            f"quote_literal({literal_alias!r}): non-string members "
            f"{[a for a in args if not isinstance(a, str)]}"
        )
    return ", ".join(f"'{v}'" for v in args)


def generate_inquiry_kind_columns() -> str:
    """Render every kind-specific column declaration on ``inquiries``.

    Walks :data:`COLUMN_SPECS` for columns with a non-``None``
    ``applies_to_inquiry_kinds`` and emits, per column:
    * ``column TYPE CHECK (CASE WHEN <applies> THEN column IS NOT NULL
      AND <sql_check> ELSE column IS NULL END),``
    * the ``AND <sql_check>`` clause is omitted when the spec doesn't
      provide one (e.g. ``outcome``, ``source``).

    Substituted into ``schema.sql``'s ``{inquiry_kind_columns}`` slot
    at bootstrap; this body was previously ~100 lines of mechanical
    CASE-WHEN duplication.
    """
    # Sanity-check: the order tuple must cover every concrete kind so
    # adding a new Inquiry subclass doesn't silently KeyError below.
    declared_kinds = set(get_args(Inquiry.InquiryKind.__value__))
    missing = declared_kinds - set(INQUIRY_KIND_ORDER)
    if missing:  # pragma: no cover -- defensive check that fires only if a new Inquiry kind is added without updating INQUIRY_KIND_ORDER.
        raise AssertionError(
            f"INQUIRY_KIND_ORDER missing {sorted(missing)}; "
            "every Inquiry.InquiryKind value must appear so the schema "
            "generator can place its per-kind columns."
        )
    # Group columns by their applicable kind. Columns with multi-kind
    # applies_to_inquiry_kinds are not currently used; if added, this generator
    # would need to merge the WHEN clauses across kinds.
    by_kind: dict[Inquiry.InquiryKind, list[tuple[str, ColumnSpec]]] = {
        k: [] for k in INQUIRY_KIND_ORDER
    }
    for col, spec in COLUMN_SPECS.items():
        if spec.applies_to_inquiry_kinds is None:
            continue
        if (
            len(spec.applies_to_inquiry_kinds) != 1
        ):  # pragma: no cover -- no current column applies to multiple kinds; raise documents the codegen gap.
            raise NotImplementedError(
                f"column {col!r} applies to multiple kinds; generator "
                "needs a multi-kind WHEN clause"
            )
        (kind,) = spec.applies_to_inquiry_kinds
        by_kind[cast(Inquiry.InquiryKind, kind)].append((col, spec))
    sections: list[str] = []
    for kind in INQUIRY_KIND_ORDER:
        cols = by_kind[kind]
        if not cols:
            continue
        sections.append(
            f"    -- -- {kind} -- generated by generate_inquiry_kind_columns"
        )
        for col, spec in cols:
            body = column_check_body(col, spec)
            presence = f"{col} IS NOT NULL" if spec.required else "TRUE"
            extra = f"\n         AND ({col} IS NULL OR {body})" if body else ""
            sections.append(
                f"    {col} {spec.sql_type} CHECK (\n"
                f"        CASE WHEN kind = '{kind}'\n"
                f"            THEN {presence}{extra}\n"
                f"            ELSE {col} IS NULL\n"
                f"        END\n"
                f"    ),"
            )
    # Trim the trailing comma on the last column so the SQL stays
    # syntactically valid when this block is the final element in the
    # CREATE TABLE column list. The caller (schema.sql) handles the
    # adjacent comma/no-comma context via placement.
    if sections and sections[-1].endswith("),"):
        sections[-1] = sections[-1][:-1]  # drop the comma
    return "\n".join(sections)


def generate_per_kind_sequences() -> str:
    """Render ``CREATE SEQUENCE`` statements for every kind in
    :data:`SEQ_FOR_KIND`.

    Adding a new Inquiry subclass automatically gets a sequence -- no
    schema edit needed.
    """
    return "\n".join(
        f"CREATE SEQUENCE IF NOT EXISTS {name};" for name in SEQ_FOR_KIND.values()
    )


def generate_change_log_kind_matrix() -> str:
    """Render kind-specific change_log CHECKs constraining ``subject_kind``.

    For every editable column with a single ``applies_to_inquiry_kinds`` entry,
    emit ``CHECK (kind <> '<change_kind>' OR subject_kind = '<kind>')``.
    The Store's dispatch rejects direct-SQL kind mismatches at write
    time; this is the schema-level backstop.
    """
    lines: list[str] = []
    for col in CHANGE_LOG_COLUMN_ORDER:
        spec = COLUMN_SPECS[col]
        if (
            spec.applies_to_inquiry_kinds is None
            or len(spec.applies_to_inquiry_kinds) != 1
        ):
            continue
        (kind,) = spec.applies_to_inquiry_kinds
        lines.append(f"    CHECK (kind <> '{col}' OR subject_kind = '{kind}'),")
    if lines:
        # Strip the trailing comma off the final clause so it can be
        # the last item before the table-level closing paren.
        lines[-1] = lines[-1][:-1]
    return "\n".join(lines)


def _edge_column_check_body(
    col: str,
    spec: ColumnSpec,
    *,
    edge_kind_col: str,
) -> str:
    body = column_check_body(col, spec)
    if spec.applies_to_edge_kinds is None:
        return body
    edge_kind_check = (
        f"{edge_kind_col} IN ({_quote_values(spec.applies_to_edge_kinds)})"
    )
    if not body:
        return edge_kind_check
    return f"{body} AND {edge_kind_check}"


def generate_edge_metadata_columns() -> str:
    """Render edge annotation columns and value CHECKs from :class:`Edge`."""
    declarations: list[str] = []
    value_checks: list[str] = []
    for col in _EDGE_METADATA_COLUMN_ORDER:
        spec = _EDGE_METADATA_SPECS[col]
        sql_type = spec.sql_type or "TEXT"
        default = (
            f" NOT NULL DEFAULT {spec.sql_default}"
            if spec.sql_default and spec.required
            else ""
        )
        declarations.append(f"    {col:14} {sql_type}{default},")
        if body := _edge_column_check_body(col, spec, edge_kind_col="edge_kind"):
            value_checks.append(f"    CHECK ({col} IS NULL OR {body}),")
    lines = declarations + value_checks
    lines[-1] = lines[-1].removesuffix(",")
    return "\n".join(lines)


def generate_edge_metadata_mirror_old() -> str:
    """Render old-side change_log edge metadata mirrors from :class:`Edge`."""
    return _generate_edge_metadata_mirror("old")


def generate_edge_metadata_mirror_new() -> str:
    """Render new-side change_log edge metadata mirrors from :class:`Edge`."""
    return _generate_edge_metadata_mirror("new")


def _generate_edge_metadata_mirror(prefix: Literal["old", "new"]) -> str:
    declarations: list[str] = []
    value_checks: list[str] = []
    for col in _EDGE_METADATA_COLUMN_ORDER:
        spec = _EDGE_METADATA_SPECS[col]
        sql_type = spec.sql_type or "TEXT"
        mirror_col = f"{prefix}_edge_{col}"
        declarations.append(f"    {mirror_col} {sql_type},")
        body = _edge_column_check_body(
            col,
            spec,
            edge_kind_col=f"{prefix}_peer_edge_kind",
        )
        if body:
            pattern = re.compile(rf"\b{re.escape(col)}\b")
            check = pattern.sub(mirror_col, body)
            value_checks.append(f"    CHECK ({mirror_col} IS NULL OR {check}),")
    return "\n".join(declarations + value_checks)


def generate_change_log_mirror() -> str:
    """Render the change_log per-column declarations + CHECK constraints.

    Walks :data:`COLUMN_SPECS` in the canonical
    :data:`CHANGE_LOG_COLUMN_ORDER` and emits, per column:
    * ``old_X TYPE,`` and ``new_X TYPE,`` declarations
    * a ``(old_X IS NOT NULL) = (kind = '<change_kind>')`` gate per side
    * when the column has a ``sql_check`` (closed-set values, ranges),
      ``CHECK (old_X IS NULL OR <sql_check rewritten with old_/new_ prefix>),``

    Substituted into ``schema.sql``'s ``{change_log_mirror}`` slot at
    bootstrap; the mirror block is now derived from ``ColumnSpec``
    metadata rather than hand-typed in two places.
    """
    declarations: list[str] = []
    populated_iff: list[str] = []
    value_checks: list[str] = []
    for col in CHANGE_LOG_COLUMN_ORDER:
        spec = COLUMN_SPECS[col]
        sql_type = spec.sql_type or "TEXT"
        declarations.append(f"    old_{col} {sql_type},")
        declarations.append(f"    new_{col} {sql_type},")
        # The storage column name IS the Change.Kind value for a field
        # edit, so ``col`` doubles as the kind string in the gate.
        if spec.required:
            populated_iff.append(
                f"    CHECK ((old_{col} IS NOT NULL) = (kind = '{col}')),"
            )
            populated_iff.append(
                f"    CHECK ((new_{col} IS NOT NULL) = (kind = '{col}')),"
            )
        else:
            populated_iff.append(f"    CHECK (kind = '{col}' OR old_{col} IS NULL),")
            populated_iff.append(f"    CHECK (kind = '{col}' OR new_{col} IS NULL),")
        body = column_check_body(col, spec)
        if body:
            # ``\b`` word-boundary substitution so e.g. a future
            # ``sql_check`` mentioning ``description_reason`` alongside
            # ``description`` doesn't get mangled into
            # ``old_description_reason``. The closed set of column
            # names is a regular identifier so ``\b`` is the right
            # boundary.
            pattern = re.compile(rf"\b{re.escape(col)}\b")
            old_check = pattern.sub(f"old_{col}", body)
            new_check = pattern.sub(f"new_{col}", body)
            value_checks.append(f"    CHECK (old_{col} IS NULL OR {old_check}),")
            value_checks.append(f"    CHECK (new_{col} IS NULL OR {new_check}),")
    return (
        "    -- -- Per-column mirror block: ``old_X`` / ``new_X`` columns,\n"
        "    -- -- ``populated iff applicable`` gates, and closed-set value\n"
        "    -- -- CHECKs. Generated from types.ColumnSpec metadata\n"
        "    -- -- by ``generate_change_log_mirror()``.\n"
        + "\n".join(declarations)
        + "\n\n"
        + "\n".join(populated_iff)
        + "\n\n"
        + "\n".join(value_checks)
    )
