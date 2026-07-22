from __future__ import annotations

from typing import Any, cast

import contextlib
import uuid

from hypothesis import (
    given,
    settings,
    strategies as st,
)

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import cli
from trackinizer.trax.conftest import FakeClient
from trackinizer.trax.grammar import (
    COST_FIELDS,
    EDGE_ALIASES,
    EDITABLE_FIELDS,
    KIND_LOWER,
    LIST_FIELDS,
    AddCost,
    AddList,
    DeleteRow,
    EdgeAction,
    InlineCreate,
    MetricAction,
    MetricMask,
    RelationAction,
    SetField,
)
from trackinizer.trax.parser import (
    consume_edge_target,
    consume_ref,
    parse_actions,
    parse_bulk_apply,
    parse_list_query,
    parse_metric_action,
    parse_subject_list,
    ref_text,
    required_token,
)
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import SeqRef, UuidRef
from trackinizer.wire.seq_ranges import SeqRange


def test_consume_ref_uuid() -> None:
    u = "550e8400-e29b-41d4-a716-446655440000"
    ref, consumed = consume_ref([u], 0)
    assert isinstance(ref, UuidRef)
    assert ref.uuid == uuid.UUID(u)
    assert ref.expected_kind is None
    assert consumed == 1


def test_consume_ref_uuid_with_kind_hint_carries_expected_kind() -> None:
    u = "550e8400-e29b-41d4-a716-446655440000"
    ref, consumed = consume_ref([u], 0, kind_hint="Belief")
    assert ref == UuidRef(uuid=uuid.UUID(u), expected_kind="Belief")
    assert consumed == 1


def test_consume_ref_kind_seq_two_token() -> None:
    ref, consumed = consume_ref(["issue", "7"], 0)
    assert isinstance(ref, SeqRef)
    assert ref.kind == "Issue"
    assert ref.seq == 7
    assert consumed == 2


def test_consume_ref_seq_only_with_kind_hint() -> None:
    ref, consumed = consume_ref(["7"], 0, kind_hint="Issue")
    assert isinstance(ref, SeqRef)
    assert ref.kind == "Issue"
    assert ref.seq == 7
    assert consumed == 1


def test_consume_ref_seq_only_without_kind_hint_fails() -> None:
    with pytest.raises(ClientError, match="cannot parse"):
        consume_ref(["7"], 0)


def test_consume_ref_unknown_kind_fails() -> None:
    with pytest.raises(ClientError, match="cannot parse"):
        consume_ref(["notakind", "7"], 0)


def test_consume_ref_kind_without_seq_fails() -> None:
    with pytest.raises(ClientError, match="incomplete ref"):
        consume_ref(["issue"], 0)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)


# Coverage for parser.py error and edge-metadata branches.


def test_parse_list_query_bare_kind() -> None:
    """``parse_list_query`` with empty rest returns the trivial query."""
    q = parse_list_query("Issue", [])
    assert q is not None
    assert q.kinds == ("Issue",)


def test_parse_list_query_filter_missing_value_raises() -> None:

    with pytest.raises(ClientError, match="expected value for filter field"):
        parse_list_query("Issue", ["priority", "is"])


def test_parse_list_query_accepts_cost_filter() -> None:
    """``agent-cost`` is a CLI alias for the SQL column.

    The parser validates the CLI alias against ``FILTER_FIELDS_CLI``
    (so the user gets ``unknown filter field 'agent-cost'`` if they
    misspell) and then translates the alias to its canonical SQL
    column name (``marginal_cost_agent_usd``) before constructing
    the wire ``Filter``. The server only ever sees the canonical
    name.
    """
    q = parse_list_query("Issue", ["agent-cost", "gt", "0"])
    assert q is not None
    assert q.filters == (Filter(field="marginal_cost_agent_usd", op="gt", value="0"),)


def test_parse_list_query_accepts_kind_alias_filter() -> None:
    """``kind`` is a CLI alias for the ``issue_kind`` SQL column."""
    q = parse_list_query("Issue", ["kind", "is", "bug"])
    assert q is not None
    assert q.filters == (Filter(field="issue_kind", op="is", value="bug"),)


def test_parse_list_query_accepts_inherited_base_filters() -> None:
    """Base columns (labels / cost) filter on every kind (TAPI-003 / F3).

    The hand-listed CLI whitelist used to omit ``label`` and the cost
    axes for CodeChange / WebResult / WebSearch, rejecting filters the
    server accepts. The derived whitelist now honors that the base
    columns apply to all kinds.
    """
    q = parse_list_query("CodeChange", ["label", "is", "bugfix"])
    assert q is not None
    assert q.filters == (Filter(field="labels", op="is", value="bugfix"),)

    q = parse_list_query("WebResult", ["agent-cost", "gt", "0"])
    assert q is not None
    assert q.filters == (Filter(field="marginal_cost_agent_usd", op="gt", value="0"),)


def test_parse_list_query_still_rejects_kind_specific_field() -> None:
    """Per-kind columns stay rejected on the wrong kind (judgement on Issue)."""
    with pytest.raises(ClientError, match="unknown filter field"):
        parse_list_query("Issue", ["judgement", "is", "proven"])


def test_parse_bulk_apply_range_plus_mutation() -> None:
    """A range selector plus a ``field to value`` mutation is a bulk apply."""
    bulk = parse_bulk_apply("Issue", ["222..", "owner", "to", "Josh"])
    assert bulk is not None
    assert bulk.query.ranges["Issue"] == (SeqRange(start=222),)
    assert bulk.actions == (SetField(field="owner", value="Josh"),)


def test_parse_bulk_apply_filter_plus_mutation_interleaved() -> None:
    """Filter and mutation triples may interleave; the operator routes each."""
    bulk = parse_bulk_apply("Issue", ["owner", "to", "Josh", "status", "is", "active"])
    assert bulk is not None
    assert bulk.query.filters == (Filter(field="status", op="is", value="active"),)
    assert bulk.actions == (SetField(field="owner", value="Josh"),)


def test_parse_bulk_apply_accepts_nre_filter() -> None:
    """``nre`` commits a bulk-apply selector like any other filter op."""
    bulk = parse_bulk_apply("Issue", ["owner", "nre", "Dan", "status", "to", "active"])
    assert bulk is not None
    assert bulk.query.filters == (Filter(field="owner", op="nre", value="Dan"),)
    assert bulk.actions == (SetField(field="status", value="active"),)


def test_parse_bulk_apply_without_selector_is_none() -> None:
    """No range or filter means a plain create, not a bulk apply."""
    assert parse_bulk_apply("Issue", ["owner", "to", "Josh"]) is None


def test_parse_bulk_apply_bare_kind_is_not_a_selector() -> None:
    """A widening kind token is not a selector; this is a create, not bulk."""
    assert parse_bulk_apply("Issue", ["belief", "owner", "to", "Josh"]) is None


def test_parse_bulk_apply_rejects_edge_before_selector() -> None:
    """An edge token before any selector is not a bulk apply (falls through).

    ``parse_bulk_apply`` returns ``None``; the command is then rejected
    downstream by ``parse_actions`` rather than silently mutating rows.
    """
    assert parse_bulk_apply("Issue", ["blocks", "issue", "8", "222.."]) is None
    with pytest.raises(ClientError, match="unexpected token"):
        parse_actions(["blocks", "issue", "8", "222.."])


def test_parse_bulk_apply_without_mutation_is_none() -> None:
    """A filter with no mutation is a plain list query, not a bulk apply."""
    assert parse_bulk_apply("Issue", ["status", "is", "active"]) is None


def test_parse_bulk_apply_rejects_edge_mutation() -> None:
    """Once a selector commits, only field mutations may follow; edges error."""
    with pytest.raises(ClientError, match="only field mutations"):
        parse_bulk_apply("Issue", ["222..", "blocks", "issue", "8"])


def test_parse_list_query_accepts_nre_op() -> None:
    """``nre`` (negated regex) parses as a filter op like ``re``."""
    q = parse_list_query("Issue", ["owner", "nre", "Dan"])
    assert q is not None
    assert q.filters == (Filter(field="owner", op="nre", value="Dan"),)


