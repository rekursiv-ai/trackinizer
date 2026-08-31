"""Pure functions that turn token sequences into parsed grammar actions."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast, get_args

import uuid

from trackinizer.client.errors import ClientError
from trackinizer.lib.custom_json import FloatCodec
from trackinizer.trax.grammar import (
    AGAINST_RELATION_SPELLINGS,
    COST_FIELDS,
    EDGE_ALIASES,
    EDITABLE_FIELDS,
    FIELDS_BY_NAME,
    FILTER_FIELDS_CLI,
    KIND_LOWER,
    LIST_FIELDS,
    RELATION_ALIASES,
    UUID_RE,
    Action,
    AddCost,
    AddList,
    BulkApply,
    DeleteRow,
    Edge,
    EdgeAction,
    EdgeTarget,
    InlineCreate,
    ListQuery,
    MetricAction,
    MetricMask,
    ReadField,
    RelationAction,
    RemoveList,
    SetField,
    field_value,
    list_payload_field,
    parse_kind,
)
from trackinizer.trax.render import resolve_labels
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.filters import (
    FILTER_OPS,
    VALUELESS_FILTER_OPS,
    Filter,
    FilterOp,
    canonical_filter_field,
)
from trackinizer.wire.refs import Ref, SeqRef, UuidRef
from trackinizer.wire.seq_ranges import (
    SeqRange,
    format_interval,
    parse_interval,
)


if TYPE_CHECKING:
    from trackinizer.wire import wire_metrics_query
else:
    # ``wire_metrics_query`` builds pydantic models at import (~44ms measured,
    # and it pulls ``wire_metrics`` with it). Only ``_parse_metric_mask`` reads
    # its axis/op constants, so an eager import taxes every ``trax`` cold start
    # -- including plain ``trax issue`` -- for a grammar branch most commands
    # never reach. It also defeats the matching ``lazy_import`` in
    # ``client.client``, which cannot help once this module has already loaded
    # the real thing.
    from wrapt import lazy_import

    wire_metrics_query = lazy_import("trackinizer.wire.wire_metrics_query")


# Every CLI filter field across all kinds. Used only to tell a mistyped
# operator (``owner not ...``) from an unrelated token: a known filter field
# in field position with a garbage operator is a lexical error, diagnosed once
# in the scanner rather than misattributed by a downstream consumer.
_ALL_FILTER_FIELDS: frozenset[str] = frozenset(
    field for names in FILTER_FIELDS_CLI.values() for field in names
)


def parse_list_query(
    kind: Inquiry.InquiryKind,
    tokens: Sequence[str],
) -> ListQuery | None:
    """Parse list-query tokens; return ``None`` if this is a row or create form."""
    if not tokens:
        return ListQuery(kinds=(kind,), ranges={}, filters=())
    if starts_with_ref(tokens):
        return None
    kinds: list[Inquiry.InquiryKind] = [kind]
    ranges: dict[Inquiry.InquiryKind, tuple[SeqRange, ...]] = {}
    cli_filters: list[tuple[str, FilterOp, str]] = []  # CLI names, not yet canonical
    committed = False  # a range or filter has fixed this as a query, not a create
    for clause in _scan_clauses(tokens):
        if isinstance(clause, _KindClause):
            kinds.append(clause.kind)
        elif isinstance(clause, _RangeClause):
            # Per GRAMMAR.md 6, a range token binds to the most recently named
            # kind, last-wins across space-separated tokens. The union lives
            # inside one comma-separated token (``222..260,279..``), carried as
            # the clause's interval tuple.
            ranges[kinds[-1]] = clause.ranges
            committed = True
        elif isinstance(clause, _FilterClause):
            cli_filters.append((clause.field, clause.op, clause.value))
            committed = True
        else:
            # A mutation or an unrecognized token: not a plain list query. A
            # mistyped filter operator (``owner not re Dan``) raises only once
            # a prior filter or range has committed the stream to a query;
            # leading, it is indistinguishable from a create missing ``to``,
            # so it falls through to the create path's clearer error.
            if (
                committed
                and isinstance(clause, _UnknownClause)
                and clause.bad_filter_op
            ):
                raise ClientError(
                    f"unknown filter operator {clause.bad_filter_op!r} "
                    f"after field {clause.token!r}"
                )
            return None
    kinds_tuple = tuple(dict.fromkeys(kinds))
    _validate_cli_filters(kinds_tuple, cli_filters)
    # Canonicalize every CLI alias before the ``Filter`` crosses the wire,
    # through the same ``wire/filters`` map the server and row evaluator use.
    # The server rejects any alias that slips through a non-CLI HTTP caller.
    filters: list[Filter] = []
    for field, op, value in cli_filters:
        try:
            filters.append(
                Filter(field=canonical_filter_field(field), op=op, value=value)
            )
        except ValueError as err_obj:
            # ``Filter`` validates the whole clause -- length, regex dialect,
            # presence ops on a NOT-NULL column. Those refusals describe the
            # user's input, so they must reach the CLI as the message every
            # other parse failure here produces; a raw ``ValueError`` would
            # surface as a traceback instead.
            raise ClientError(str(err_obj)) from err_obj
    return ListQuery(kinds=kinds_tuple, ranges=ranges, filters=tuple(filters))


@dataclass(frozen=True, kw_only=True, slots=True)
class _KindClause:
    """A bare kind token: widens the listed set."""

    kind: Inquiry.InquiryKind


@dataclass(frozen=True, kw_only=True, slots=True)
class _RangeClause:
    """A ``start..stop`` selector, or a comma-separated union of them."""

    ranges: tuple[SeqRange, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class _FilterClause:
    """A ``field op value`` predicate in raw CLI spelling."""

    field: str
    op: FilterOp
    value: str


@dataclass(frozen=True, kw_only=True, slots=True)
class _MutationClause:
    """A ``field to/add/del value`` field mutation, not yet parsed.

    ``tokens`` is usually three (``field op value``) but spans the full typed
    ref (``result add paper 1`` -> four) for a ref-list field, so a typed ref
    is not truncated before ``parse_actions`` resolves it.
    """

    tokens: tuple[str, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class _UnknownClause:
    """A token that opens none of the above (edge, relation, create field).

    ``bad_filter_op`` is set when the token is a known filter field whose
    following token is neither a filter nor a mutation operator -- a mistyped
    operator (``owner not ...``). It carries the offending operator so a
    consumer already in a filter context can report it precisely; a consumer
    still deciding between query and create ignores it and falls through.
    """

    token: str
    bad_filter_op: str | None = None


_Clause = _KindClause | _RangeClause | _FilterClause | _MutationClause | _UnknownClause


def _is_mutation_head(field: str, op: str | None) -> bool:
    """Whether ``field op`` opens a bulk-apply mutation triple.

    A scalar field takes ``to``; a list field takes ``to``, ``add``, or
    ``del``. Anything else (edges, relations, costs) is not a field mutation
    and is rejected once a selector has committed to the bulk-apply form.
    """
    if field in EDITABLE_FIELDS:
        return op == "to"
    if field in LIST_FIELDS:
        return op in ("to", "add", "del")
    return False


def _mutation_span(tokens: Sequence[str], index: int) -> int:
    """Token width of the mutation at ``index``: ``field op value(s)``.

    Three for a scalar or plain-list mutation; ``2 + consumed`` for a ref-list
    field whose value is a typed ``kind seq`` ref (``codechange add 7``), so
    the ref survives the bulk-apply scan intact (trax #419).
    """
    spec = FIELDS_BY_NAME.get(tokens[index].lower())
    if spec is None or spec.ref_kind is None:
        return 3
    _, consumed = consume_ref(tokens, index + 2, kind_hint=spec.ref_kind)
    return 2 + consumed


def _scan_clauses(tokens: Sequence[str]) -> Iterator[_Clause]:
    """Tokenize a row tail into the clause grammar shared by query forms.

    One walk of the ``kind | range | filter | mutation`` grammar, emitting a
    typed clause per step and leaving the keep/reject policy to each consumer.
    The only lexical error raised here is a missing filter value, which is
    unambiguous regardless of context. A known filter field with a garbage
    operator (``owner not ...``) is flagged on its :class:`_UnknownClause` via
    ``bad_filter_op`` rather than raised, because whether it is an error
    depends on context: an error inside a committed filter context, but a
    plain create field where no query has been established.
    """
    index = 0
    while index < len(tokens):
        token_text = tokens[index].lower()
        op_next = token(tokens, index + 1)
        op_next_lower = op_next.lower() if op_next is not None else None
        if token_text in KIND_LOWER and not _is_mutation_head(
            token_text, op_next_lower
        ):
            # A token that is both a kind keyword and a list field (only
            # ``codechange``) is a bare kind ONLY when no mutation operator
            # follows: ``trax issue belief`` widens, but ``codechange add 7`` is
            # a field mutation. A bare kind is never followed by to/add/del, so
            # the next token disambiguates without ambiguity.
            yield _KindClause(kind=KIND_LOWER[token_text])
            index += 1
        elif ranges := _parse_range(token_text):
            yield _RangeClause(ranges=ranges)
            index += 1
        elif op_next_lower in FILTER_OPS:
            op_value = op_next_lower
            if op_value in VALUELESS_FILTER_OPS:
                # ``isnull`` / ``notnull`` carry no operand: two tokens, no value.
                yield _FilterClause(field=token_text, op=op_value, value="")
                index += 2
            else:
                if index + 2 >= len(tokens):
                    raise ClientError(
                        f"expected value for filter field {tokens[index]!r}"
                    )
                yield _FilterClause(
                    field=token_text, op=op_value, value=tokens[index + 2]
                )
                index += 3
        elif _is_mutation_head(token_text, op_next_lower):
            span = _mutation_span(tokens, index)
            yield _MutationClause(tokens=tuple(tokens[index : index + span]))
            index += span
        else:
            bad_op = (
                op_next
                if token_text in _ALL_FILTER_FIELDS and op_next_lower is not None
                else None
            )
            yield _UnknownClause(token=tokens[index], bad_filter_op=bad_op)
            index += 1


def parse_bulk_apply(
    kind: Inquiry.InquiryKind,
    tokens: Sequence[str],
) -> BulkApply | None:
    """Parse ``<query> <field to value>+`` into a bulk apply, or ``None``.

    Returns a :class:`BulkApply` only when the tokens carry at least one
    range or filter (the row-selection half) alongside at least one ``field
    to value`` mutation. With no range or filter the tokens are a create, so
    this yields ``None`` and the caller falls through to the create path; with
    no mutation they are a plain list query, likewise ``None``.

    Query and mutation tokens may interleave: the shared :func:`_scan_clauses`
    routes each ``field _ value`` triple by its operator -- ``to`` is a
    mutation, a filter op is a predicate -- so the two halves are recovered
    without a separator keyword.

    Args:
      kind: The leading inquiry kind the row command is keyed on.
      tokens: The row tail after the kind.

    Returns:
      bulk_apply: The parsed bulk apply, or ``None`` when the tokens are a
        create or a plain list query.

    """
    if not tokens or starts_with_ref(tokens):
        return None
    query_tokens: list[str] = []
    mutation_tokens: list[str] = []
    has_selector = False
    for clause in _scan_clauses(tokens):
        if isinstance(clause, _KindClause):
            # A bare kind only widens the listed set; it does not constrain
            # one, so it is not a selector. Without a range or filter the
            # tokens stay a create, never an unbounded bulk apply.
            query_tokens.append(clause.kind)
        elif isinstance(clause, _RangeClause):
            query_tokens.append(_range_text(clause.ranges))
            has_selector = True
        elif isinstance(clause, _FilterClause):
            # A valueless op (isnull/notnull) re-emits as two tokens, mirroring
            # its CLI surface, so the round-trip through parse_list_query below
            # does not see an empty stray value token.
            if clause.op in VALUELESS_FILTER_OPS:
                query_tokens.extend((clause.field, clause.op))
            else:
                query_tokens.extend((clause.field, clause.op, clause.value))
            has_selector = True
        elif isinstance(clause, _MutationClause):
            mutation_tokens.extend(clause.tokens)
        elif has_selector:
            # A range or filter has committed this to a bulk apply, so a token
            # that is neither a further selector nor a field mutation is an
            # error -- never a silent fall-through to create or list query. A
            # mistyped filter operator (``status is active owner not re Dan``)
            # is reported as such; anything else as a non-mutation token.
            if clause.bad_filter_op:
                raise ClientError(
                    f"unknown filter operator {clause.bad_filter_op!r} "
                    f"after field {clause.token!r}"
                )
            raise ClientError(
                f"bulk apply supports only field mutations; got {clause.token!r}"
            )
        else:
            # No selector yet, and this token is not one: the tokens are a
            # create or an edge form, not a bulk apply -- including a filter
            # field with a garbage operator, which is a create field here.
            return None
    if not has_selector or not mutation_tokens:
        return None
    query = parse_list_query(kind, query_tokens)
    if query is None:
        return None
    # ``_is_mutation_head`` gates ``mutation_tokens`` to scalar/list fields, so
    # ``parse_actions`` can only yield field mutations; ``_as_bulk_action``
    # raises if that ever ceases to hold.
    actions = tuple(
        _as_bulk_action(action) for action in parse_actions(mutation_tokens)
    )
    return BulkApply(query=query, actions=actions)


def _range_text(ranges: tuple[SeqRange, ...]) -> str:
    """Render parsed ranges back to their comma-separated selector token.

    The inverse of :func:`_parse_range` for re-parsing purposes: the
    bulk-apply selector half is re-emitted as one token that
    :func:`parse_list_query` re-parses. Each interval uses the wire spelling
    (a single-row interval renders ``n..n``), which :func:`_parse_interval`
    accepts identically to the bare ``n`` the user may have typed.
    """
    return ",".join(format_interval(interval) for interval in ranges)


def _as_bulk_action(action: Action) -> SetField | AddList | RemoveList:
    """Narrow a parsed action to a field mutation, raising on anything else.

    ``_is_mutation_head`` already gates the mutation tokens to scalar and list
    fields, so this never fires in practice; it is the tripwire that keeps the
    two in lockstep if either changes.
    """
    if isinstance(action, SetField | AddList | RemoveList):
        return action
    raise ClientError(
        "bulk apply supports only field mutations (FIELD to/add/del VALUE)"
    )


def parse_actions(tokens: Sequence[str]) -> list[Action]:
    """Parse row-local action tokens.

    Keywords match case-insensitively (GRAMMAR.md §1); values are kept
    verbatim. ``del`` is terminal, so any token after it is rejected. A
    scalar field may be set only once per command.
    """
    actions: list[Action] = []
    set_fields: set[str] = set()
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        word = raw.lower()
        if word == "del":
            if index + 1 < len(tokens):
                raise ClientError(
                    f"'del' must be the last token; got {tokens[index + 1]!r} after"
                )
            actions.append(DeleteRow())
            index += 1
        elif word in EDITABLE_FIELDS:
            action, consumed = _parse_scalar_action(tokens, index, word)
            if isinstance(action, SetField):
                if action.field in set_fields:
                    raise ClientError(
                        f"scalar field {action.field!r} set more than once"
                    )
                set_fields.add(action.field)
            actions.append(action)
            index += consumed
        elif word in COST_FIELDS:
            action, consumed = _parse_cost_action(tokens, index, word)
            actions.append(action)
            index += consumed
        elif word in LIST_FIELDS:
            action, consumed = _parse_list_action(tokens, index, word)
            actions.append(action)
            index += consumed
        elif relation := RELATION_ALIASES.get(word):
            action, consumed = _parse_relation_or_edge(tokens, index, word, relation)
            actions.append(action)
            index += consumed
        elif edge := EDGE_ALIASES.get(word):
            action, consumed = _parse_edge_action(tokens, index, edge)
            actions.append(action)
            index += consumed
        else:
            raise ClientError(f"unexpected token: {raw!r}")
    return actions


def parse_metric_action(tokens: Sequence[str]) -> MetricAction:
    """Parse a ``metric`` tail into masks plus one grid operation.

    ``tokens`` is everything after the leading ``metric`` keyword. The grammar
    (see ``docs/metric-grammar.md``) is::

        metric_tail ::= mask_clause* write? read_opts?
        mask_clause ::= "at" field op value | "at" bareword
        write       ::= "to" value
        read_opts   ::= ("sort" ("asc"|"desc"))? ("limit" INT)?

    Masks AND together in order. ``at <bareword>`` (a token not in
    ``key``/``step``/``value``) is the shorthand ``at key is <bareword>``. The
    step-axis reductions ``max``/``min`` take no value. ``sort``/``limit`` window
    a read; combining either with a ``to`` write is an error. The parser is
    purely structural: the ``to`` value stays a raw string (a later layer coerces
    it and enforces finiteness).

    Args:
      tokens: The row tail after the ``metric`` keyword.

    Returns:
      action: The parsed masks, optional write target, and read options.

    Raises:
      ClientError: On a malformed clause, an unknown op, a non-positive
        ``limit``, or ``sort``/``limit`` combined with a ``to`` write.

    """
    masks: list[MetricMask] = []
    write: str | None = None
    sort: Literal["asc", "desc"] | None = None
    limit: int | None = None
    index = 0
    while index < len(tokens):
        word = tokens[index].lower()
        if word == "at":
            mask, index = _parse_metric_mask(tokens, index + 1)
            masks.append(mask)
        elif word == "to":
            write = required_token(tokens, index + 1, "'to' requires a value")
            index += 2
        elif word == "sort":
            sort = _parse_metric_sort(
                required_token(tokens, index + 1, "'sort' requires asc or desc")
            )
            index += 2
        elif word == "limit":
            limit = _parse_metric_limit(
                required_token(tokens, index + 1, "'limit' requires a positive integer")
            )
            index += 2
        else:
            raise ClientError(f"unexpected token in metric tail: {tokens[index]!r}")
    if write is not None and (sort is not None or limit is not None):
        raise ClientError("sort/limit apply to reads, not writes")
    return MetricAction(masks=tuple(masks), write=write, sort=sort, limit=limit)


def _parse_metric_mask(tokens: Sequence[str], index: int) -> tuple[MetricMask, int]:
    """Parse one ``at`` clause starting at ``index`` (past the ``at``).

    Returns the mask and the index just past it. A bareword (a token not in
    ``key``/``step``/``value``) is the ``at key is <bareword>`` shorthand.

    The axis, comparator, and reduction sets are read from the wire constants
    (the ONE definition), so the CLI parser cannot drift from the wire model or
    the store: adding a metric op in one place is a type error until the wire
    ``Literal`` gains it too. This is the fix for the class of bug where the
    parser gated on the broad inquiry ``FILTER_OPS`` and admitted ops (``re`` /
    ``isnull``) the grid does not support.
    """
    head = required_token(tokens, index, "'at' requires a field or key")
    if head.lower() not in frozenset(wire_metrics_query.METRIC_AXES):
        return MetricMask(field="key", op="is", value=head), index + 1
    field = cast("Literal['key', 'step', 'value']", head.lower())
    op = required_token(
        tokens, index + 1, f"expected an operator after {field!r}"
    ).lower()
    # Step-axis reductions: highest/lowest step per key ("final"/"first"). They
    # take no value and apply only to the ``step`` axis (metric-grammar.md
    # Grammar summary).
    if op in frozenset(get_args(wire_metrics_query.MetricReduce.__value__)):
        if field != "step":
            raise ClientError(f"{op} applies to step only, not {field!r}")
        return MetricMask(field=field, op=op, value=""), index + 2
    # Gate on the narrow metric comparator set, NOT the broad inquiry
    # ``FILTER_OPS``: the grid has no text-regex or presence ops, so ``re`` /
    # ``nre`` / ``isnull`` / ``notnull`` are unknown here. A presence op thus
    # falls into this branch instead of being treated as a valued comparator,
    # so it can never consume the following clause's token as a spurious value.
    if op not in frozenset(wire_metrics_query.METRIC_COMPARE_OPS):
        raise ClientError(f"unknown metric operator {op!r} after {field!r}")
    value = required_token(tokens, index + 2, f"expected a value after {field} {op}")
    return MetricMask(field=field, op=op, value=value), index + 3


def _parse_metric_sort(direction: str) -> Literal["asc", "desc"]:
    """Parse a ``sort`` operand into a direction literal, raising otherwise."""
    lowered = direction.lower()
    if lowered == "asc":
        return "asc"
    if lowered == "desc":
        return "desc"
    raise ClientError(f"'sort' takes asc or desc, got {direction!r}")


def _parse_metric_limit(token_text: str) -> int:
    """Parse a ``limit`` operand into a positive int, raising otherwise."""
    try:
        limit = int(token_text)
    except ValueError:
        raise ClientError(
            f"limit must be a positive integer, got {token_text!r}"
        ) from None
    if limit <= 0:
        raise ClientError(f"limit must be a positive integer, got {token_text!r}")
    return limit


def ref_text(ref: Ref) -> str:
    """CLI spelling for ``ref``: seq number or UUID."""
    if isinstance(ref, SeqRef):
        return str(ref.seq)
    return str(ref.uuid)


def starts_with_ref(tokens: Sequence[str]) -> bool:
    """Whether the first token is a seq number or UUID."""
    return bool(tokens) and (
        tokens[0].isdigit() or UUID_RE.match(tokens[0]) is not None
    )


def parse_subject_list(
    tokens: Sequence[str], *, default_kind: Inquiry.InquiryKind
) -> list[Ref] | None:
    """Return a list of refs when ``tokens`` is purely a ref sequence.

    Accepts bare seqs (under the leading or most recently named kind),
    explicit ``kind seq`` pairs, and UUIDs. Returns ``None`` if any token
    is a tail keyword (field, edge, op) or a trailing kind has no seq.
    """
    subjects: list[Ref] = []
    current_kind = default_kind
    pos = 0
    while pos < len(tokens):
        token = tokens[pos]
        lower = token.lower()
        if lower in KIND_LOWER:
            if pos + 1 >= len(tokens):
                return None
            next_token = tokens[pos + 1]
            if not (next_token.isdigit() or UUID_RE.match(next_token)):
                return None
            current_kind = KIND_LOWER[lower]
            pos += 1
            continue
        if token.isdigit():
            subjects.append(SeqRef(kind=current_kind, seq=int(token)))
            pos += 1
            continue
        if UUID_RE.match(token):
            subjects.append(UuidRef(uuid=uuid.UUID(token), expected_kind=current_kind))
            pos += 1
            continue
        return None
    return subjects or None


_EDGE_METADATA_FIELDS: frozenset[str] = frozenset(
    {"priority", "note", "valence", "label", "labels"}
)

# The subset of edge-metadata words that are NOT also row fields, so they carry
# exactly one meaning regardless of position. Only these may interleave with
# fields inside an inline-create body; the collision words (``label`` / ``labels``
# / ``priority``, which are also row fields) stay positional -- row field in a
# create body, edge annotation only after an explicit edge ref. Derived by
# subtraction so it cannot drift if either set gains a member.
_SAFE_INLINE_META_FIELDS: frozenset[str] = _EDGE_METADATA_FIELDS - (
    frozenset(EDITABLE_FIELDS) | frozenset(LIST_FIELDS)
)


def edge_metadata(
    tokens: Sequence[str], *, allow_bare_collision: bool = False
) -> tuple[Mapping[str, object], int]:
    """Parse edge-metadata actions, stopping at the first non-metadata token.

    The caller resumes parsing the rest as further tails of the leading
    subject. This is how edge actions chain under one anchor: in
    ``... blocked_by issue 7 blocks issue 8``, ``blocks`` isn't a metadata
    field, so this returns and the outer loop picks ``blocks`` up next.

    ``allow_bare_collision``: when True, the collision words
    (``priority``/``label``/``labels``) are accepted WITHOUT the ``edge`` marker.
    Set in the PRE-target position (between the edge keyword and its target),
    where metadata is unambiguous -- there is no row yet for them to mean. After
    a target they stay marker-required (a bare collision word is a vertex field
    that rolls up to the subject).
    """
    metadata: dict[str, object] = {}
    index = 0
    while index < len(tokens):
        word = tokens[index].lower()
        # The ``edge`` marker forces the edge reading of the next metadata word.
        # It is REQUIRED for the collision words (priority/label/labels, which are
        # also row fields) and OPTIONAL for the edge-only words (note/valence) --
        # unless ``allow_bare_collision`` (pre-target), where all words are bare.
        marked = word == "edge"
        field_index = index + 1 if marked else index
        field = token(tokens, field_index)
        field = field.lower() if field is not None else ""
        if field == "del" or field not in _EDGE_METADATA_FIELDS:
            if marked:
                raise ClientError(
                    f"'edge' must be followed by an edge field "
                    f"({', '.join(sorted(_EDGE_METADATA_FIELDS))}), got {field!r}"
                )
            break
        # A collision word (also a row field) needs the ``edge`` marker to mean the
        # edge -- except pre-target (``allow_bare_collision``), where it is
        # unambiguous. Bare and post-target, it is a vertex field: stop here.
        if (
            field not in _SAFE_INLINE_META_FIELDS
            and not marked
            and not allow_bare_collision
        ):
            break
        op = required_token(
            tokens, field_index + 1, f"expected operation for edge metadata {field}"
        ).lower()
        value = required_token(
            tokens, field_index + 2, f"expected value for edge metadata {field}"
        )
        if field == "priority":
            if op != "to":
                raise ClientError("edge priority uses to")
            metadata["priority"] = field_value("priority", value)
        elif field == "note":
            if op != "to":
                raise ClientError("edge note uses to")
            metadata["note"] = value
        elif field == "valence":
            if op != "to":
                raise ClientError("edge valence uses to")
            try:
                metadata["valence"] = float(value)
            except ValueError:
                # A non-numeric valence must surface as a clean ClientError, not a
                # raw ValueError leaking a Python traceback to the CLI (mirrors
                # ``priority``'s int check).
                raise ClientError(
                    f"edge valence must be a number, got {value!r}"
                ) from None
        else:
            # ``label`` / ``labels``: the membership check above leaves
            # this branch handling every remaining valid field.
            if op not in ("to", "add", "del"):
                raise ClientError("edge label uses to, add, or del")
            labels = list(cast(Sequence[str], metadata.get("labels") or ()))
            if op == "to":
                labels = resolve_labels((value,))
            elif op == "add":
                labels.extend(resolve_labels((value,)))
            else:
                removed = frozenset(resolve_labels((value,)))
                labels = [label for label in labels if label not in removed]
            metadata["labels"] = labels
        # field + op + value (3), plus the ``edge`` marker when present.
        index += 4 if marked else 3
    return metadata, index


def token(tokens: Sequence[str], index: int) -> str | None:
    """``tokens[index]`` if in range, else ``None``."""
    return tokens[index] if index < len(tokens) else None


def required_token(tokens: Sequence[str], index: int, message: str) -> str:
    """``tokens[index]`` if in range, else raise ``ClientError(message)``."""
    if index >= len(tokens):
        raise ClientError(message)
    return tokens[index]


def _parse_range(token_text: str) -> tuple[SeqRange, ...]:
    """Parse a range token into one or more intervals, or ``()`` if not a range.

    A token is a range token when it carries ``..`` or a comma. A comma
    unions disjoint elements in a single token (``..10,222..225,227,228..``);
    each comma-separated element is either a ``start..stop`` interval or a
    bare seq, which is the degenerate interval ``n..n``. The empty tuple
    signals "not a range token" so the scanner falls through to the next
    clause kind, leaving a lone seq (``227``) to parse as a ref.
    """
    if ".." not in token_text and "," not in token_text:
        return ()
    return tuple(_parse_interval(part) for part in token_text.split(","))


def _parse_interval(part: str) -> SeqRange:
    """Parse one range element -- an interval or a bare seq -- raising if malformed.

    A bare seq ``n`` (no ``..``) is the single-row interval ``n..n``, the
    CLI-only ergonomic the wire never sees; an element with ``..`` defers to
    the shared wire :func:`parse_interval` so the CLI and the server agree on
    one interval grammar. An empty element (a stray comma) is an error.
    """
    if ".." not in part:
        if not part.isdigit():
            raise ClientError(f"invalid range element {part!r}")
        return SeqRange(start=int(part), stop=int(part))
    try:
        return parse_interval(part)
    except ValueError as err:
        raise ClientError(str(err)) from err


def _validate_cli_filters(
    kinds: Sequence[Inquiry.InquiryKind],
    filters: Sequence[tuple[str, FilterOp, str]],
) -> None:
    """Reject filters whose CLI field isn't valid for every listed kind.

    Runs before canonicalization, so the error names the spelling the user
    typed (``kind``) rather than the server-canonical ``issue_kind``.
    """
    for field, _op, _value in filters:
        missing = [k for k in kinds if field not in FILTER_FIELDS_CLI[k]]
        if not missing:
            continue
        raise ClientError(f"unknown filter field {field!r} for {', '.join(missing)}")


def consume_ref(
    args: Sequence[str],
    pos: int = 0,
    *,
    kind_hint: Inquiry.InquiryKind | None = None,
) -> tuple[Ref, int]:
    """Consume a reference at ``args[pos]``; return it and how many tokens it used."""
    if pos >= len(args):
        raise ClientError("expected reference")
    head = args[pos]
    if UUID_RE.match(head):
        return UuidRef(uuid=uuid.UUID(head), expected_kind=kind_hint), 1
    if kind_hint is not None and head.isdigit():
        return SeqRef(kind=kind_hint, seq=int(head)), 1
    if head.lower() in KIND_LOWER:
        if pos + 1 >= len(args):
            raise ClientError(f"incomplete ref: kind {head!r} without seq")
        seq_token = args[pos + 1]
        if seq_token.isdigit():
            return SeqRef(kind=parse_kind(head), seq=int(seq_token)), 2
        if UUID_RE.match(seq_token):
            return UuidRef(
                uuid=uuid.UUID(seq_token),
                expected_kind=parse_kind(head),
            ), 2
        raise ClientError(
            f"expected seq number or uuid after kind {head!r}, got {seq_token!r}"
        )
    raise ClientError(f"cannot parse reference at {head!r}")


def _parse_scalar_action(
    tokens: Sequence[str], index: int, field: str
) -> tuple[Action, int]:
    op = token(tokens, index + 1)
    op_lower = op.lower() if op is not None else None
    if op_lower != "to":
        return ReadField(field=field), 1
    value = required_token(tokens, index + 2, f"expected value for {field}")
    # No lookahead past the value: a trailing ``del`` is the terminal row
    # delete, reached by ``parse_actions``'s outer loop, not a field delete.
    # The old ``index+3`` peek misread a chained set's terminal ``del`` (e.g.
    # ``title to x description to y del``) as a delete of this field (BUG-001).
    return SetField(field=field, value=field_value(field, value)), 3


def _parse_cost_action(
    tokens: Sequence[str], index: int, field: str
) -> tuple[Action, int]:
    op = token(tokens, index + 1)
    if op is None:
        return ReadField(field=field), 1
    if op.lower() != "add":
        raise ClientError("cost fields use add with a signed USD delta")
    value = required_token(tokens, index + 2, f"expected value for {field}")
    try:
        delta = float(value)
    except ValueError:
        # A non-numeric cost must surface as a clean ClientError, not a raw
        # ValueError leaking a Python traceback to the CLI (mirrors the edge
        # valence guard).
        raise ClientError(f"cost {field!r} must be a number, got {value!r}") from None
    return AddCost(field=field, value=delta), 3


def _parse_list_action(
    tokens: Sequence[str], index: int, field: str
) -> tuple[Action, int]:
    op = token(tokens, index + 1)
    op_lower = op.lower() if op is not None else None
    if op_lower is None:
        return ReadField(field=list_payload_field(field)), 1
    if op_lower not in ("to", "add", "del"):
        raise ClientError(f"list field {field} uses to, add, or del")
    if op_lower == "del" and token(tokens, index + 2) is None:
        raise ClientError(f"del requires a value for list field {field!r}")
    # Ref-list fields (codechange(s) -> CodeChange) take a typed `kind seq` ref
    # (one or two tokens); the parsed Ref rides on the action so the verb layer
    # resolves it once (trax #419). Plain list fields (label, subscriber) take
    # one free-string token.
    spec = FIELDS_BY_NAME.get(field)
    if spec is not None and spec.ref_kind is not None:
        ref, consumed = consume_ref(tokens, index + 2, kind_hint=spec.ref_kind)
        if op_lower == "to":
            return SetField(field=list_payload_field(field), value=(ref,)), consumed + 2
        if op_lower == "add":
            return AddList(field=field, value=ref_text(ref), ref=ref), consumed + 2
        return RemoveList(field=field, value=ref_text(ref), ref=ref), consumed + 2
    value = required_token(tokens, index + 2, f"expected value for {field}")
    if op_lower == "to":
        return SetField(field=list_payload_field(field), value=(value,)), 3
    if op_lower == "add":
        return AddList(field=field, value=value), 3
    return RemoveList(field=field, value=value), 3


def _parse_relation_or_edge(
    tokens: Sequence[str],
    index: int,
    word: str,
    relation: tuple[str, bool],
) -> tuple[Action, int]:
    """Decide between a relation projection and an edge mutation.

    A digit, UUID, or no-token tail means "list rows in this relation" (or
    select one by index). Anything else looks like a ref, so dispatch to
    ``_parse_edge_action`` if the keyword is also an edge alias. A
    relation-only alias followed by a ref is a grammar error, not a KeyError.
    """
    value = token(tokens, index + 1)
    if value is None or value.isdigit():
        return RelationAction(
            relation=relation,
            index=value or "",
            against=word in AGAINST_RELATION_SPELLINGS,
        ), 2 if value else 1
    edge = EDGE_ALIASES.get(word)
    if edge is None:
        raise ClientError(
            f"{word!r} is a relation, not an edge; it does not accept a ref"
        )
    return _parse_edge_action(tokens, index, edge)


def _parse_edge_action(
    tokens: Sequence[str],
    index: int,
    edge: Edge,
) -> tuple[Action, int]:
    # PRE-target metadata: words between the edge keyword and its target annotate
    # the edge unambiguously (no target yet for them to mean), so even the
    # collision words are accepted bare here. ``narrows priority to high issue 3``
    # == ``narrows issue 3 edge priority to high``.
    pre_meta, pre_consumed = edge_metadata(
        tokens[index + 1 :], allow_bare_collision=True
    )
    target_start = index + 1 + pre_consumed
    target, consumed = consume_edge_target(tokens, target_start)
    offset = target_start + consumed
    terminator = token(tokens, offset)
    if terminator is not None and terminator.lower() == "del":
        if isinstance(target, InlineCreate):
            raise ClientError("cannot 'del' an inline-create edge target")
        if pre_meta:
            raise ClientError("cannot combine edge metadata with 'del'")
        return EdgeAction(
            edge=edge, target=target, metadata={}, remove=True
        ), pre_consumed + consumed + 2
    # An inline-create target captures metadata written right after its fields as
    # THIS edge's annotation (the edge that produced the node -- the deepest edge so
    # far). Trailing metadata after the target works too and merges in; the inline
    # form is what lets a verdict note sit beside the node it describes without a
    # `begin ... end` wrapper.
    inbound = dict(target.inbound_meta) if isinstance(target, InlineCreate) else {}
    # POST-target metadata: a bare collision word here is a vertex field (maximal
    # munch rolls it up to the subject), so ``edge_metadata`` stops at it; only an
    # ``edge``-marked or edge-only word is consumed. Merge precedence on a same-key
    # tie is last-write-wins: inbound (an inline target's own meta), then pre-target,
    # then post-target -- so post-target overrides pre. Setting the same key twice
    # is malformed input either way; the order just makes it deterministic.
    metadata, consumed_metadata = edge_metadata(tokens[offset:])
    merged = _apply_valence_alias(edge, {**inbound, **dict(pre_meta), **metadata})
    return EdgeAction(
        edge=edge,
        target=target,
        metadata=merged,
        annotate=bool(merged),
    ), pre_consumed + consumed + 1 + consumed_metadata


def _apply_valence_alias(edge: Edge, metadata: dict[str, object]) -> dict[str, object]:
    """Resolve a citation alias's valence convention into a concrete ``valence``.

    A plain ``proves`` / ``favors`` defaults to ``+0.5``; a ``dis*`` spelling
    defaults to ``-0.5``. Either way the polarity is the SPELLING and the value
    is the magnitude, so a user-supplied valence must be non-negative on BOTH
    branches: the positive spelling stores it as-is, the ``dis*`` spelling
    negates it. For a non-citation alias (``valence_default`` unset) the
    metadata passes through.
    """
    if edge.valence_default is None:
        return metadata
    given = metadata.get("valence")
    if given is None:
        metadata["valence"] = edge.valence_default
        return metadata
    value = FloatCodec.coerce(given)
    if value < 0:
        # The magnitude is non-negative; the for/against polarity is carried by
        # the spelling (plain vs ``dis*``), not by a negative value. A positive
        # spelling accepting a negative valence would store an against-citation
        # under a for-spelling (BUG-002).
        polarity = "'dis...'" if edge.valence_negate else "a citation"
        raise ClientError(
            f"{polarity} edge takes a non-negative valence "
            f"(the spelling sets the for/against polarity); got {value}"
        )
    metadata["valence"] = -value if edge.valence_negate else value
    return metadata


def consume_edge_target(args: Sequence[str], pos: int = 0) -> tuple[EdgeTarget, int]:
    """Consume a ref or an inline-create as an edge-action target.

    Forms:
      * ``uuid`` / ``kind seq`` / ``kind uuid``        -- an existing ref.
      * ``kind field to value ...``                    -- a bare inline-create.
        It carries its own fields AND, after them, its own cost and nested edges
        (the DEEP cursor descends into it), terminating at the first token that
        opens none of those.
      * ``begin kind ... end``                         -- a grouped inline-create.
        Same body as a bare inline-create but the ``end`` is an explicit pop, so
        the edge AFTER the group rebinds to the PARENT (go wide / fan out).

    ``begin`` / ``end`` are bare words, not punctuation, so they stay inert in
    bash and fish; their literal spelling is the whole vocabulary, so they
    read better inline than behind a constant.
    """
    if pos >= len(args):
        raise ClientError("expected reference")
    head = args[pos]
    if head.lower() == "begin":
        inline, consumed = _consume_inline_create(args, pos + 1, grouped=True)
        end = token(args, pos + 1 + consumed)
        if end is None or end.lower() != "end":
            raise ClientError("'begin' group must be closed by 'end'")
        return inline, consumed + 2  # +begin +end
    if UUID_RE.match(head):
        return UuidRef(uuid=uuid.UUID(head)), 1
    if head.lower() in KIND_LOWER:
        next_tok = token(args, pos + 1)
        next_lower = next_tok.lower() if next_tok is not None else None
        if next_lower in (*EDITABLE_FIELDS, *LIST_FIELDS):
            return _consume_inline_create(args, pos)
        # A kind followed by metadata/cost/edge (not a field) is an inline create
        # that mistakenly leads with something other than a field. Say so, rather
        # than letting ``consume_ref`` report the opaque "expected seq number or
        # uuid" -- the real fix is to put a field first.
        if next_lower in (*COST_FIELDS, *_EDGE_METADATA_FIELDS, "edge") or (
            next_lower is not None and next_lower in EDGE_ALIASES
        ):
            raise ClientError(
                f"inline create for {head!r} must lead with a field, not "
                f"{next_tok!r}; write a field (e.g. 'title to ...') first"
            )
    return consume_ref(args, pos)


def _consume_inline_create(
    args: Sequence[str], pos: int, *, grouped: bool = False
) -> tuple[InlineCreate, int]:
    """Consume ``kind`` then the node's fields, costs, metadata, and edges.

    The create must LEAD with a field (the dispatch in ``consume_edge_target``
    recognizes an inline create by a field token right after the kind). After
    that, three classes interleave in any order before the node's first outgoing
    edge: create FIELDS, ``agent-cost``/``resource-cost`` deltas, and the
    UNAMBIGUOUS producer-edge metadata ``note``/``valence`` (edge-only words).
    The COLLISION words ``priority``/``label``/``labels`` are ALSO row fields, so
    inside the body they are ROW fields; their edge-annotation meaning needs the
    ``edge`` marker (``edge priority to high``). The one hard boundary is the
    node's first OUTGOING edge: after it, metadata binds to that CHILD (hoisted
    by ``_parse_edge_action``) and a stray field has no home (the node is
    closed), which raises rather than silently rebinding to the caller. Each
    edge's inline target recurses, so ``produced ws note to v agent-cost add N
    produced paper favors belief`` lands the note+cost on ws and nests paper
    under ws. Any non-{field,cost,metadata,edge} token ends the node and rebinds
    to the caller's anchor. When ``grouped`` (inside ``begin ... end``) it also
    stops at ``end`` so the parent can fan out. At least one field is required.
    """
    # Bounds + kind validity: reached via ``begin <kind> ...`` and the inline
    # dispatch. A missing or non-kind token here (e.g. ``begin`` at end of input,
    # or ``begin 3``) must be a clean ClientError, not a raw IndexError / the
    # ValueError ``parse_kind`` raises.
    if pos >= len(args):
        raise ClientError("expected a kind to start an inline create")
    try:
        kind = parse_kind(args[pos])
    except ValueError as err:
        raise ClientError(str(err)) from err
    fields: list[SetField] = []
    costs: list[AddCost] = []
    edges: list[EdgeAction] = []
    inbound_meta: dict[str, object] = {}
    cursor = pos + 1
    # ONE loop. Before the node's first OUTGOING edge, three token classes
    # interleave in ANY order: create FIELDS, COST deltas, and the UNAMBIGUOUS
    # producer-edge metadata ``note``/``valence``. Order is free among them
    # because none collide: ``note``/``valence`` are edge-only words, never row
    # fields. The COLLIDING metadata words -- ``label``/``labels``/``priority``,
    # which name BOTH a row field and an edge annotation -- are deliberately NOT
    # interleaved here: inside a create body they are ROW fields (the natural
    # meaning when minting a row), so they fall through to the field branch. Their
    # edge-annotation meaning is reachable only AFTER an explicit edge ref (where
    # ``_parse_edge_action`` -> ``edge_metadata`` binds them to that edge). The
    # field branch therefore precedes the metadata branch, and the metadata branch
    # admits only the safe set. The first child edge closes the node: after it,
    # metadata is that child's (hoisted by ``_parse_edge_action``) and a stray
    # field has no home (raises, never silently rebinds to the caller).
    while cursor < len(args):
        word = args[cursor].lower()
        if grouped and word == "end":
            break  # the parent's `end` pops this group; leave it for the caller.
        if word in COST_FIELDS:
            action, consumed = _parse_cost_action(args, cursor, word)
            if not isinstance(action, AddCost):
                raise ClientError(f"inline create cost {word!r} must use add VALUE")
            costs.append(action)
            cursor += consumed
            continue
        if word in (*EDITABLE_FIELDS, *LIST_FIELDS):
            # Row field -- including the collision words label/labels/priority,
            # which mean "row field" in a create body. Checked BEFORE the metadata
            # branch so those words are never mis-read as edge metadata here.
            if edges:
                # A field after this node's first outgoing edge: the node is
                # closed (an edge descended past it), so the field has no home.
                # Raise rather than silently rebinding it to the caller's anchor
                # (the corruption the old two-phase parser allowed).
                raise ClientError(
                    f"inline create field {word!r} appears after an edge; a node's "
                    f"fields must precede its outgoing edges -- move {word!r} before "
                    f"the edge"
                )
            cursor = _consume_inline_field(args, cursor, fields)
            continue
        if (word in _SAFE_INLINE_META_FIELDS or word == "edge") and not edges:
            # Producer-edge metadata on THIS node (the deepest edge so far),
            # before its first child edge. Bare ``note``/``valence`` (edge-only)
            # interleave with fields freely; the ``edge`` marker reaches the
            # collision words (``edge priority``/``edge label``) so their
            # edge-annotation meaning is expressible without colliding with the
            # row-field reading the field branch above already took.
            captured, consumed = edge_metadata(args[cursor:])
            inbound_meta.update(captured)
            cursor += consumed
            continue
        edge = EDGE_ALIASES.get(word)
        if edge is None:
            break  # neither field, cost, metadata, nor edge: rebinds to the caller.
        action, consumed = _parse_edge_action(args, cursor, edge)
        if not isinstance(action, EdgeAction):  # pragma: no cover -- always EdgeAction
            raise ClientError("inline-create tail must be an edge")
        edges.append(action)
        cursor += consumed

    if not fields:
        raise ClientError(
            f"inline create for {args[pos]!r} requires at least one field"
        )
    return InlineCreate(
        kind=kind,
        fields=tuple(fields),
        edges=tuple(edges),
        costs=tuple(costs),
        inbound_meta=inbound_meta,
    ), cursor - pos


def _consume_inline_field(
    args: Sequence[str], cursor: int, fields: list[SetField]
) -> int:
    """Consume one inline-create field at ``cursor``; return the new cursor.

    Mutates ``fields`` in place. A scalar takes ``to VALUE`` once; a list field
    seeds with ``to`` and extends with ``add`` (an ordered byline); a ref-list
    field consumes a typed ``kind seq`` ref. A scalar re-set, or a list re-seeded
    with ``to`` after it holds values, is the author clobbering their own input
    and raises.
    """
    field = args[cursor].lower()
    is_list_field = field in LIST_FIELDS
    op = token(args, cursor + 1)
    op_lower = op.lower() if op is not None else None
    valid_ops = ("to", "add") if is_list_field else ("to",)
    if op_lower not in valid_ops:
        verbs = " or ".join(f"'{verb} VALUE'" for verb in valid_ops)
        raise ClientError(f"inline create field {field!r} must be followed by {verbs}")
    parsed_field = list_payload_field(field) if is_list_field else field
    existing = next((f for f in fields if f.field == parsed_field), None)
    if existing is not None and not (is_list_field and op_lower == "add"):
        kind_word = "list" if is_list_field else "scalar"
        raise ClientError(f"{kind_word} field {parsed_field!r} set more than once")
    spec = FIELDS_BY_NAME.get(field)
    if spec is not None and spec.ref_kind is not None:
        # Ref-list field: consume a typed `kind seq` ref (1-2 tokens), mirroring
        # the top-level `to` path (trax #419).
        ref, consumed = consume_ref(args, cursor + 2, kind_hint=spec.ref_kind)
        fields[:] = _append_inline_list_value(fields, parsed_field, ref, existing)
        return cursor + 2 + consumed
    value = required_token(
        args, cursor + 2, f"expected value for inline create field {field}"
    )
    if is_list_field:
        fields[:] = _append_inline_list_value(fields, parsed_field, value, existing)
    else:
        fields.append(SetField(field=parsed_field, value=field_value(field, value)))
    return cursor + 3


def _append_inline_list_value(
    fields: list[SetField],
    parsed_field: str,
    value: object,
    existing: SetField | None,
) -> list[SetField]:
    """Append one value to an inline-create list field's ordered tuple.

    A list field is stored as a single :class:`SetField` whose ``value`` is the
    byline tuple; seeding with ``to`` creates it and each ``add`` extends it. The
    frozen ``SetField`` is replaced in place to preserve declaration order.
    """
    if existing is None:
        fields.append(SetField(field=parsed_field, value=(value,)))
        return fields
    extended = (*cast(tuple[object, ...], existing.value), value)
    return [
        SetField(field=parsed_field, value=extended) if f is existing else f
        for f in fields
    ]
