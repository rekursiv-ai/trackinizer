"""CLI grammar: the vocabulary and aliases at the ``trax`` boundary.

Holds the token vocabulary, ergonomic aliases, parsed-action dataclasses,
field metadata, edge/relation alias tables, priority-band aliases, and the
helpers that coerce raw user input into typed wire values.

``types/inquiries.py`` and ``types/edges.py`` are the source of truth for
column names. Every alias here exists for CLI ergonomics and is translated
to its canonical SQL column before the ``Filter`` crosses the wire; the
server never sees a CLI alias.

Changing a table here means updating ``GRAMMAR.md`` in the same commit;
``grammar_test.py`` enforces that they stay in sync.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import (
    dataclass,
    field as dataclass_field,
)
from typing import Final, Literal, TypeGuard, cast, get_args

import json
import re
import uuid

from trackinizer.client.errors import ClientError
from trackinizer.types.columns import flat_column_specs
from trackinizer.types.inquiries import (
    CITATION_VALENCE_DEFAULT,
    KIND_TO_CLASS,
    Belief,
    Inquiry,
    Issue,
    Paper,
)
from trackinizer.wire.filters import Filter
from trackinizer.wire.refs import Ref, SeqRef, UuidRef
from trackinizer.wire.seq_ranges import SeqRange


__all__ = [
    "COST_FIELDS",
    "EDGE_ALIASES",
    "EDITABLE_FIELDS",
    "FIELDS_BY_NAME",
    "FILTER_FIELDS_CLI",
    "ISSUE_KINDS",
    "KIND_LOWER",
    "LIST_FIELDS",
    "PRIORITY_ALIASES",
    "REF_FIELD_BY_PAYLOAD",
    "RELATION_ALIASES",
    "SORT_CHOICES",
    "UUID_RE",
    "VALID_KINDS",
    "WRITE_FIELDS_CLI",
    "Action",
    "AddCost",
    "AddList",
    "BulkApply",
    "DeleteRow",
    "Edge",
    "EdgeAction",
    "EdgeTarget",
    "Field",
    "InlineCreate",
    "ListQuery",
    "MetricAction",
    "MetricMask",
    "ReadField",
    "RelationAction",
    "RemoveList",
    "SetField",
    "cost_key",
    "field_value",
    "is_issue_kind",
    "list_payload_field",
    "parse_kind",
    "parse_ref",
    "validate_writable_fields",
]


VALID_KINDS: tuple[Inquiry.InquiryKind, ...] = get_args(Inquiry.InquiryKind.__value__)
KIND_LOWER: Mapping[str, Inquiry.InquiryKind] = {k.lower(): k for k in VALID_KINDS}


@dataclass(frozen=True, kw_only=True, slots=True)
class Edge:
    """Parsed edge alias: the canonical stored kind plus how this CLI spelling
    maps onto it.

    ``reverse`` stores the edge with endpoints swapped (a ``*_by`` spelling
    addresses the same stored edge from the opposite vertex).

    ``valence_default`` / ``valence_negate`` carry the citation-polarity
    convention into the alias layer (the stored edge never sees it): a ``dis*``
    spelling (``disproves`` / ``disfavors``) resolves to the same ``proves`` /
    ``favors`` kind with ``valence_negate=True`` and a negative default, so a
    user-given positive valence is negated and an omitted one defaults to
    ``-0.5``. A plain ``proves`` / ``favors`` defaults to ``+0.5``.
    """

    name: str
    reverse: bool = False
    valence_default: float | None = None
    valence_negate: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class ReadField:
    """Parsed field projection."""

    field: str


@dataclass(frozen=True, kw_only=True, slots=True)
class SetField:
    """Parsed scalar or replacement field mutation.

    ``field`` is the ``payload_key`` (wire name) for a list ``... to`` replace
    and the ``cli_name`` for a scalar set; ``AddList``/``RemoveList`` always
    store the ``cli_name``. For a ref-list field (``ref_kind`` set), ``value``
    is the tuple of parsed :class:`Ref`s to install; the verb layer resolves
    each to its wire shape. For every other field ``value`` is the coerced
    scalar/string.
    """

    field: str
    value: object


@dataclass(frozen=True, kw_only=True, slots=True)
class AddList:
    """Parsed list append mutation.

    ``ref`` carries the parsed reference for a ref-list field; ``value`` is its
    CLI spelling, used only for echo. Plain list fields leave ``ref`` ``None``.
    """

    field: str
    value: str
    ref: Ref | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class RemoveList:
    """Parsed list remove mutation.

    ``ref`` carries the parsed reference for a ref-list field; ``value`` is its
    CLI spelling, used only for echo. Plain list fields leave ``ref`` ``None``.
    """

    field: str
    value: str
    ref: Ref | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class AddCost:
    """Parsed signed cost delta."""

    field: str
    value: float


@dataclass(frozen=True, kw_only=True, slots=True)
class RelationAction:
    """Parsed relation view action.

    ``against`` is set when the spelling was an against-citation (``dis*``):
    the stored kind is the same proves/favors, so the view filters to the
    negative-valence rows -- for-vs-against is the sign, not a separate kind.
    """

    relation: tuple[str, bool]
    index: str = ""
    against: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class MetricMask:
    """One ``at <field> <op> <value>`` clause on the metric grid.

    The metric grid is an array ``(key, step) -> value``; a mask clause is one
    trax filter triple whose ``field`` is a grid axis (``key`` / ``step`` /
    ``value``) rather than an ``inquiries`` column. Several clauses AND together
    (numpy boolean-mask indexing spelled in words). ``op`` is an existing
    :class:`~wire.filters.FilterOp` (``is`` / ``ne`` / ``lt`` / ``le`` / ``gt``
    / ``ge``) or a step-axis reduction (``max`` / ``min``).

    The bareword shorthand ``at loss`` parses to ``MetricMask(field="key",
    op="is", value="loss")``.
    """

    field: Literal["key", "step", "value"]
    op: str
    value: str


@dataclass(frozen=True, kw_only=True, slots=True)
class MetricAction:
    """A parsed ``metric`` tail: masks plus one operation over the grid.

    ``masks`` are the AND-ed mask clauses (possibly empty -- a bare ``metric``
    reads the whole grid). ``write`` is the ``to <value>`` target when the tail
    assigns to the masked selection; ``None`` makes it a read. ``sort`` /
    ``limit`` order and window a read (never a write).
    """

    masks: tuple[MetricMask, ...]
    write: str | None = None
    sort: Literal["asc", "desc"] | None = None
    limit: int | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class InlineCreate:
    """An anonymous create embedded as an ``EdgeAction`` target.

    ``edges`` carries this node's OWN outgoing edges (Issue#425 item 6): the
    DEEP cursor descends into each inline-create, so an edge written after the
    create's fields binds to IT, not the leading subject. Bare juxtaposition
    descends; a ``begin ... end`` group scopes a subtree and pops on ``end`` to
    let the parent fan out. ``edges`` is empty for a plain leaf.

    ``costs`` carries the node's own ``agent-cost`` / ``resource-cost`` deltas,
    written after its fields and interleaved with its ``edges``. Cost columns are
    flattened on the wire, so the delta cannot ride the create body; the runner
    applies each to THIS node after the atomic create lands.

    ``inbound_meta`` carries metadata (note/priority/valence/labels) written
    beside this node's fields, before any of its own edges. It describes the edge
    that PRODUCED this node -- the deepest edge so far -- so the parser hoists it
    onto that edge. This is what lets a verdict note sit beside the node it
    annotates (``produced websearch <fields> note to "<verdict>"``) instead of
    after the whole subtree, so a chain needs no ``begin ... end`` for a note.
    """

    kind: Inquiry.InquiryKind
    fields: tuple[SetField, ...]
    edges: tuple[EdgeAction, ...] = ()
    costs: tuple[AddCost, ...] = ()
    inbound_meta: Mapping[str, object] = dataclass_field(
        default_factory=lambda: cast(dict[str, object], {})
    )


EdgeTarget = Ref | InlineCreate


@dataclass(frozen=True, kw_only=True, slots=True)
class EdgeAction:
    """Parsed edge mutation or selection action."""

    edge: Edge
    target: EdgeTarget
    metadata: Mapping[str, object]
    remove: bool = False
    annotate: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class DeleteRow:
    """Parsed row deletion action."""


@dataclass(frozen=True, kw_only=True, slots=True)
class ListQuery:
    """Parsed multi-kind list query.

    A kind's ``ranges`` value is a tuple because one selector token may
    carry comma-separated disjoint intervals (``222..260,279..``); the
    union of the tuple's intervals is the kind's selected seq set.
    """

    kinds: tuple[Inquiry.InquiryKind, ...]
    ranges: Mapping[Inquiry.InquiryKind, tuple[SeqRange, ...]]
    filters: tuple[Filter, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class BulkApply:
    """A list query plus the field mutations to apply to every matched row.

    Triggered whenever a row command carries at least one range or filter
    alongside ``field to value`` mutations: the range/filter half selects the
    rows, the mutation half applies to each. ``field OP value`` triples
    discriminate by operator -- ``to`` is a mutation, a filter op is a
    predicate -- so no separator keyword is needed.
    """

    query: ListQuery
    actions: tuple[SetField | AddList | RemoveList, ...]

    def __post_init__(self) -> None:
        """Reject an empty mutation list; a bulk apply must change something."""
        if not self.actions:
            raise ValueError("BulkApply requires at least one mutation")


Action = (
    ReadField
    | SetField
    | AddList
    | RemoveList
    | AddCost
    | RelationAction
    | EdgeAction
    | DeleteRow
)

EDGE_ALIASES: Mapping[str, Edge] = {
    # Every edge is stored child -> parent (from = the younger/dependent vertex).
    # A CLI spelling names the same stored edge from either vertex; the ``*_by``
    # / opposite-voice spelling sets ``reverse`` to swap which endpoint is the
    # subject. See ``types/edges.py``.
    #
    # narrows: stored narrower(child) -> broader(parent).
    #   child narrows parent     / parent narrowed_by child   (child is from)
    #   parent broadens child     / child broadened_by parent  (parent is from)
    "narrows": Edge(name="narrows"),
    "broadened_by": Edge(name="narrows"),
    "narrowed_by": Edge(name="narrows", reverse=True),
    "broadens": Edge(name="narrows", reverse=True),
    # requires: stored requirer(child) -> prerequisite(parent). ``A requires B``
    # means B must be done first. ``blocks`` is the parent's voice (B blocks A).
    "requires": Edge(name="requires"),
    "blocked_by": Edge(name="requires"),
    "required_by": Edge(name="requires", reverse=True),
    "blocks": Edge(name="requires", reverse=True),
    # produced_by: stored produced(child) -> producer(parent). ``produces`` is
    # the producer's voice (parent -> child), so it reverses; ``produced`` is the
    # same producer-voice past tense.
    "produced_by": Edge(name="produced_by"),
    "produces": Edge(name="produced_by", reverse=True),
    "produced": Edge(name="produced_by", reverse=True),
    # proves/favors: stored evidence(child Artifact) -> claim(parent). The
    # ``dis*`` spellings are the SAME kind with a negated valence (the polarity
    # lives in the valence sign, not a separate edge kind).
    "proves": Edge(name="proves", valence_default=CITATION_VALENCE_DEFAULT),
    "proved_by": Edge(
        name="proves", reverse=True, valence_default=CITATION_VALENCE_DEFAULT
    ),
    "disproves": Edge(
        name="proves", valence_default=-CITATION_VALENCE_DEFAULT, valence_negate=True
    ),
    "disproved_by": Edge(
        name="proves",
        reverse=True,
        valence_default=-CITATION_VALENCE_DEFAULT,
        valence_negate=True,
    ),
    "favors": Edge(name="favors", valence_default=CITATION_VALENCE_DEFAULT),
    "favored_by": Edge(
        name="favors", reverse=True, valence_default=CITATION_VALENCE_DEFAULT
    ),
    "disfavors": Edge(
        name="favors", valence_default=-CITATION_VALENCE_DEFAULT, valence_negate=True
    ),
    "disfavored_by": Edge(
        name="favors",
        reverse=True,
        valence_default=-CITATION_VALENCE_DEFAULT,
        valence_negate=True,
    ),
    # supersedes: stored successor(child) -> predecessor(parent).
    "supersedes": Edge(name="supersedes"),
    "superseded_by": Edge(name="supersedes", reverse=True),
    # cites_paper: stored citing(child) -> cited(parent), Paper -> Paper. A
    # historical/bibliographic citation, not epistemic: it carries no valence,
    # so no dis* spelling and no valence_default. ``A cites B`` = A's bibliography
    # lists B; ``A cited_by B`` = B cites A (same stored edge, reversed).
    "cites": Edge(name="cites_paper"),
    "cited_by": Edge(name="cites_paper", reverse=True),
}
ISSUE_KINDS: tuple[Issue.Kind, ...] = get_args(Issue.Kind.__value__)
PRIORITY_ALIASES: Final[Mapping[str, int]] = {
    "critical": 0,
    "high": 10,
    "medium": 20,
    "low": 30,
    "backlog": 40,
}
SORT_CHOICES: Final[tuple[str, ...]] = (
    "priority",
    "seq",
    "recent",
    "oldest",
    "valence",
)
# Read-side relation views: spelling -> (stored edge kind, inbound?). The
# ``inbound`` flag reads the vertex's inbound (from-side) edges; a forward
# spelling reads its outbound (to-side) edges. ``dis*`` spellings share the
# stored kind with their plain forms (polarity is the valence sign).
RELATION_ALIASES: Final[Mapping[str, tuple[str, bool]]] = {
    "narrows": ("narrows", False),
    "broadened_by": ("narrows", False),
    "narrowed_by": ("narrows", True),
    "broadens": ("narrows", True),
    "requires": ("requires", False),
    "blocked_by": ("requires", False),
    "required_by": ("requires", True),
    "blocks": ("requires", True),
    "produced_by": ("produced_by", False),
    "produces": ("produced_by", True),
    "produced": ("produced_by", True),
    "proves": ("proves", False),
    "disproves": ("proves", False),
    "proved_by": ("proves", True),
    "disproved_by": ("proves", True),
    "favors": ("favors", False),
    "disfavors": ("favors", False),
    "favored_by": ("favors", True),
    "disfavored_by": ("favors", True),
    "supersedes": ("supersedes", False),
    "superseded_by": ("supersedes", True),
    "cites": ("cites_paper", False),
    "cited_by": ("cites_paper", True),
}

# Relation spellings that select the AGAINST (negative-valence) subset of their
# shared stored kind. ``trax belief 5 disproves`` lists only the proves edges
# with valence < 0, where ``trax belief 5 proves`` lists them all.
AGAINST_RELATION_SPELLINGS: frozenset[str] = frozenset(
    {"disproves", "disproved_by", "disfavors", "disfavored_by"}
)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Field:
    """Everything about one CLI field: spelling, column, coercion, help.

    Adding a field is one new ``Field`` entry; the rest of the system
    picks it up automatically.

    Attributes:
      cli_name: Token as the user types it.
      payload_key: The wire field name used in create/edit bodies -- the
        bare field name, since the HTTP body is kind-scoped (the kind is
        in the route). The server maps it to the flat storage column. The
        parser translates ``cli_name`` to this before any wire send.
        (Filter fields take a separate path through
        ``canonical_filter_field``, which resolves to the storage column.)
      shape: How the field accepts values.
      help: One-line description for the help renderer. Empty for aliases
        (e.g. ``labels`` mirrors ``label``).
      filterable: Whether the scalar/list field has defined filter semantics.
      ref_kind: For a ref-list field, the default kind a bare ``seq`` resolves
        to; ``None`` for a plain list field whose values are free strings. A
        ref-list field is monomorphic -- the server stores a bare ``id`` (the
        sole such field is ``codechanges`` -> CodeChange).

    """

    cli_name: str
    payload_key: str
    shape: Literal["scalar", "list", "cost"]
    help: str = ""
    list_add: str = ""
    list_remove: str = ""
    filterable: bool = True
    ref_kind: Inquiry.InquiryKind | None = None

    def coerce(self, value: str) -> object:
        """Coerce a string token to its wire value."""
        return _COERCE.get(self.cli_name, _coerce_identity)(value)


def _coerce_identity(value: str) -> object:
    return value


def _coerce_priority(value: str) -> int:
    alias = value.strip().lower()
    if alias in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[alias]
    try:
        return int(alias)
    except ValueError as err:
        raise ValueError("priority must be an int") from err


def _coerce_confidence(value: str) -> float:
    try:
        return float(value)
    except ValueError as err:
        # A clean message (mirrors priority's "must be an int"); ``field_value``
        # wraps any ValueError into a ClientError, but the raw float() text
        # ("could not convert string to float") is opaque to a CLI user.
        raise ValueError("confidence must be a number") from err


def _coerce_status(value: str) -> str:
    if value not in get_args(Inquiry.Status.__value__):
        raise ValueError(f"unknown status {value!r}")
    return value


def _coerce_judgement(value: str) -> str:
    judgements = get_args(Belief.Judgement.__value__)
    if value not in judgements:
        raise ValueError(f"unknown judgement {value!r}")
    return value


def _coerce_publication_type(value: str) -> str:
    publication_types = get_args(Paper.PublicationType.__value__)
    if value not in publication_types:
        raise ValueError(f"unknown publication_type {value!r}")
    return value


def _coerce_config(value: str) -> object:
    """Parse a ``config`` token to its JSON-object wire value.

    The ``-`` (stdin) and ``@path`` (file) sentinels pass through untouched:
    they are resolved at the verb layer, which re-coerces the read text
    through :func:`field_value`.
    """
    if value == "-" or value.startswith("@"):
        return value
    try:
        parsed: object = json.loads(
            value,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except ValueError as err:
        raise ValueError(f"config must be valid JSON: {err}") from err
    match parsed:
        case dict():
            config = cast(dict[str, object], parsed)
        case _:
            raise ValueError("config must be a JSON object")
    try:
        json.dumps(config, allow_nan=False)
    except ValueError as err:
        raise ValueError("config numbers must be finite") from err
    return config


def _reject_nonstandard_json_constant(value: str) -> object:
    """Reject Python JSON decoder extensions absent from the JSON standard."""
    raise ValueError(f"non-standard numeric constant {value!r}")


_COERCE: Mapping[str, Callable[[str], object]] = {
    "priority": _coerce_priority,
    "confidence": _coerce_confidence,
    "status": _coerce_status,
    "judgement": _coerce_judgement,
    "publication_type": _coerce_publication_type,
    "config": _coerce_config,
}


# The one place every CLI field is declared.
_FIELDS: tuple[Field, ...] = (
    Field(
        cli_name="owner",
        payload_key="owner",
        shape="scalar",
        help="human responsible for the row",
    ),
    Field(
        cli_name="account",
        payload_key="account",
        shape="scalar",
        help="authenticated active user the row is attributed to",
    ),
    Field(cli_name="title", payload_key="title", shape="scalar", help="short title"),
    Field(
        cli_name="description",
        payload_key="description",
        shape="scalar",
        help="long-form body",
    ),
    Field(
        cli_name="status",
        payload_key="status",
        shape="scalar",
        help="active|complete|abandoned|invalid",
    ),
    Field(
        cli_name="validation",
        payload_key="validation",
        shape="scalar",
        help="success condition or verification note",
    ),
    Field(
        cli_name="priority",
        payload_key="priority",
        shape="scalar",
        help="integer or critical|high|medium|low|backlog",
    ),
    Field(
        cli_name="judgement",
        payload_key="judgement",
        shape="scalar",
        help="proven|disproven|unproven|undecidable",
    ),
    Field(
        cli_name="confidence",
        payload_key="confidence",
        shape="scalar",
        help="float in [0.0, 1.0]",
    ),
    Field(
        cli_name="outcome",
        payload_key="outcome",
        shape="scalar",
        help="result of the experiment",
    ),
    Field(
        cli_name="config",
        payload_key="config",
        shape="scalar",
        help="run settings as one JSON object (inline or @file.json)",
        filterable=False,
    ),
    Field(
        cli_name="source",
        payload_key="source",
        shape="scalar",
        help="self-describing id (arXiv:.../doi:.../http://.../https://...)",
    ),
    Field(
        cli_name="google_scholar_cluster_id",
        payload_key="google_scholar_cluster_id",
        shape="scalar",
        help="Google Scholar cluster handle (data-cid); paper identity, coexists with source",
    ),
    Field(
        cli_name="google_scholar_cites_id",
        payload_key="google_scholar_cites_id",
        shape="scalar",
        help="Google Scholar cited-by handle (cites_id); citation pivot, only if cited",
    ),
    Field(
        cli_name="abstract",
        payload_key="abstract",
        shape="scalar",
        help="paper abstract",
    ),
    Field(
        cli_name="author",
        payload_key="authors",
        shape="list",
        help="paper author (ordered byline)",
        list_add="add_author",
        list_remove="remove_author",
    ),
    Field(
        cli_name="authors",
        payload_key="authors",
        shape="list",
        list_add="add_author",
        list_remove="remove_author",
    ),
    Field(
        cli_name="publication_type",
        payload_key="publication_type",
        shape="scalar",
        help="article|inproceedings|book|thesis|techreport|misc",
    ),
    Field(
        cli_name="venue",
        payload_key="venue",
        shape="scalar",
        help="free-text series/journal name (NeurIPS, Nature, KDD)",
    ),
    Field(
        cli_name="subvenue",
        payload_key="subvenue",
        shape="scalar",
        help="track/workshop within the venue, or book title / school",
    ),
    Field(
        cli_name="publish_date",
        payload_key="publish_date",
        shape="scalar",
        help="publication date (ISO 8601)",
    ),
    Field(
        cli_name="query",
        payload_key="query",
        shape="scalar",
        help="search query string",
    ),
    Field(
        cli_name="provider",
        payload_key="provider",
        shape="scalar",
        help="search provider name",
    ),
    Field(cli_name="sha", payload_key="sha", shape="scalar", help="git commit SHA"),
    Field(cli_name="url", payload_key="url", shape="scalar", help="resource URL"),
    Field(
        cli_name="cli",
        payload_key="cli",
        shape="scalar",
        help="wrapped CLI (claude/gemini/codex/cursor)",
    ),
    Field(
        cli_name="cli_session_id",
        payload_key="cli_session_id",
        shape="scalar",
        help="the CLI's own session id (dedup/scoping key)",
    ),
    Field(
        cli_name="started",
        payload_key="started",
        shape="scalar",
        help="session start time (ISO 8601)",
    ),
    Field(
        cli_name="label",
        payload_key="labels",
        shape="list",
        help="tag for grouping/filtering",
        list_add="add_label",
        list_remove="remove_label",
    ),
    Field(
        cli_name="labels",
        payload_key="labels",
        shape="list",
        list_add="add_label",
        list_remove="remove_label",
    ),
    Field(
        cli_name="subscriber",
        payload_key="subscribers",
        shape="list",
        help="actor to notify/watch",
        list_add="add_subscriber",
        list_remove="remove_subscriber",
    ),
    Field(
        cli_name="subscribers",
        payload_key="subscribers",
        shape="list",
        list_add="add_subscriber",
        list_remove="remove_subscriber",
    ),
    Field(
        cli_name="kind",
        payload_key="issue_kind",
        shape="list",
        help="feature|bug|task|question",
        list_add="add_issue_kind",
        list_remove="remove_issue_kind",
    ),
    Field(
        cli_name="issuekind",
        payload_key="issue_kind",
        shape="list",
        list_add="add_issue_kind",
        list_remove="remove_issue_kind",
    ),
    Field(
        cli_name="issue_kind",
        payload_key="issue_kind",
        shape="list",
        list_add="add_issue_kind",
        list_remove="remove_issue_kind",
    ),
    Field(
        cli_name="codechange",
        payload_key="codechanges",
        shape="list",
        help="linked code-change row",
        list_add="add_codechange",
        list_remove="remove_codechange",
        ref_kind="CodeChange",
    ),
    Field(
        cli_name="codechanges",
        payload_key="codechanges",
        shape="list",
        list_add="add_codechange",
        list_remove="remove_codechange",
        ref_kind="CodeChange",
    ),
    Field(
        cli_name="agent-cost",
        payload_key="marginal_cost_agent_usd",
        shape="cost",
        help="agent/tool/model spend",
    ),
    Field(
        cli_name="resource-cost",
        payload_key="marginal_cost_resource_usd",
        shape="cost",
        help="compute/storage/API spend",
    ),
)


FIELDS_BY_NAME: Mapping[str, Field] = {f.cli_name: f for f in _FIELDS}


def _ref_fields_by_payload() -> Mapping[str, Field]:
    """Map each ref-list ``payload_key`` to its (alias-agreeing) ``Field``."""
    by_key: dict[str, Field] = {}
    for f in _FIELDS:
        if f.ref_kind is None:
            continue
        prior = by_key.get(f.payload_key)
        # Aliases sharing a payload_key (e.g. ``codechange``/``codechanges``)
        # must agree on their ref kind so the lookup is unambiguous.
        assert prior is None or prior.ref_kind == f.ref_kind, (
            f"ref-list aliases disagree on wire shape for {f.payload_key!r}"
        )
        by_key.setdefault(f.payload_key, f)
    return by_key


REF_FIELD_BY_PAYLOAD: Mapping[str, Field] = _ref_fields_by_payload()


EDITABLE_FIELDS: tuple[str, ...] = tuple(
    f.cli_name for f in _FIELDS if f.shape == "scalar"
)
LIST_FIELDS: tuple[str, ...] = tuple(f.cli_name for f in _FIELDS if f.shape == "list")
COST_FIELDS: tuple[str, ...] = tuple(f.cli_name for f in _FIELDS if f.shape == "cost")
# Identity columns the schema declares directly (no ColumnSpec), so they
# don't appear in ``flat_column_specs`` but are still filterable. Mirrors
# ``server/api/query.py::_IDENTITY_COLUMNS`` minus ``kind`` (the row
# discriminator; the CLI ``kind`` alias routes to ``issue_kind`` instead).
_IDENTITY_FILTER_COLUMNS: frozenset[str] = frozenset(
    {"seq", "id", "created", "modified"}
)


def _filterable_columns(kind: Inquiry.InquiryKind) -> frozenset[str]:
    """Canonical SQL columns a filter may target for ``kind``.

    Derived from the column specs honoring ``applies_to_inquiry_kinds``,
    matching the server's rule in ``query.py::_filter_columns_for`` so the
    CLI doesn't over-reject base columns (labels, cost) the server accepts.
    """
    cls = KIND_TO_CLASS[kind]
    columns = set(_IDENTITY_FILTER_COLUMNS)
    for column, flat in flat_column_specs(cls).items():
        applies = flat.spec.applies_to_inquiry_kinds
        if applies is None or kind in applies:
            columns.add(column)
    return frozenset(columns)


def _filter_fields_cli(kind: Inquiry.InquiryKind) -> tuple[str, ...]:
    """Every CLI filter name (canonical column plus aliases) for ``kind``."""
    canonical = _filterable_columns(kind)
    non_filterable = {spec.payload_key for spec in _FIELDS if not spec.filterable}
    # The composite ``marginal_cost`` is never a filter target; only its
    # flattened axes appear in ``flat_column_specs``, reached via cost aliases.
    names = {
        col for col in canonical if col != "marginal_cost" and col not in non_filterable
    }
    for spec in _FIELDS:
        if spec.filterable and spec.payload_key in canonical:
            names.add(spec.cli_name)
    return tuple(sorted(names))


# Per-kind CLI filter-field whitelist, derived from the column specs so it
# can't drift from the server's. A hand-listed table once omitted labels and
# cost on kinds the server accepts; deriving it removes that whole class of bug.
FILTER_FIELDS_CLI: Mapping[Inquiry.InquiryKind, tuple[str, ...]] = {
    kind: _filter_fields_cli(kind) for kind in KIND_TO_CLASS
}


def _writable_fields_cli(kind: Inquiry.InquiryKind) -> frozenset[str]:
    """Every CLI write-field name (scalar/list/cost) editable on ``kind``.

    Derived from the same ``applies_to_inquiry_kinds`` specs the server
    gates on, so the CLI can reject a kind-invalid field before sending any
    request. A scalar/list field is writable when its storage column
    applies to ``kind``; cost fields are base columns valid on every kind.
    """
    cls = KIND_TO_CLASS[kind]
    flat = flat_column_specs(cls)
    names: set[str] = set()
    for spec in _FIELDS:
        if spec.shape == "cost":
            names.add(spec.cli_name)
            continue
        column = flat.get(spec.payload_key)
        if column is None:
            continue
        applies = column.spec.applies_to_inquiry_kinds
        if applies is None or kind in applies:
            names.add(spec.cli_name)
    return frozenset(names)


# Per-kind CLI write-field whitelist. The write path applies one field per
# request in its own transaction, so a later kind-invalid field used to 409
# only after earlier valid fields had already committed (non-atomic). The CLI
# validates the whole field set against this table up front and rejects before
# any request, restoring all-or-nothing semantics without a server round-trip.
WRITE_FIELDS_CLI: Mapping[Inquiry.InquiryKind, frozenset[str]] = {
    kind: _writable_fields_cli(kind) for kind in KIND_TO_CLASS
}


def validate_writable_fields(
    kind: Inquiry.InquiryKind, fields: tuple[str, ...]
) -> None:
    """Reject any CLI write field not valid on ``kind`` before any request.

    Args:
      kind: Inquiry kind the row-local mutations target.
      fields: CLI field names from the parsed write actions, in order.

    Raises:
      ClientError: Names the first field invalid on ``kind`` so a
        multi-field command fails atomically instead of partially applying.

    """
    allowed = WRITE_FIELDS_CLI[kind]
    for field in fields:
        if field not in allowed:
            raise ClientError(f"field {field!r} is not valid on {kind}")


def is_issue_kind(token: str) -> TypeGuard[Issue.Kind]:
    """Whether ``token`` is an issue-kind literal."""
    return token in ISSUE_KINDS


_KIND_HASH_RE = re.compile(r"^([A-Za-z]+)#(\d+)$")


def parse_ref(value: str) -> Ref:
    """Parse a UUID or ``Kind#seq`` reference; raise ``ValueError`` otherwise."""
    value = value.strip()
    if not value:
        raise ValueError("empty reference")
    if UUID_RE.match(value):
        return UuidRef(uuid=uuid.UUID(value))
    if (match := _KIND_HASH_RE.match(value)) is not None:
        name, seq = match.group(1).lower(), int(match.group(2))
        if name not in KIND_LOWER:
            raise ValueError(f"unknown kind {name!r} in reference {value!r}")
        return SeqRef(kind=KIND_LOWER[name], seq=seq)
    raise ValueError(f"cannot parse reference {value!r}")


def parse_kind(value: str) -> Inquiry.InquiryKind:
    """Parse a kind name, case-insensitively and accepting a trailing ``s``."""
    name = value.strip().lower()
    if name in KIND_LOWER:
        return KIND_LOWER[name]
    if name.endswith("s") and name[:-1] in KIND_LOWER:
        return KIND_LOWER[name[:-1]]
    raise ValueError(f"unknown kind {value!r}")


def cost_key(field: str) -> str:
    """Canonical SQL column for a CLI cost field."""
    spec = FIELDS_BY_NAME.get(field)
    if spec is None or spec.shape != "cost":
        raise ClientError(f"unknown cost field {field!r}")
    return spec.payload_key


def field_value(field: str, value: str) -> object:
    """Coerce one scalar field token to its wire value, mapping errors to ClientError."""
    spec = FIELDS_BY_NAME.get(field)
    if spec is None:
        return value
    try:
        return spec.coerce(value)
    except ValueError as err:
        raise ClientError(str(err)) from err


def list_payload_field(field: str) -> str:
    """Canonical SQL column for a CLI list field."""
    spec = FIELDS_BY_NAME.get(field)
    return field if spec is None else spec.payload_key