def test_parse_list_query_isnull_takes_no_value() -> None:
    """``field isnull`` is a two-token filter; it consumes no value token."""
    q = parse_list_query("Issue", ["kind", "isnull"])
    assert q is not None
    assert q.filters == (Filter(field="issue_kind", op="isnull", value=""),)


def test_parse_list_query_notnull_takes_no_value() -> None:
    """``field notnull`` parses with no value, like ``isnull``."""
    q = parse_list_query("Issue", ["owner", "notnull"])
    assert q is not None
    assert q.filters == (Filter(field="owner", op="notnull", value=""),)


def test_parse_list_query_rejects_isnull_on_not_null_column() -> None:
    """``isnull`` / ``notnull`` are rejected on NOT-NULL columns.

    A presence test on a column that can never be NULL (id, kind, seq,
    created, modified, status, title, cost axes) is always-empty /
    always-all -- a silent wrong answer. The CLI rejects it up front.
    """
    for field, op in (
        ("id", "isnull"),
        ("status", "isnull"),
        ("title", "notnull"),
        ("agent-cost", "isnull"),
        ("seq", "notnull"),
    ):
        with pytest.raises(ClientError, match=r"NOT NULL"):
            parse_list_query("Issue", [field, op])


def test_parse_list_query_allows_isnull_on_nullable_column() -> None:
    """A nullable column still accepts the presence ops."""
    q = parse_list_query("Issue", ["owner", "isnull"])
    assert q is not None
    assert q.filters == (Filter(field="owner", op="isnull", value=""),)


def test_parse_list_query_isnull_then_next_filter() -> None:
    """A valueless ``isnull`` does not swallow the following filter field."""
    q = parse_list_query("Issue", ["kind", "isnull", "status", "is", "active"])
    assert q is not None
    assert q.filters == (
        Filter(field="issue_kind", op="isnull", value=""),
        Filter(field="status", op="is", value="active"),
    )


def test_parse_list_query_filters_agentsession_like_any_kind() -> None:
    """AgentSession is filterable: a filter validates against its columns.

    AgentSession is writable and listable on the generic ``trax <kind>``
    surface, so excluding it from filtering left a gap where any filter
    raised ``KeyError`` instead of validating. A base-column filter now
    parses, and a kind-invalid field still rejects.
    """
    q = parse_list_query("AgentSession", ["owner", "isnull"])
    assert q is not None
    assert q.filters == (Filter(field="owner", op="isnull", value=""),)

    # A field that does not apply to AgentSession is still rejected cleanly.
    with pytest.raises(ClientError, match="unknown filter field"):
        parse_list_query("AgentSession", ["judgement", "is", "proven"])


def test_parse_bulk_apply_isnull_selector() -> None:
    """A valueless ``isnull`` filter commits a bulk-apply selector."""
    bulk = parse_bulk_apply("Issue", ["kind", "isnull", "owner", "to", "Josh"])
    assert bulk is not None
    assert bulk.query.filters == (Filter(field="issue_kind", op="isnull", value=""),)
    assert bulk.actions == (SetField(field="owner", value="Josh"),)


def test_parse_list_query_unknown_op_after_filter_reports_op() -> None:
    """A bad operator between two filter fields names the operator, not bulk apply.

    ``status is active owner not re Dan`` puts ``not`` where an operator is
    expected. ``not`` is not a filter op, so the honest error is an unknown
    operator -- not the misleading ``bulk apply supports only field mutations``
    that the bulk-apply scanner used to emit on the trailing ``owner``.
    """
    with pytest.raises(ClientError, match="unknown filter operator 'not'"):
        parse_bulk_apply(
            "Issue", ["status", "is", "active", "owner", "not", "re", "Dan"]
        )


def test_parse_bare_bad_op_falls_through_to_create() -> None:
    """A leading bad operator is ambiguous with a create and falls through.

    ``owner not re Dan`` with nothing committing it to a query is
    indistinguishable from a create missing ``to``. Both ``parse_bulk_apply``
    and ``parse_list_query`` return ``None`` so the dispatcher reaches the
    create path, whose ``FIELD to VALUE`` error is the right one. Only a
    *committed* query (a prior filter or range) turns the bad operator into a
    hard ``unknown filter operator`` error.
    """
    assert parse_bulk_apply("Issue", ["owner", "not", "re", "Dan"]) is None
    assert parse_list_query("Issue", ["owner", "not", "re", "Dan"]) is None


def test_parse_list_query_committed_then_bad_op_raises() -> None:
    """A bad operator after a committed filter raises in ``parse_list_query`` too.

    The list-query path is reached for a pure query (no mutation). Once a
    filter commits the stream, a trailing mistyped operator is reported as an
    unknown operator, the same diagnosis the bulk-apply path gives.
    """
    with pytest.raises(ClientError, match="unknown filter operator 'not'"):
        parse_list_query(
            "Issue", ["status", "is", "active", "owner", "not", "re", "Dan"]
        )


def test_parse_bulk_apply_supports_list_add() -> None:
    """List add/del mutations apply per matched row."""
    bulk = parse_bulk_apply("Issue", ["status", "is", "active", "label", "add", "hot"])
    assert bulk is not None
    assert bulk.actions == (AddList(field="label", value="hot"),)


def test_parse_list_action_codechange_accepts_typed_ref() -> None:
    """`codechange add codechange N` must parse the typed ref (trax #419).

    Regression -- the ref-list append truncated to a single token, dropping
    the seq and leaving `codechange` as the value, so the typed `kind seq`
    ref form was rejected with "unexpected token". The parser now attaches
    the parsed `Ref` to the action and the verb layer resolves it.
    """
    assert parse_actions(["codechange", "add", "codechange", "1"]) == [
        AddList(field="codechange", value="1", ref=SeqRef(kind="CodeChange", seq=1)),
    ]
    # A bare seq still parses (back-compat; defaults to the field's ref_kind).
    assert parse_actions(["codechange", "add", "7"]) == [
        AddList(field="codechange", value="7", ref=SeqRef(kind="CodeChange", seq=7)),
    ]


def test_parse_bulk_apply_ref_list_add_accepts_typed_ref() -> None:
    """Bulk-apply must keep a typed `kind seq` ref intact (trax #419).

    The mutation scan tokenized every mutation as exactly `FIELD OP VALUE`, so
    the trailing seq of `codechange add codechange 1` fell through as a stray
    token and raised before `parse_actions` could attach the parsed `Ref`.
    """
    bulk = parse_bulk_apply(
        "Experiment", ["outcome", "is", "ok", "codechange", "add", "codechange", "1"]
    )
    assert bulk is not None
    assert bulk.actions == (
        AddList(field="codechange", value="1", ref=SeqRef(kind="CodeChange", seq=1)),
    )


def test_bulk_apply_codechange_field_not_swallowed_as_kind() -> None:
    """`codechange` is both a kind keyword and a list field; a following mutation
    operator makes it a MUTATION, not a bare kind-widening clause.

    Regression: the clause scanner matched the kind keyword first, so
    ``... codechange add 7`` consumed ``codechange`` as a bare kind and orphaned
    ``add``. Dropping ``WebSearch.results`` left ``codechange`` the only
    kind/field collision, exposing this. A bare kind is never followed by
    ``to``/``add``/``del``, so the next token disambiguates cleanly.
    """
    bulk = parse_bulk_apply(
        "Experiment", ["outcome", "is", "ok", "codechange", "add", "7"]
    )
    assert bulk is not None
    assert bulk.actions == (
        AddList(field="codechange", value="7", ref=SeqRef(kind="CodeChange", seq=7)),
    )
    # A bare kind (no following operator) still widens the listed set.
    query = parse_list_query("Issue", ["belief"])
    assert query is not None
    assert query.kinds == ("Issue", "Belief")


def test_ref_text_seq_ref_returns_seq() -> None:

    assert ref_text(SeqRef(kind="Issue", seq=7)) == "7"


def test_parse_subject_list_kind_without_seq_returns_none() -> None:

    # Kind followed by a non-ref token aborts subject-list parsing.
    assert parse_subject_list(["issue"], default_kind="Issue") is None


