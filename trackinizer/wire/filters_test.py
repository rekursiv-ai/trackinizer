from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import re

import pytest

from trackinizer.types.columns import (
    column_specs,
    flat_column_specs,
    storage_name,
)
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
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
from trackinizer.wire import filters
from trackinizer.wire.filters import (
    NON_NULLABLE_COLUMNS,
    canonical_filter_field,
    validate_presence_op,
)


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def isolate_clause_cache() -> Iterator[None]:
    """Exercise validation instead of cached answers from earlier pytest sessions."""
    filters.validate_clause.cache_clear()
    yield
    filters.validate_clause.cache_clear()


def test_every_kind_specific_bare_field_canonicalizes_to_its_storage_column() -> None:
    """Every kind-specific bare field name aliases to its ``<kind>_`` column.

    GSI-02 class: a kind-specific column stores under a ``<kind>_`` prefix
    (``paper_source``), but the CLI filters by the bare field (``source``). The
    alias map DERIVES these (``_kind_specific_aliases``); this recomputes the
    expected set the same way so a new kind-specific field is filterable with no
    edit -- a bare filter the CLI accepts can never 400 for a missing alias
    (the original ``google_scholar_id`` bug, now covering the cluster/cites
    split too).
    """
    aliases = filters._kind_specific_aliases()
    for cls in KIND_TO_CLASS.values():
        for name, spec in column_specs(cls).items():
            storage = storage_name(name, spec)
            if storage != name:
                assert aliases[name] == storage
                if spec.list_verb_stem:
                    assert aliases[spec.list_verb_stem] == storage
                assert canonical_filter_field(name) == storage, (
                    f"bare filter field {name!r} does not resolve to its storage "
                    f"column {storage!r}"
                )
    # The two Google Scholar handles (cluster identity + cited-by pivot) each
    # resolve -- the split that replaced the single google_scholar_id.
    assert (
        canonical_filter_field("google_scholar_cluster_id")
        == "paper_google_scholar_cluster_id"
    )
    assert (
        canonical_filter_field("google_scholar_cites_id")
        == "paper_google_scholar_cites_id"
    )


@pytest.mark.parametrize("field", ["title", "issue_priority", "unknown_filter_column"])
def test_canonical_and_unknown_filter_fields_pass_through(field: str) -> None:
    assert canonical_filter_field(field) == field


def test_agentsession_filter_aliases_resolve_to_storage_columns() -> None:
    """AgentSession's bare CLI field names canonicalize to their storage names.

    The CLI filters an AgentSession by the bare field (``cli``, ``started``),
    the kind already in scope; the canonical SQL column is the
    ``agentsession_``-prefixed storage name. Omitting these aliases 400s a
    ``filter cli`` query, so each must resolve here.
    """
    assert canonical_filter_field("cli") == "agentsession_cli"
    assert canonical_filter_field("cli_session_id") == "agentsession_cli_session_id"
    assert canonical_filter_field("started") == "agentsession_started"
    assert canonical_filter_field("ended") == "agentsession_ended"
    assert canonical_filter_field("rooms") == "agentsession_rooms"


@pytest.mark.parametrize(
    ("field", "column"),
    [
        ("author", "paper_authors"),
        ("authors", "paper_authors"),
        ("codechange", "experiment_codechanges"),
        ("codechanges", "experiment_codechanges"),
    ],
)
def test_list_filter_aliases_resolve_to_storage_columns(
    field: str,
    column: str,
) -> None:
    assert canonical_filter_field(field) == column


def test_non_nullable_columns_match_schema_metadata() -> None:
    """The NOT-NULL set tracks the column specs, not a hand-kept list.

    Drift guard: a future ``required=True`` field (or a new flattened cost
    axis) must appear here automatically, else ``isnull`` / ``notnull``
    validation silently breaks on it. The expected set is recomputed the
    same way the source derives it, so a hardcoded edit that desyncs from
    the specs fails loudly.
    """
    identity = {"id", "kind", "seq", "locked", "created", "modified"}
    expected = set(identity)
    for src in (
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
    ):
        for name, flat in flat_column_specs(src).items():
            # A column required of one kind is NULL on the other kinds' rows.
            kind_free = flat.spec.applies_to_inquiry_kinds is None
            if (flat.spec.required and kind_free) or flat.spec.flatten is not None:
                expected.add(storage_name(name, flat.spec))
    assert filters._derive_non_nullable_columns() == expected
    assert set(NON_NULLABLE_COLUMNS) == expected
    assert "agentsession_audience" not in NON_NULLABLE_COLUMNS


def test_validate_presence_op_rejects_not_null_allows_nullable() -> None:
    """``isnull`` / ``notnull`` reject NOT-NULL columns and pass nullable ones."""
    assert validate_presence_op("id", "isnull") is not None
    assert validate_presence_op("status", "notnull") is not None
    assert validate_presence_op("marginal_cost_agent_usd", "isnull") is not None
    assert validate_presence_op("owner", "isnull") is None
    assert validate_presence_op("issue_kind", "notnull") is None
    # A value-bearing op on a NOT-NULL column is fine; the gate is presence-only.
    assert validate_presence_op("status", "is") is None