def test_parse_subject_list_accepts_uuid() -> None:
    u = "550e8400-e29b-41d4-a716-446655440000"
    subjects = parse_subject_list([u], default_kind="Issue")
    assert subjects is not None
    ref = subjects[0]
    assert ref == UuidRef(uuid=uuid.UUID(u), expected_kind="Issue")


def test_edge_metadata_priority_requires_to() -> None:
    # ``priority`` is a collision word, so it needs the ``edge`` marker to be read
    # as edge metadata after a ref (bare would roll up to the subject).
    with pytest.raises(ClientError, match="edge priority uses to"):
        parse_actions(["blocked_by", "issue", "5", "edge", "priority", "add", "7"])


def test_edge_metadata_note_requires_to() -> None:

    with pytest.raises(ClientError, match="edge note uses to"):
        parse_actions(["blocked_by", "issue", "5", "note", "add", "hi"])


def test_edge_metadata_valence_requires_to() -> None:

    with pytest.raises(ClientError, match="edge valence uses to"):
        parse_actions(["blocked_by", "issue", "5", "valence", "add", "0.5"])


def test_edge_metadata_labels_reject_bad_op() -> None:
    with pytest.raises(ClientError, match="edge label uses"):
        parse_actions(["blocked_by", "issue", "5", "edge", "labels", "noop", "x"])


def test_edge_metadata_labels_to_resolves_csv() -> None:
    actions = parse_actions(["blocked_by", "issue", "5", "edge", "labels", "to", "a,b"])
    edge = actions[0]
    assert isinstance(edge, EdgeAction)
    assert edge.metadata.get("labels") == ["a", "b"]


def test_edge_priority_bad_value_raises_client_error() -> None:
    with pytest.raises(ClientError, match="priority must be an int"):
        parse_actions(["blocked_by", "issue", "5", "edge", "priority", "to", "oops"])


def test_inline_create_rejects_duplicate_scalar_field() -> None:
    with pytest.raises(ClientError, match="scalar field 'title' set more than once"):
        parse_actions(["blocked_by", "issue", "title", "to", "a", "title", "to", "b"])


def test_inline_create_reseeding_list_field_with_to_is_rejected() -> None:
    """Re-seeding a list field with ``to`` after it holds values is rejected.

    ``to`` seeds the byline once; a second ``to`` is the user clobbering their
    own input, so it raises -- and the message names the ``list`` field
    accurately rather than calling it ``scalar`` (F1/F12). Accumulation is the
    ``add`` verb's job (see ``test_inline_create_accumulates_list_field``).
    """
    with pytest.raises(ClientError, match="list field 'labels' set more than once"):
        parse_actions(["blocked_by", "issue", "labels", "to", "a", "labels", "to", "b"])


def test_inline_create_accumulates_list_field() -> None:
    """An inline list field seeds with ``to`` and extends with ``add``.

    The ordered byline ``author to A author add B author add C`` lands as one
    multi-entry tuple, mirroring the edit-path ``add`` verb instead of clobbering
    to the last value.
    """
    actions = parse_actions(
        [
            "produced",
            "paper",
            "title",
            "to",
            "P",
            "author",
            "to",
            "Ada Lovelace",
            "author",
            "add",
            "Alan Turing",
            "author",
            "add",
            "Grace Hopper",
        ]
    )
    inline = cast(InlineCreate, cast(EdgeAction, actions[0]).target)
    authors = next(f for f in inline.fields if f.field == "authors")
    assert authors.value == ("Ada Lovelace", "Alan Turing", "Grace Hopper")


def test_edge_metadata_case_insensitive_field_and_op() -> None:
    actions = parse_actions(
        ["blocked_by", "issue", "5", "EDGE", "PRIORITY", "TO", "high"]
    )
    edge = actions[0]
    assert isinstance(edge, EdgeAction)
    assert edge.metadata.get("priority") == 10


def test_edge_action_del_terminator_case_insensitive() -> None:
    """``trax issue 7 blocks issue 8 DEL`` removes the edge."""
    actions = parse_actions(["blocks", "issue", "8", "DEL"])
    assert len(actions) == 1
    edge = actions[0]
    assert isinstance(edge, EdgeAction)
    assert edge.remove is True


def test_edge_metadata_labels_del_removes_value() -> None:
    # Each collision-word metadata occurrence after a ref carries its own ``edge``
    # marker.
    actions = parse_actions(
        [
            "blocked_by", "issue", "5",
            "edge", "labels", "add", "a",
            "edge", "labels", "del", "a",
        ]
    )  # fmt: skip
    edge = actions[0]
    assert isinstance(edge, EdgeAction)
    assert edge.metadata.get("labels") == []


def test_edge_metadata_labels_del_resolves_csv() -> None:
    actions = parse_actions(
        [
            "blocked_by", "issue", "5",
            "edge", "labels", "add", "a,b,c",
            "edge", "labels", "del", "a,c",
        ]
    )  # fmt: skip
    edge = actions[0]
    assert isinstance(edge, EdgeAction)
    assert edge.metadata.get("labels") == ["b"]


@pytest.mark.parametrize("tokens", [["label", "noop"], ["label", "noop", "x"]])
def test_list_field_unknown_op_rejects_at_op(tokens: list[str]) -> None:
    with pytest.raises(ClientError, match="list field label uses"):
        parse_actions(tokens)


def test_chained_scalar_set_then_row_del() -> None:
    """A trailing ``del`` after two chained scalar sets is a ROW delete (BUG-001).

    The old ``index+3`` lookahead in ``_parse_scalar_action`` peeked past the
    second set's value and misread the terminal ``del`` as a delete of the
    ``description`` field, raising "cannot delete scalar field". With the
    lookahead gone, ``parse_actions``'s outer loop reaches the ``del`` and emits
    a terminal :class:`DeleteRow` after both sets.
    """
    actions = parse_actions(["title", "to", "x", "description", "to", "y", "del"])
    assert actions == [
        SetField(field="title", value="x"),
        SetField(field="description", value="y"),
        DeleteRow(),
    ]


def test_scalar_set_then_row_del_is_row_delete() -> None:
    """``title to x del`` is a set followed by a terminal row delete (BUG-001).

    ``del`` is uniformly the terminal row-delete; the preceding set is a no-op
    before the delete, allowed rather than errored (the old lookahead rejected
    it as a scalar-field delete).
    """
    actions = parse_actions(["title", "to", "x", "del"])
    assert actions == [SetField(field="title", value="x"), DeleteRow()]


def test_token_after_del_still_rejected() -> None:
    """``del`` stays terminal: any action after it raises (invariant)."""
    with pytest.raises(ClientError, match="'del' must be the last token"):
        parse_actions(["title", "to", "x", "del", "status", "to", "y"])


def test_positive_citation_rejects_negative_valence() -> None:
    """A positive-polarity citation (``proves``) rejects a negative valence.

    For-vs-against is the SIGN of valence: a ``proves``/``favors`` edge carries
    a non-negative magnitude; the ``dis*`` spelling is the negative polarity.
    The positive branch must mirror the ``dis*`` guard rather than silently
    storing a negative valence on a ``proves`` edge (BUG-002).
    """
    with pytest.raises(ClientError, match="non-negative valence"):
        parse_actions(["proves", "belief", "5", "valence", "to", "-0.5"])


def test_positive_citation_accepts_non_negative_valence() -> None:
    """``proves ... valence to 0.9`` stores the magnitude unchanged."""
    actions = parse_actions(["proves", "belief", "5", "valence", "to", "0.9"])
    edge = actions[0]
    assert isinstance(edge, EdgeAction)
    assert edge.edge.name == "proves"
    assert edge.metadata.get("valence") == 0.9


def test_required_token_raises_on_missing() -> None:

    with pytest.raises(ClientError, match="boom"):
        required_token([], 0, "boom")


def test_parse_range_invalid_too_many_dots() -> None:

    with pytest.raises(ClientError, match="invalid seq range"):
        parse_list_query("Issue", ["1..2..3"])


def test_parse_range_empty_both_sides_raises() -> None:

    with pytest.raises(ClientError, match="range requires"):
        parse_list_query("Issue", [".."])


def test_parse_range_non_digit_start_raises() -> None:

    with pytest.raises(ClientError, match="invalid seq range start"):
        parse_list_query("Issue", ["foo..5"])


def test_parse_range_non_digit_stop_raises() -> None:

    with pytest.raises(ClientError, match="invalid seq range stop"):
        parse_list_query("Issue", ["5..bar"])


def test_parse_range_csv_unions_disjoint_intervals() -> None:
    """A comma unions disjoint intervals in one selector token."""
    q = parse_list_query("Issue", ["..10,222..225,227,228.."])
    assert q is not None
    assert q.ranges["Issue"] == (
        SeqRange(stop=10),
        SeqRange(start=222, stop=225),
        SeqRange(start=227, stop=227),
        SeqRange(start=228),
    )


def test_parse_range_bare_seq_in_csv_is_single_row_interval() -> None:
    """A bare seq inside a CSV is the degenerate interval ``n..n``."""
    q = parse_list_query("Issue", ["5,8"])
    assert q is not None
    assert q.ranges["Issue"] == (SeqRange(start=5, stop=5), SeqRange(start=8, stop=8))


def test_parse_range_lone_seq_is_a_ref_not_a_range() -> None:
    """A lone seq token has no ``..`` or comma, so it stays a ref, not a range."""
    assert parse_list_query("Issue", ["227"]) is None


def test_parse_range_trailing_comma_raises() -> None:
    """An empty CSV element (trailing comma) is malformed."""
    with pytest.raises(ClientError, match="invalid range element"):
        parse_list_query("Issue", ["5,"])


def test_parse_range_csv_round_trips_through_bulk_apply() -> None:
    """A CSV selector survives the bulk-apply parse/emit/re-parse round-trip."""
    bulk = parse_bulk_apply("Issue", ["1..5,9,12..", "owner", "to", "Josh"])
    assert bulk is not None
    assert bulk.query.ranges["Issue"] == (
        SeqRange(start=1, stop=5),
        SeqRange(start=9, stop=9),
        SeqRange(start=12),
    )


def test_consume_ref_empty_raises() -> None:

    with pytest.raises(ClientError, match="expected reference"):
        consume_ref([], 0)


def test_consume_ref_kind_without_seq_raises() -> None:

    with pytest.raises(ClientError, match="incomplete ref"):
        consume_ref(["issue"], 0)


def test_consume_ref_kind_with_non_digit_raises() -> None:

    with pytest.raises(ClientError, match="expected seq number or uuid"):
        consume_ref(["issue", "abc"], 0)


def test_consume_ref_kind_uuid_two_token() -> None:
    """``kind <uuid>`` accepts the redundant kind and carries it on
    the parsed ref as ``expected_kind``; the client compares it to
    the server-resolved kind to catch typos.
    """
    u = "550e8400-e29b-41d4-a716-446655440000"
    ref, consumed = consume_ref(["issue", u], 0)
    assert isinstance(ref, UuidRef)
    assert str(ref.uuid) == u
    assert ref.expected_kind == "Issue"
    assert consumed == 2


def test_consume_ref_bare_uuid_carries_no_expected_kind() -> None:
    """A bare UUID has no spelled-kind to validate against."""
    u = "550e8400-e29b-41d4-a716-446655440000"
    ref, consumed = consume_ref([u], 0)
    assert isinstance(ref, UuidRef)
    assert ref.expected_kind is None
    assert consumed == 1


def test_consume_edge_target_empty_raises() -> None:

    with pytest.raises(ClientError, match="expected reference"):
        consume_edge_target([], 0)


def test_consume_edge_target_uuid() -> None:
    u = "550e8400-e29b-41d4-a716-446655440000"
    target, consumed = consume_edge_target([u], 0)
    assert isinstance(target, UuidRef)
    assert str(target.uuid) == u
    assert target.expected_kind is None
    assert consumed == 1


def test_inline_create_requires_at_least_one_field() -> None:

    # ``blocked_by issue`` with no field after = inline_create with no fields.
    with pytest.raises(ClientError):
        parse_actions(["blocked_by", "issue", "title"])


def test_inline_create_rejects_del_after() -> None:

    with pytest.raises(ClientError, match="cannot 'del' an inline-create"):
        parse_actions(["blocked_by", "issue", "title", "to", "x", "del"])


# Folded in from former crasher_test.py (relation-alias parser bug fixed).


def test_bare_seq_after_relation_word_is_a_relation_index_not_edge_create() -> None:
    """A bare number after a relation/edge word selects a relation ROW, not a ref.

    Grammar invariant (pinned so it can't silently change): ``narrows 2`` reads
    the 2nd ``narrows`` relation row, because a bare seq after a relation word is
    an index. To CREATE an edge to seq 2 the user must spell a TYPED ref
    (``narrows issue 2``) or a UUID. ``RELATION_ALIASES`` is matched before
    ``EDGE_ALIASES`` for exactly this reason.
    """
    [action] = parse_actions(["narrows", "2"])
    assert isinstance(action, RelationAction)
    assert action.relation == ("narrows", False)
    assert action.index == "2"

    # A typed ref after the same word is an edge create, not a relation index.
    [edge_action] = parse_actions(["narrows", "issue", "2"])
    assert isinstance(edge_action, EdgeAction)


def test_relation_only_alias_raises_client_error() -> None:
    """Relation-only aliases followed by a kind+seq target raise ``ClientError``."""
    client = FakeClient()
    with pytest.raises(ClientError):
        cli.parse_and_run(
            ["issue", "1", "narrower", "issue", "5"],
            client_factory=lambda: cast(Any, client),
        )


@pytest.mark.parametrize(
    "alias",
    [
        "supports",
        "refutes",
        "evidence_for",
        "evidence_against",
        "artifact_for",
        "artifact_against",
        "is_blocked_by",
        "evidence-for",
        "evidence-against",
        "artifact-for",
        "artifact-against",
        "refuted-by-experiment",
        "produced-artifact",
        "blocked-by",
        "broader-issue",
        "narrower-issue",
    ],
)
def test_removed_edge_aliases_reject(alias: str) -> None:
    client = FakeClient()
    with pytest.raises(ClientError):
        cli.parse_and_run(
            ["issue", "1", alias, "issue", "5"],
            client_factory=lambda: cast(Any, client),
        )


# -- Issue#425 item 6: deep-cursor nesting + begin/end grouping ----------------


def test_inline_create_carries_nested_edge_deep() -> None:
    """A bare inline-create target may carry its own edge (deep descent).

    `produced websearch <f> produced paper <f>` -- the second `produced` binds to
    the just-created websearch (the cursor descended), so the websearch's
    InlineCreate carries a nested `produced paper` EdgeAction.
    """
    actions = parse_actions(
        [
            "produced",
            "websearch",
            "query",
            "to",
            "q",
            "produced",
            "paper",
            "title",
            "to",
            "p",
        ]
    )
    assert len(actions) == 1, "the whole chain is ONE tail (deep), not two siblings"
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    ws = outer.target
    assert isinstance(ws, InlineCreate)
    assert ws.kind == "WebSearch"
    assert len(ws.edges) == 1, "the websearch carries the nested produced-paper edge"
    inner = ws.edges[0]
    assert isinstance(inner, EdgeAction)
    assert isinstance(inner.target, InlineCreate)
    assert inner.target.kind == "Paper"


def test_begin_end_group_pops_cursor_for_siblings() -> None:
    """`begin ... end` scopes a subtree; `end` pops back to the parent.

    `produced websearch <f> produced begin paper <f> end produced begin paper <f> end`
    -- both papers are SIBLINGS under the websearch (each group pops back to it),
    so the websearch carries TWO nested produced-paper edges.
    """
    actions = parse_actions(
        [
            "produced",
            "websearch",
            "query",
            "to",
            "q",
            "produced",
            "begin",
            "paper",
            "title",
            "to",
            "p1",
            "end",
            "produced",
            "begin",
            "paper",
            "title",
            "to",
            "p2",
            "end",
        ]
    )
    assert len(actions) == 1
    ws = actions[0]
    assert isinstance(ws, EdgeAction)
    assert isinstance(ws.target, InlineCreate)
    assert len(ws.target.edges) == 2, "both papers are siblings under the websearch"
    titles = [
        e.target.fields[0].value
        for e in ws.target.edges
        if isinstance(e.target, InlineCreate)
    ]
    assert titles == ["p1", "p2"]