@pytest.mark.parametrize("field", ["title", "agent-cost"])
@pytest.mark.parametrize("op", ["isnull", "notnull"])
def test_presence_filter_refuses_non_nullable_fields(
    field: str,
    op: filters.FilterOp,
) -> None:
    with pytest.raises(ValueError, match="NOT NULL"):
        filters.Filter(field=field, op=op, value="")


@pytest.mark.parametrize(
    ("op", "value", "message"),
    [
        ("unknown", "", "unknown filter op"),
        ("isnull", "present", "takes no value"),
        ("is", "x" * (filters.MAX_FILTER_VALUE_CHARS + 1), "exceeds 512"),
    ],
)
def test_clause_rejects_invalid_operator_and_operand_shape(
    op: str,
    value: str,
    message: str,
) -> None:
    rejection = filters.validate_clause("owner", op, value)
    assert rejection is not None
    assert message in rejection


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" \t-1_234.5_0e+2\r\n\v\f", Decimal(-123_450)),
        ("+.5", Decimal("0.5")),
        ("1.", Decimal(1)),
        ("Infinity", Decimal("Infinity")),
        ("-inf", Decimal("-Infinity")),
        ("1.00000000000000001", Decimal("1.00000000000000001")),
        ("", None),
        ("1__0", None),
        ("1_", None),
        ("_1", None),
        ("X1X", None),
        ("\uff11", None),
        ("\u00a01", None),
    ],
)
def test_numeric_operands_preserve_postgres_syntax_and_precision(
    value: str,
    expected: Decimal | None,
) -> None:
    assert filters.as_numeric(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("NaN", True), (" nAn ", True), ("1", False), ("invalid", False)],
)
def test_nan_detection_distinguishes_invalid_and_finite_operands(
    value: str,
    *,
    expected: bool,
) -> None:
    assert filters.is_nan(value) is expected


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("[[:digit:]]", "POSIX bracket construct"),
        ("[", "invalid regex"),
        ("a{999999999999,}", "invalid regex"),
        (r"\N{LATIN CAPITAL LETTER A}", "Python-only"),
        (r"\z", "use '\\Z' instead"),
        ("(?>a)", "not a construct Postgres implements"),
        ("(?=a)(?>a)", "not a construct Postgres implements"),
        ("(?:)(?>a)", "not a construct Postgres implements"),
        ("(?a)a", "not a construct Postgres implements"),
        ("(?n)a", "valid Postgres flag"),
        ("(?n:a)", "valid Postgres flag"),
        ("(?i:a)", "scopes its flags"),
        ("a*+", "possessive quantifier"),
        ("a++", "possessive quantifier"),
        ("a?+", "possessive quantifier"),
        (r"\.*+", "possessive quantifier"),
        ("a{2,3}+", "possessive quantifier"),
        ("a{1}b{2}+{x", "possessive quantifier"),
        ("a{2", "never closed"),
        ("a{x{2", "never closed"),
        ("a{1x}", "never closed"),
        ("(?i)é", "case-folds non-ASCII"),
        ("(?i)[\u017f]", "case-folds non-ASCII"),
        (r"[\D]", "has no Python spelling"),
        (r"[\S]", "has no Python spelling"),
        (r"[\b]\b", "word boundary"),
        (r"[\b]\B", "non-word-boundary"),
        ("(?P<name>a)", "named groups are Python-only"),
    ],
)
def test_regex_clause_names_each_incompatible_construct(
    pattern: str,
    message: str,
) -> None:
    rejection = filters.validate_clause("title", "re", pattern)
    assert rejection is not None
    assert message in rejection
    with pytest.raises(ValueError, match=re.escape(message)) as raised:
        filters.Filter(field="title", op="nre", value=pattern)
    assert str(raised.value) == rejection


@pytest.mark.parametrize(
    "pattern",
    [
        "(?=a)a",
        "(?!b)a",
        "(?<=a)b",
        "(?<!b)a",
        "(?:)",
        "(?i)ascii",
        "(?i)(?#é)a",
        "(?ix)a # é",
        "é",
        "[*+]",
        "(?#*+)a",
        r"a\*+",
        "a}+",
        "a{}+",
        "a{,2}+",
        "a{٢}+",
        r"a\{2}+",
        "a{",
        "a{,",
        "a{x",
        "a{2,3}",
        "a{2}b{3}",
        "a2",
        "X+",
        r"[\b]",
        r"\yword\Y",
        r"[\d\s]",
        "[[]",
    ],
)
def test_regex_clause_preserves_shared_syntax_and_inert_lookalikes(
    pattern: str,
) -> None:
    assert filters.validate_clause("title", "re", pattern) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