def test_inline_create_carries_own_cost_deep() -> None:
    """A deep inline-create consumes its OWN ``agent-cost`` delta (Issue#425 item 6).

    Regression: the canonical research examples write
    ``produced websearch <fields> agent-cost add N produced paper <fields>`` --
    the cost binds to the websearch, not the leading subject. Before the fix the
    cost loop did not exist inside the inline-create, so ``agent-cost`` either
    rebound to the root or wedged the parse at the following token.
    """
    actions = parse_actions(
        [
            "produced", "websearch", "query", "to", "q",
            "agent-cost", "add", "0.89",
            "produced", "paper", "title", "to", "p",
        ]
    )  # fmt: skip
    assert len(actions) == 1, "one tail: the cost rides the websearch, not the root"
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    ws = outer.target
    assert isinstance(ws, InlineCreate)
    assert ws.costs == (AddCost(field="agent-cost", value=0.89),)
    assert len(ws.edges) == 1, "the websearch still carries its nested produced-paper"


def test_node_note_binds_to_its_producer_edge_without_grouping() -> None:
    """A note beside a node's fields annotates the edge that produced it.

    The verdict ``note`` is the deepest edge so far: it binds to the
    belief->websearch ``produces`` edge, written right after the websearch's
    fields/cost -- no ``begin ... end`` needed. ``begin ... end`` is reserved for
    WIDTH (siblings), never for placing a note. Mirrors the empty / single-paper
    searches in ``trax_research_example_1.sh`` and the spine in ``_2.sh``.
    """
    belief = "00000000-0000-4000-8000-000000000001"
    actions = parse_actions(
        [
            "produced", "websearch", "query", "to", "q",
            "agent-cost", "add", "1.33",
            "note", "to", "verdict",
            "produced", "paper", "title", "to", "p",
            "favors", "belief", belief, "note", "to", "w",
        ]
    )  # fmt: skip
    assert len(actions) == 1, "one belief->websearch producer edge (deep spine)"
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    assert outer.metadata.get("note") == "verdict", "verdict note on the producer edge"
    ws = outer.target
    assert isinstance(ws, InlineCreate)
    assert ws.costs == (AddCost(field="agent-cost", value=1.33),)
    assert len(ws.edges) == 1, "the single paper chains under the websearch"
    paper_edge = ws.edges[0]
    assert isinstance(paper_edge.target, InlineCreate)
    favors = paper_edge.target.edges[0]
    assert favors.edge.name == "favors"
    assert favors.metadata.get("note") == "w", "the paper's own edge note stays put"


def test_two_papers_need_begin_end_only_for_width() -> None:
    """Two findings under one search are siblings via ``begin ... end`` -- only that.

    Mirrors the two-paper searches in ``trax_research_example_1.sh``: the verdict
    note sits beside the websearch (binding the belief->websearch edge), and each
    paper is wrapped ``begin ... end`` purely so the second is a SIBLING of the
    first, not chained under it. Nothing else is wrapped.
    """
    belief = "00000000-0000-4000-8000-000000000001"
    actions = parse_actions(
        [
            "produced", "websearch", "query", "to", "q",
            "agent-cost", "add", "0.89",
            "note", "to", "verdict",
            "produced", "begin", "paper", "title", "to", "p1",
            "disfavors", "belief", belief, "note", "to", "n1", "end",
            "produced", "begin", "paper", "title", "to", "p2",
            "disfavors", "belief", belief, "note", "to", "n2", "end",
        ]
    )  # fmt: skip
    assert len(actions) == 1
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    assert outer.metadata.get("note") == "verdict"
    ws = outer.target
    assert isinstance(ws, InlineCreate)
    assert len(ws.edges) == 2, "both papers are SIBLINGS under the websearch"
    for child, expected in zip(ws.edges, ("n1", "n2"), strict=True):
        assert isinstance(child.target, InlineCreate)
        disfavor = child.target.edges[0]
        # ``disfavors`` is the negated-valence spelling of the ``favors`` kind.
        assert disfavor.edge.name == "favors"
        assert disfavor.metadata.get("note") == expected


def test_inline_create_fields_metadata_cost_interleave_in_any_order() -> None:
    """Fields, note/metadata, and cost interleave freely before the first edge.

    Their token heads are disjoint, so order carries no meaning: a field after a
    ``note``, or a ``note`` before a field, both land on the SAME node. This is
    the property the canonical examples rely on -- the author should not have to
    remember a fields-then-note ordering. Two equivalent orderings must produce
    identical nodes.
    """
    belief = "00000000-0000-4000-8000-000000000001"
    fields_first = parse_actions(
        [
            "produced", "websearch", "query", "to", "q",
            "status", "to", "complete", "agent-cost", "add", "0.5",
            "note", "to", "n", "favors", "belief", belief,
        ]
    )  # fmt: skip
    # A create must LEAD with a field (the dispatch recognizes a create by a
    # field right after the kind); after that first field, note/cost/fields
    # interleave in any order.
    meta_first = parse_actions(
        [
            "produced", "websearch", "query", "to", "q",
            "note", "to", "n", "agent-cost", "add", "0.5",
            "status", "to", "complete", "favors", "belief", belief,
        ]
    )  # fmt: skip
    for actions in (fields_first, meta_first):
        outer = actions[0]
        assert isinstance(outer, EdgeAction)
        assert outer.metadata.get("note") == "n", "note lands on the produces edge"
        ws = outer.target
        assert isinstance(ws, InlineCreate)
        assert ws.costs == (AddCost(field="agent-cost", value=0.5),)
        assert {f.field: f.value for f in ws.fields} == {
            "query": "q",
            "status": "complete",
        }, "both orderings yield the same websearch fields"
        assert len(ws.edges) == 1, "the favors edge attached to the websearch"


def test_inline_create_field_after_edge_is_rejected() -> None:
    """A vertex field AFTER the node's first OUTGOING edge is a loud error.

    The one real boundary: once an edge descends past this node, the node is
    closed, so a trailing field has no home. The pre-fix parser silently rebound
    it to the caller's anchor -- dropping it and corrupting the command. Now it
    raises. (A field after a NOTE is fine -- only an edge closes the node.)
    """
    belief = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ClientError, match="appears after an edge"):
        parse_actions(
            [
                "produced", "paper", "title", "to", "p",
                "favors", "belief", belief, "status", "to", "complete",
            ]
        )  # fmt: skip


def test_inline_create_cost_then_field_interleaves() -> None:
    """``agent-cost`` does NOT close the field run -- a field may follow it.

    A cost delta interleaves freely with fields (a self-terminating one-value
    op), so ``... agent-cost add N status to complete`` keeps ``status`` a create
    field of THIS node. This is the exact ordering the canonical examples rely
    on; regressing it silently dropped ``status``.
    """
    actions = parse_actions(
        [
            "produced", "websearch", "query", "to", "q",
            "agent-cost", "add", "0.5",
            "status", "to", "complete",
        ]
    )  # fmt: skip
    assert len(actions) == 1
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    ws = outer.target
    assert isinstance(ws, InlineCreate)
    assert ws.costs == (AddCost(field="agent-cost", value=0.5),)
    assert any(f.field == "status" and f.value == "complete" for f in ws.fields), (
        "status after agent-cost is a field of the websearch, not dropped"
    )


# -- The `edge` marker: disambiguate vertex-vs-edge for collision words --------
# `priority`/`label`/`labels` name BOTH a row field and an edge annotation. The
# `edge` marker forces the edge reading; bare always means vertex. `note`/
# `valence` are edge-only, so they need no marker (but accept one).


def test_edge_marker_sets_collision_word_on_the_edge() -> None:
    """`edge priority` after a ref annotates the EDGE, not the referenced row."""
    actions = parse_actions(["narrows", "issue", "3", "edge", "priority", "to", "high"])
    assert len(actions) == 1
    act = actions[0]
    assert isinstance(act, EdgeAction)
    assert act.metadata.get("priority") == 10  # high -> 10


def test_bare_collision_word_after_ref_rolls_up_never_silently_edge() -> None:
    """Bare `priority`/`label` after a ref is NOT the edge -- maximal munch.

    Pre-fix, ``issue 7 narrows issue 3 priority to high`` silently set the EDGE
    priority while reading like it set issue 3's -- the footgun. Now a bare
    collision word is never consumed as edge metadata: the completed ref can take
    no more, so the token rolls UP to the next construct that can claim it (the
    leading subject, as an ordinary field). The edge meaning requires the
    explicit ``edge`` marker. So bare never silently lands on the edge.
    """
    actions = parse_actions(["narrows", "issue", "3", "priority", "to", "high"])
    edge_acts = [a for a in actions if isinstance(a, EdgeAction)]
    assert len(edge_acts) == 1
    assert edge_acts[0].metadata.get("priority") is None, (
        "bare priority must NOT silently become edge metadata"
    )
    assert any(isinstance(a, SetField) and a.field == "priority" for a in actions), (
        "it rolls up to the leading subject as a field"
    )


def test_unambiguous_meta_needs_no_marker_after_ref() -> None:
    """`note`/`valence` are edge-only, so bare works after a ref (no footgun)."""
    belief = "00000000-0000-4000-8000-000000000001"
    actions = parse_actions(
        ["favors", "belief", belief, "note", "to", "adjacent", "valence", "to", "0.5"]
    )  # fmt: skip
    act = actions[0]
    assert isinstance(act, EdgeAction)
    assert act.metadata.get("note") == "adjacent"
    assert act.metadata.get("valence") == 0.5


def test_edge_marker_also_accepted_on_unambiguous_meta() -> None:
    """`edge note` is accepted too -- writing `edge` is never wrong."""
    belief = "00000000-0000-4000-8000-000000000001"
    actions = parse_actions(
        ["favors", "belief", belief, "edge", "note", "to", "adjacent"]
    )  # fmt: skip
    act = actions[0]
    assert isinstance(act, EdgeAction)
    assert act.metadata.get("note") == "adjacent"


def test_collision_word_in_create_body_is_a_vertex_field() -> None:
    """Bare `priority` in a create BODY is the new row's field (vertex)."""
    actions = parse_actions(
        ["produced", "issue", "title", "to", "X", "priority", "to", "high"]
    )  # fmt: skip
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    node = outer.target
    assert isinstance(node, InlineCreate)
    assert {f.field: f.value for f in node.fields}.get("priority") == 10
    assert not node.edges
    assert node.inbound_meta == {}, "bare priority is the row's, not the produces edge"


def test_edge_marker_in_create_body_sets_the_producer_edge() -> None:
    """`edge priority` in a create body annotates the PRODUCER edge, not the row."""
    actions = parse_actions(
        [
            "produced", "issue", "title", "to", "X",
            "edge", "priority", "to", "high",
        ]
    )  # fmt: skip
    outer = actions[0]
    assert isinstance(outer, EdgeAction)
    node = outer.target
    assert isinstance(node, InlineCreate)
    assert {f.field: f.value for f in node.fields}.get("priority") is None, (
        "priority went to the edge, not the row"
    )
    assert outer.metadata.get("priority") == 10


# -- B: edge metadata BEFORE the target (unambiguous, no marker needed) --------


def test_edge_metadata_before_target_is_unambiguous_edge() -> None:
    """Metadata between the edge keyword and its target annotates the EDGE.

    There is nothing else it could attach to yet (the target has not appeared),
    so even the collision words ``priority``/``label`` are unambiguous here and
    need no ``edge`` marker. ``narrows priority to high issue 3`` == ``narrows
    issue 3 edge priority to high``.
    """
    pre = parse_actions(["narrows", "priority", "to", "high", "issue", "3"])
    post = parse_actions(["narrows", "issue", "3", "edge", "priority", "to", "high"])
    for actions in (pre, post):
        assert len(actions) == 1
        act = actions[0]
        assert isinstance(act, EdgeAction)
        assert act.metadata.get("priority") == 10
        assert isinstance(act.target, UuidRef | SeqRef)


def test_edge_metadata_before_and_after_target_merge() -> None:
    """Pre-target and post-target metadata on the same edge merge."""
    actions = parse_actions(
        ["favors", "note", "to", "n", "belief", "3", "valence", "to", "0.5"]
    )  # fmt: skip
    act = actions[0]
    assert isinstance(act, EdgeAction)
    assert act.metadata.get("note") == "n"
    assert act.metadata.get("valence") == 0.5


def test_edge_valence_non_number_is_client_error_not_value_error() -> None:
    """A non-numeric edge valence raises ClientError, never a raw ValueError.

    Mirrors ``priority``'s int check: a malformed value must surface as a clean
    client message, not leak a Python traceback to the CLI. Covered at every
    parse position valence can appear (post-target, pre-target, inline body).
    """
    belief = "00000000-0000-4000-8000-000000000001"
    positions = [
        ["favors", "belief", belief, "valence", "to", "abc"],  # post-target
        ["favors", "valence", "to", "abc", "belief", belief],  # pre-target
        ["produced", "websearch", "query", "to", "q", "valence", "to", "abc"],  # body
    ]
    for toks in positions:
        with pytest.raises(ClientError, match="valence must be a number"):
            parse_actions(toks)


def test_edge_metadata_pre_and_post_merge_post_wins_same_key() -> None:
    """Pre- and post-target edge metadata merge; post-target wins a same-key tie.

    Distinct keys union; a key set both before and after the target (malformed,
    but must be deterministic) resolves to the POST-target value -- last write
    wins (inbound -> pre -> post). Pins the merge precedence the docstring
    describes so a future refactor cannot silently flip it.
    """
    belief = "00000000-0000-4000-8000-000000000001"
    # priority high (=10) pre-target, critical (=0) post-target via the marker.
    act = parse_actions(
        ["narrows", "priority", "to", "high",
         "issue", "3", "edge", "priority", "to", "critical"]
    )[0]  # fmt: skip
    assert isinstance(act, EdgeAction)
    assert act.metadata.get("priority") == 0, "post-target priority wins the tie"

    # Distinct keys union across the boundary.
    act2 = parse_actions(
        ["favors", "note", "to", "n", "belief", belief, "valence", "to", "0.5"]
    )[0]
    assert isinstance(act2, EdgeAction)
    assert act2.metadata.get("note") == "n"
    assert act2.metadata.get("valence") == 0.5


def test_cost_non_number_is_client_error_not_value_error() -> None:
    """A non-numeric cost delta raises ClientError, never a raw ValueError.

    Both the top-level and inline-create cost paths route through
    ``_parse_cost_action``; a malformed amount must surface as a clean client
    message, mirroring the edge-valence guard.
    """
    for toks in (
        ["agent-cost", "add", "abc"],  # top-level
        ["produced", "issue", "title", "to", "X", "agent-cost", "add", "xyz"],  # inline
        ["resource-cost", "add", "nope"],
    ):
        with pytest.raises(ClientError, match="must be a number"):
            parse_actions(toks)


def test_confidence_non_number_is_clean_client_error() -> None:
    """A non-numeric confidence raises a CLEAN ClientError message.

    ``field_value`` already mapped the raw ``float()`` ValueError to a
    ClientError, but the message was the opaque "could not convert string to
    float". The coerce now raises a bespoke "confidence must be a number",
    matching ``priority``'s "must be an int".
    """
    with pytest.raises(ClientError, match="confidence must be a number"):
        parse_actions(
            ["produced", "belief", "title", "to", "X", "confidence", "to", "abc"]
        )


def test_inline_create_leading_with_non_field_gives_clear_message() -> None:
    """A create body that leads with metadata/cost/edge gets a CLEAR error.

    The dispatch recognizes an inline create by a FIELD right after the kind; a
    create that mistakenly leads with ``note``/``edge``/``agent-cost``/an edge
    keyword used to fall through to ``consume_ref`` and report the opaque
    "expected seq number or uuid". It now says the create must lead with a field.
    """
    for lead in (
        ["produced", "websearch", "note", "to", "N", "query", "to", "q"],
        ["produced", "issue", "edge", "priority", "to", "high", "title", "to", "X"],
        ["produced", "issue", "agent-cost", "add", "1", "title", "to", "X"],
        ["produced", "issue", "favors", "belief", "3"],
    ):
        with pytest.raises(ClientError, match="must lead with a field"):
            parse_actions(lead)


def test_begin_with_missing_or_bad_kind_is_client_error() -> None:
    """``begin`` at EOF or before a non-kind is a clean ClientError, not a leak.

    The inline-create entry (reached via ``begin <kind> ...`` and the dispatch)
    must bounds-check and validate the kind: a missing token raised IndexError
    and ``begin 3`` raised ``parse_kind``'s raw ValueError -- both leaked a
    Python traceback to the CLI. Now both surface as ClientError.
    """
    cases = [
        ["disfavors", "begin"],  # begin at EOF -> IndexError
        ["narrows", "begin", "3", "title", "to", "X"],  # non-kind after begin
        ["produced", "begin", "notakind", "title", "to", "X"],
    ]
    for toks in cases:
        with pytest.raises(ClientError):
            parse_actions(toks)


# -- Property: the parser never leaks a non-ClientError -------------------------
# Every input either parses or raises ClientError (the parser's contract); a raw
# IndexError/ValueError/RecursionError would surface a Python traceback at the
# CLI. Hypothesis generates plausible trax-ish token streams (drawn from the real
# grammar tables so the alphabet cannot drift) and shrinks any leak to a minimal
# reproducer. Found the `begin`-at-EOF IndexError and `begin <non-kind>`
# ValueError; replays them from the persistent failure DB on every run.

_ALPHABET: list[str] = sorted(
    {
        *KIND_LOWER,
        *EDITABLE_FIELDS,
        *LIST_FIELDS,
        *COST_FIELDS,
        *EDGE_ALIASES,
        "to", "add", "del", "edge", "begin", "end",
        # values of each shape the coercers care about
        "0.5", "-0.5", "abc", "high", "0", "3",
        "00000000-0000-4000-8000-000000000001",
    }
)  # fmt: skip


@settings(max_examples=400, deadline=None)
@given(st.lists(st.sampled_from(_ALPHABET), min_size=1, max_size=10))
def test_parser_never_leaks_non_client_error(tokens: list[str]) -> None:
    """``parse_actions`` raises only ClientError (or succeeds) -- never a leak.

    ``suppress(ClientError)`` is the assertion: a ClientError is the sole
    sanctioned failure (a user-facing message); any other exception propagates
    and fails the test.
    """
    with contextlib.suppress(ClientError):
        parse_actions(tokens)


# -- More properties: other entry points never leak ----------------------------
# Same contract as parse_actions: a malformed input is a ClientError, never a
# raw exception. These entry points were previously unfuzzed.

_A_KIND: Inquiry.InquiryKind = (
    "Issue"  # a fixed valid kind for entry points that need one
)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(_ALPHABET), max_size=8))
def test_parse_list_query_never_leaks(tokens: list[str]) -> None:
    with contextlib.suppress(ClientError):
        parse_list_query(_A_KIND, tokens)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(_ALPHABET), max_size=8))
def test_parse_bulk_apply_never_leaks(tokens: list[str]) -> None:
    with contextlib.suppress(ClientError):
        parse_bulk_apply(_A_KIND, tokens)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(_ALPHABET), max_size=8))
def test_parse_subject_list_never_leaks(tokens: list[str]) -> None:
    with contextlib.suppress(ClientError):
        parse_subject_list(tokens, default_kind=_A_KIND)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(_ALPHABET), min_size=1, max_size=8))
def test_consume_ref_never_leaks(tokens: list[str]) -> None:
    with contextlib.suppress(ClientError):
        consume_ref(tokens, 0)


# -- Semantic laws as properties -----------------------------------------------

_COLLISION = ["priority", "label", "labels"]
_EDGE_KW = ["narrows", "requires", "blocks", "favors", "produced"]
_UUID = "00000000-0000-4000-8000-000000000001"


def _meta_value(word: str) -> tuple[str, str]:
    """A (op, value) pair valid for an edge-metadata ``word``."""
    if word == "priority":
        return "to", "high"
    return "add", "x"  # label / labels


@settings(max_examples=300, deadline=None)
@given(st.sampled_from(_EDGE_KW), st.sampled_from(_COLLISION))
def test_pre_target_equals_edge_marked_post_target(edge_kw: str, word: str) -> None:
    """Pre-target metadata == ``edge``-marked post-target, for collision words.

    The core disambiguation law: writing the metadata before the ref (bare,
    unambiguous) and after the ref (``edge``-marked) annotate the SAME edge with
    the SAME value. Proves the equivalence over all edge keywords x collision
    words, not one example.
    """
    op, val = _meta_value(word)
    pre = parse_actions([edge_kw, word, op, val, "belief", _UUID])
    post = parse_actions([edge_kw, "belief", _UUID, "edge", word, op, val])
    assert isinstance(pre[0], EdgeAction)
    assert isinstance(post[0], EdgeAction)
    key = "labels" if word in ("label", "labels") else word
    assert pre[0].metadata.get(key) == post[0].metadata.get(key)


@settings(max_examples=300, deadline=None)
@given(st.sampled_from(_EDGE_KW), st.sampled_from(_COLLISION))
def test_bare_collision_after_ref_never_lands_on_edge(edge_kw: str, word: str) -> None:
    """A bare collision word after a ref is NEVER edge metadata (maximal munch).

    It rolls up to the leading subject as a field; the edge carries no such key.
    The footgun guarantee, generalized over every edge keyword x collision word.
    """
    op, val = _meta_value(word)
    # parse_actions is the post-subject TAIL parser, so the edge keyword leads;
    # the leading subject is supplied by the outer layer. A bare collision word
    # after the ref must roll up (a SetField on that implicit subject), never the
    # edge.
    actions = parse_actions([edge_kw, "belief", _UUID, word, op, val])
    edge_acts = [a for a in actions if isinstance(a, EdgeAction)]
    assert edge_acts, "the edge itself still parses"
    key = "labels" if word in ("label", "labels") else word
    assert edge_acts[0].metadata.get(key) is None, "bare collision never on the edge"
    # The roll-up is a row-field action: a scalar (priority) lands as SetField; a
    # list field (label/labels, which use add/del) lands as AddList. Either way it
    # is on the subject, never the edge.
    assert any(isinstance(a, SetField | AddList) for a in actions), (
        "it rolled up to a row field action, not the edge"
    )


# -- parse_metric_action -------------------------------------------------------
# Covers every Write / Read / Cross-experiment example in metric-grammar.md plus
# the structural error cases. The parser is purely structural: `to`'s value stays
# a raw string (finiteness is a later layer's job).


class TestParseMetricAction:
    """The ``metric`` tail parser: masks + one operation over the grid."""

    def test_empty_is_whole_grid_read(self) -> None:
        assert parse_metric_action([]) == MetricAction(masks=())

    def test_bareword_key_shorthand(self) -> None:
        # `at loss` == `at key is loss` (spec Read: loss's whole series).
        assert parse_metric_action(["at", "loss"]) == MetricAction(
            masks=(MetricMask(field="key", op="is", value="loss"),)
        )

    def test_explicit_key_is(self) -> None:
        assert parse_metric_action(["at", "key", "is", "loss"]) == MetricAction(
            masks=(MetricMask(field="key", op="is", value="loss"),)
        )

    def test_step_is(self) -> None:
        # spec Read: every key at step 3.
        assert parse_metric_action(["at", "step", "is", "3"]) == MetricAction(
            masks=(MetricMask(field="step", op="is", value="3"),)
        )

    def test_value_gt(self) -> None:
        # spec Read: cells with value > 0.9.
        assert parse_metric_action(["at", "value", "gt", "0.9"]) == MetricAction(
            masks=(MetricMask(field="value", op="gt", value="0.9"),)
        )

    def test_two_masks_and_together_read(self) -> None:
        # spec Read: loss cells, step > 3.
        assert parse_metric_action(
            ["at", "key", "is", "loss", "at", "step", "gt", "3"]
        ) == MetricAction(
            masks=(
                MetricMask(field="key", op="is", value="loss"),
                MetricMask(field="step", op="gt", value="3"),
            )
        )

    def test_single_cell_write(self) -> None:
        # spec Write: one cell (key + step pinned, then `to`).
        assert parse_metric_action(
            ["at", "key", "is", "loss", "at", "step", "is", "3", "to", "0.5"]
        ) == MetricAction(
            masks=(
                MetricMask(field="key", op="is", value="loss"),
                MetricMask(field="step", op="is", value="3"),
            ),
            write="0.5",
        )

    def test_write_with_bareword_key_and_step(self) -> None:
        # `at step is 4 at loss to 0.5` -> step mask + bareword-key mask, write.
        assert parse_metric_action(
            ["at", "step", "is", "4", "at", "loss", "to", "0.5"]
        ) == MetricAction(
            masks=(
                MetricMask(field="step", op="is", value="4"),
                MetricMask(field="key", op="is", value="loss"),
            ),
            write="0.5",
        )

    def test_bulk_write_step_gt(self) -> None:
        # spec Write bulk: set every loss cell with step > 3 to 0.5.
        assert parse_metric_action(
            ["at", "key", "is", "loss", "at", "step", "gt", "3", "to", "0.5"]
        ) == MetricAction(
            masks=(
                MetricMask(field="key", op="is", value="loss"),
                MetricMask(field="step", op="gt", value="3"),
            ),
            write="0.5",
        )

    def test_read_sort_desc_limit(self) -> None:
        # spec Read: loss's 5 largest.
        assert parse_metric_action(
            ["at", "key", "is", "loss", "sort", "desc", "limit", "5"]
        ) == MetricAction(
            masks=(MetricMask(field="key", op="is", value="loss"),),
            sort="desc",
            limit=5,
        )

    def test_read_bareword_sort_limit(self) -> None:
        # `at loss sort desc limit 5` -- bareword key plus read options.
        assert parse_metric_action(
            ["at", "loss", "sort", "desc", "limit", "5"]
        ) == MetricAction(
            masks=(MetricMask(field="key", op="is", value="loss"),),
            sort="desc",
            limit=5,
        )

    def test_sort_asc(self) -> None:
        assert parse_metric_action(["sort", "asc"]) == MetricAction(
            masks=(), sort="asc"
        )

    def test_step_max_reduction(self) -> None:
        # spec Cross-experiment: final per experiment; max takes NO value.
        assert parse_metric_action(["at", "step", "max"]) == MetricAction(
            masks=(MetricMask(field="step", op="max", value=""),)
        )

    def test_step_min_reduction(self) -> None:
        assert parse_metric_action(["at", "step", "min"]) == MetricAction(
            masks=(MetricMask(field="step", op="min", value=""),)
        )

    def test_bareword_key_then_step_max(self) -> None:
        # `at loss at step max` -- final loss per experiment (cross-experiment).
        assert parse_metric_action(["at", "loss", "at", "step", "max"]) == MetricAction(
            masks=(
                MetricMask(field="key", op="is", value="loss"),
                MetricMask(field="step", op="max", value=""),
            )
        )

    def test_cross_experiment_loss_at_step(self) -> None:
        # spec Cross-experiment: loss@100 across all experiments.
        assert parse_metric_action(
            ["at", "loss", "at", "step", "is", "100"]
        ) == MetricAction(
            masks=(
                MetricMask(field="key", op="is", value="loss"),
                MetricMask(field="step", op="is", value="100"),
            )
        )

    def test_cross_experiment_ranked(self) -> None:
        # spec Cross-experiment: top 5 experiments by loss@100.
        assert parse_metric_action(
            ["at", "loss", "at", "step", "is", "100", "sort", "desc", "limit", "5"]
        ) == MetricAction(
            masks=(
                MetricMask(field="key", op="is", value="loss"),
                MetricMask(field="step", op="is", value="100"),
            ),
            sort="desc",
            limit=5,
        )

    def test_ne_op(self) -> None:
        assert parse_metric_action(["at", "key", "ne", "acc"]) == MetricAction(
            masks=(MetricMask(field="key", op="ne", value="acc"),)
        )

    def test_write_value_is_kept_raw_string(self) -> None:
        # Structural only: NaN is not float-validated here (a later layer does).
        action = parse_metric_action(
            ["at", "step", "is", "3", "at", "loss", "to", "NaN"]
        )
        assert action.write == "NaN"

    # -- errors ---------------------------------------------------------------

    def test_at_at_end_errors(self) -> None:
        with pytest.raises(ClientError, match="at"):
            parse_metric_action(["at"])

    def test_to_without_value_errors(self) -> None:
        with pytest.raises(ClientError, match="to"):
            parse_metric_action(["at", "loss", "to"])

    def test_bad_op_after_field_errors(self) -> None:
        with pytest.raises(ClientError, match="foo"):
            parse_metric_action(["at", "step", "foo", "3"])

    @pytest.mark.parametrize("op", ["re", "nre", "isnull", "notnull"])
    def test_filter_only_op_rejected(self, op: str) -> None:
        # A metric grid is neither text-regex-matchable nor nullable, so the
        # regex / presence ops from the inquiry-filter set are not metric ops.
        # The parser rejects them cleanly here rather than letting them slip to
        # the server as a 409 (they used to pass, keyed off the broad
        # ``FILTER_OPS`` instead of the narrow metric op set).
        with pytest.raises(ClientError, match="operator"):
            parse_metric_action(["at", "value", op, "0.9"])

    @pytest.mark.parametrize("op", ["isnull", "notnull"])
    def test_presence_op_does_not_swallow_next_clause(self, op: str) -> None:
        # A presence op is valueless. It must be rejected outright, never
        # consume the following clause's token as a spurious operand -- the old
        # bug parsed ``at value isnull at loss`` as one clause with value="at".
        with pytest.raises(ClientError, match="operator"):
            parse_metric_action(["at", "value", op, "at", "loss"])

    def test_step_max_needs_value_never(self) -> None:
        # A bareword after a field that is NOT a known op is a bad op, not a value.
        with pytest.raises(ClientError):
            parse_metric_action(["at", "key", "bogus", "loss"])

    def test_max_on_key_errors(self) -> None:
        with pytest.raises(ClientError, match="step"):
            parse_metric_action(["at", "key", "max"])

    def test_max_on_value_errors(self) -> None:
        with pytest.raises(ClientError, match="step"):
            parse_metric_action(["at", "value", "max"])

    def test_min_on_key_errors(self) -> None:
        with pytest.raises(ClientError, match="step"):
            parse_metric_action(["at", "key", "min"])

    def test_sort_with_write_errors(self) -> None:
        with pytest.raises(ClientError, match="reads"):
            parse_metric_action(["at", "loss", "to", "0.5", "sort", "desc"])

    def test_limit_with_write_errors(self) -> None:
        with pytest.raises(ClientError, match="reads"):
            parse_metric_action(["at", "loss", "to", "0.5", "limit", "5"])

    def test_write_before_sort_order_independent_errors(self) -> None:
        # sort seen first, then `to`: still rejected (write has no ordering).
        with pytest.raises(ClientError, match="reads"):
            parse_metric_action(["at", "loss", "sort", "desc", "to", "0.5"])

    def test_limit_not_positive_int_errors(self) -> None:
        with pytest.raises(ClientError, match="limit"):
            parse_metric_action(["at", "loss", "limit", "-1"])

    def test_limit_zero_errors(self) -> None:
        with pytest.raises(ClientError, match="limit"):
            parse_metric_action(["at", "loss", "limit", "0"])

    def test_limit_non_int_errors(self) -> None:
        with pytest.raises(ClientError, match="limit"):
            parse_metric_action(["at", "loss", "limit", "x"])

    def test_limit_without_value_errors(self) -> None:
        with pytest.raises(ClientError, match="limit"):
            parse_metric_action(["at", "loss", "limit"])

    def test_sort_bad_direction_errors(self) -> None:
        with pytest.raises(ClientError, match="sort"):
            parse_metric_action(["at", "loss", "sort", "sideways"])

    def test_sort_without_direction_errors(self) -> None:
        with pytest.raises(ClientError, match="sort"):
            parse_metric_action(["at", "loss", "sort"])

    def test_unknown_leading_token_errors(self) -> None:
        with pytest.raises(ClientError):
            parse_metric_action(["bogus"])
