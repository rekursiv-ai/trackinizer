"""The Edge dataclass and the per-kind behavior registry.

The edges table is the SQL source of truth for every Inquiry-to-Inquiry
relationship where order does not matter. Each :data:`Edge.Kind` shows up
as a read-only projection on the relevant Inquiry subclass, but every
mutation goes through ``edges``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Self, get_args
from uuid import UUID

from trackinizer.types.columns import ColumnSpec, Row
from trackinizer.types.inquiries import Artifact, Inquiry, Issue


@dataclass(frozen=True, slots=True, kw_only=True)
class Edge:
    """One directed relationship, as stored in the edges table.

    THE EPISTEMOLOGY. Every edge points from a child UP to its parent -- the
    stored direction is always ``from = the younger/dependent vertex, to = its
    older parent`` -- so a vertex stores exactly the edges toward its own
    parents. Each :data:`Edge.Kind` is named from that child's view::

      Invariant:  Parents are always OLDER.
      Convention: Top are _fundamental_ edge names.

          {requires,narrows}     {produced_by,supersedes}     {proves,favors}
                  ▲                         ▲                        ▲
                  │                         │                        │
                Issue ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄▷ Inquiry ◁┄┄┄┄┄┄┄┄┄ {Belief,Experiment}
                  │                         │                        │
                  ▼                         ▼                        ▼
      {required_by,narrowed_by}  {produces,superseded_by}  {proved_by,favored_by}

      Invariant:  Children are always NEWER
      Convention: Bottom are _projected_ edge names.
      Dashed hollow arrows are IS-A; solid arrows are relationships.

      time := CreationTime xor CompletionTime (uniquely for `requires`)

      Edge types come in pairs: "hard" and "soft" aka "logical" and "organizational".

    Invariants:

    * "Parents" are always "older" than "children", on each edge's own clock:
      creation-time for every edge except ``requires``, which is completion-time
      (do-time). ``A requires B`` means B must be done first, so B is do-time
      older.
    * Any Inquiry can be ``produced_by`` one or more older Inquiries (its
      origins); the parent's inverse view is ``produces``. Provenance is the one
      relation with no natural child-as-subject active verb in English, so it
      keeps the passive ``produced_by`` as the child's stored name.
    * Any Inquiry can be ``superseded_by`` one or more others (M:N
      knowledge-surgery: replace / coarsen / split / merge).
    * An Issue ``narrows`` a broader Issue (decomposition) or ``requires`` a
      prerequisite Issue (sequencing) -- both Issue -> Issue.
    * Citations (``proves`` / ``favors``) store ``Artifact -> {Belief,
      Experiment}``: the citing evidence (the younger child, gathered to bear on
      a claim) points up to the older claim. ``proves`` is load-bearing (votes
      in the proof predicate); ``favors`` is context (informs without voting).
      For-vs-against is NOT a separate edge kind; it is the sign of
      :attr:`valence` (``[-1, 1]``; magnitude is the evidential weight, 0
      neutral, default 0.5). The forward view (``proves`` / ``favors``, the
      claims an artifact cites) lives on the base :class:`Artifact`; the passive
      inverse (``proved_by`` / ``favored_by``, the artifacts citing a claim)
      lives on :class:`Belief` and :class:`Experiment`.
    * A ``cites_paper`` edge stores ``Paper -> Paper`` (the citing/younger paper
      points up to the cited/older one): a HISTORICAL, bibliographic citation,
      deliberately distinct from the epistemic ``proves`` / ``favors``. It
      carries NO :attr:`valence` (it is not our judgement -- it records that one
      external source cites another, not that we weigh it for or against a
      claim) and is provenance-NEUTRAL (omitted from both
      :data:`PRODUCED_INFERENCE_PRECEDENCE` and
      :data:`PRODUCED_INFERENCE_SUPPRESSED`, so a lone citation infers no
      ``produced_by`` yet never vetoes a coexisting epistemic edge's
      inference). ``Paper -> Paper`` only, because in-house artifacts
      (Belief/Experiment/CodeChange) are our own epistemology; historical
      citation is a fact between external sources. The forward view is
      :attr:`~..inquiries.Paper.cites`; the inverse is
      :attr:`~..inquiries.Paper.cited_by`.

    Each :data:`Edge.Kind` is mirrored by a projection field on the relevant
    Inquiry subclass (a tuple of :class:`~..inquiries.InquiryEdge`), filled by
    ``Store.get_X(id)`` from this table. Those fields are in-memory views; the
    edge row is the real storage, and every mutation goes through it::

        narrows      -> Issue.narrows / Issue.narrowed_by
        requires     -> Issue.requires / Issue.required_by
        produced_by  -> Inquiry.produced_by / Inquiry.produces
        supersedes   -> Inquiry.supersedes / Inquiry.superseded_by
        proves       -> Artifact.proves / {Belief,Experiment}.proved_by
        favors       -> Artifact.favors / {Belief,Experiment}.favored_by
        cites_paper  -> Paper.cites / Paper.cited_by

    A few relationships are kept as row-level arrays on ``inquiries`` instead of
    edges, because they are inherently ordered, mostly appended to, and read in
    bulk. Postgres ``UUID[]`` / ``TEXT[]`` columns preserve order for free, which
    the edge model would need an ``ORDER BY`` to match:

    - :attr:`Inquiry.subscribers`     (agent ids, not Inquiries)
    - :attr:`Experiment.codechanges`  (code states in chronological order)

    ``from_kind`` and ``to_kind`` are denormalized copies of the endpoint rows'
    discriminators, so the schema ``CHECK`` can validate an
    ``(edge_kind, from_kind, to_kind)`` triple without a join. The annotation
    fields hold what the relationship means in context: nodes say what a thing
    is, edges say what it means here.
    """

    type Kind = Literal[
        "narrows",
        "requires",
        "produced_by",
        "proves",
        "favors",
        "supersedes",
        "cites_paper",
    ]
    """Closed set, mirrored in the schema CHECK constraint. Every kind names the
    edge from the child's view: the stored edge points child -> parent (from =
    the younger/dependent vertex, to = its older parent)."""

    from_id: UUID
    """The inquiry the edge starts at."""

    from_kind: Inquiry.InquiryKind
    """Kind of ``from_id``'s row."""

    to_id: UUID
    """The inquiry the edge points at."""

    to_kind: Inquiry.InquiryKind
    """Kind of ``to_id``'s row."""

    edge_kind: Kind
    """Which relationship this is; see :data:`Edge.Kind`."""

    priority: Issue.Priority | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_edge_kinds=frozenset({"narrows", "requires"}),
            sql_type="INTEGER",
            sql_check="priority >= 0",
        ),
    )
    """Contextual priority on an Issue-to-Issue edge.

    Lower is higher priority. ``None`` means the edge adds no override, so
    the endpoint rows' own priorities apply.
    """

    note: str | None = field(
        default=None,
        metadata=ColumnSpec(
            sql_type="TEXT",
        ),
    )
    """Free text saying what ``to_id`` means in this relationship.

    The live ``edges`` row stores NULL for an absent note. On the audit
    (``change_log``) mirror, a NULL is coerced to ``""`` whenever the edge's
    ``peer_id`` is set, to satisfy the edge-peer presence CHECK -- so a
    ``change_log`` ``edge_note = ""`` means "no note", not an empty-string note.
    """

    valence: float | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_edge_kinds=frozenset({"proves", "favors"}),
            sql_type="DOUBLE PRECISION",
            sql_check="valence >= -1 AND valence <= 1",
        ),
    )
    """The signed strength ``to_id`` carries here, in ``[-1, 1]``, or ``None``.

    Carried only by ``proves`` / ``favors`` citations; ``None`` on every
    structural edge (the column is NULL there). On a citation the sign is the
    polarity (positive supports the claim, negative argues against it) and the
    magnitude is the evidential weight. ``0`` is neutral; a citation written
    without an explicit value takes :data:`CITATION_VALENCE_DEFAULT` (``0.5``,
    mild support), so a citation is never NULL while a structural edge always
    is."""

    labels: tuple[str, ...] | None = field(
        default=None,
        metadata=ColumnSpec(
            sql_type="TEXT[]",
            list_verb_stem="label",
        ),
    )
    """Edge-local labels for why this relationship matters here.

    The live ``edges`` row stores NULL for an empty label set. On the audit
    (``change_log``) mirror, a NULL is coerced to ``[]`` whenever the edge's
    ``peer_id`` is set (edge-peer presence CHECK), so a ``change_log``
    ``edge_labels = []`` means "no labels", not a stored empty array.
    """

    @classmethod
    def from_row(cls, row: Row) -> Self:
        """Build from a complete ``edges`` row.

        The identity columns (``from_id``, ``to_id``, and the kinds) are
        always present. The four annotation columns may or may not be
        selected, so for them a missing column reads the same as NULL.
        """
        return cls(
            from_id=row["from_id"],
            from_kind=row["from_kind"],
            to_id=row["to_id"],
            to_kind=row["to_kind"],
            edge_kind=row["edge_kind"],
            priority=row.get("priority"),
            note=row.get("note"),
            valence=row.get("valence"),
            labels=None if row.get("labels") is None else tuple(row["labels"]),
        )


# Each edge kind behaves differently across three concerns: cascade
# direction, whether it hides an endpoint from the scheduler, and whether
# it drops an endpoint from "currently true" evidence. One EdgeKindPolicy
# per kind keeps those three rules in one place instead of scattered
# conditionals across the cascade, next_issue, and proves_belief SQL.

type _EdgeEndpoint = Literal["from", "to"]
"""Which side of an edge a policy points at."""


type KindGroup = Literal[
    "issue", "inquiry", "artifact", "belief", "experiment", "claimable", "paper"
]
"""A named set of inquiry kinds an edge endpoint admits. Resolved to the
concrete kind tuple by :func:`kind_group_members`; the schema's edge-validity
CHECK and the SPA edge picker both derive their topology from the same groups,
so neither can drift from the other. ``claimable`` is the citation-target set
``{Belief, Experiment}`` -- the kinds a ``proves`` / ``favors`` edge may point
at. ``paper`` is the ``Paper``-only endpoint set both sides of a
``cites_paper`` (historical citation) edge admit."""


def kind_group_members(group: KindGroup) -> tuple[Inquiry.InquiryKind, ...]:
    """The concrete inquiry kinds a :data:`KindGroup` admits.

    The single resolver behind both the schema CHECK (``{inquiry_kinds}`` /
    ``{artifact_kinds}``) and ``/api/meta/edges``, so the edge topology has one
    source of truth instead of a hand-typed copy in the SPA.
    """
    inquiry = get_args(Inquiry.InquiryKind.__value__)
    artifact = get_args(Artifact.Kind.__value__)
    match group:
        case "issue":
            return ("Issue",)
        case "inquiry":
            return tuple(inquiry)
        case "artifact":
            return tuple(artifact)
        case "belief":
            return ("Belief",)
        case "experiment":
            return ("Experiment",)
        case "claimable":
            return ("Belief", "Experiment")
        case "paper":
            return ("Paper",)


@dataclass(frozen=True, slots=True, kw_only=True)
class EdgeKindPolicy:
    """The cross-cutting behaviors for one :data:`Edge.Kind`.

    Most kinds follow ``from = dependent, to = dependency``: the dependent's
    state derives from the dependency, so a change to the dependency alerts
    the dependent. The fields below capture where a kind departs from that.
    """

    description: str
    """One-line prose saying what this edge kind means and why its policy is set
    as it is -- the per-entry documentation that used to live as a ``#`` comment
    above each row in :data:`EDGE_POLICIES`."""

    forward_label: str
    """The relation verb read from the stored FROM-side (child) vertex: the
    outbound view, e.g. ``narrows`` / ``proves`` / ``cites``. The single source
    of the human relation name for the CLI (``trax.render._relation_title``) and
    the SPA edge picker (served via ``/api/meta/edges``)."""

    inverse_label: str
    """The relation verb read from the stored TO-side (parent) vertex: the
    inbound view, e.g. ``narrowed_by`` / ``proved_by`` / ``cited_by``. Paired
    with :attr:`forward_label`; same single-source role."""

    from_kinds: KindGroup
    """The inquiry-kind group admitted on the stored edge's ``from`` side. The
    schema edge-validity CHECK and the SPA picker derive from this."""

    to_kinds: KindGroup
    """The inquiry-kind group admitted on the stored edge's ``to`` side."""

    cascade_dependent: _EdgeEndpoint = "from"
    """Which endpoint is the DEPENDENT (the side re-assessed when the other side
    changes). This is INDEPENDENT of the stored direction: storage is always
    ``from = younger child, to = older parent`` for every kind; this field only
    says which of those two endpoints the cascade alerts. Do not read it as a
    direction flag.

    The default ``"from"`` holds for every kind whose dependent happens to be the
    stored from-side child: ``requires`` (the requirer waits on its
    prerequisite), provenance, and supersession.

    ``narrows`` and the citations (``proves``/``favors``) are ``"to"``. All are
    STILL stored child -> parent -- same uniform rule -- but the dependent is the
    ``to`` side: for ``narrows`` the BROADER issue rolls up its narrower
    children's state, so a narrower change re-assesses the broader; for
    ``proves``/``favors`` the cited CLAIM leans on its evidence, so an evidence
    change re-assesses the claim (see ``docs/design.md``: "when a cited artifact
    moves, every belief leaning on it is alerted"). Read ``"to"`` as "the parent
    is dependent ON its children"."""

    skips_scheduler_on: _EdgeEndpoint | None = None
    """When set, ``next_issue`` ignores the endpoint on this side. Used by
    ``supersedes`` to keep replaced Issues out of the scheduler."""

    invalidates_currency_on: _EdgeEndpoint | None = None
    """When set, ``proves_belief`` drops the Artifact on this side from the
    "currently true" view. Used by ``supersedes`` (the predecessor)."""

    enforces_acyclicity: bool = True
    """Whether a new edge of this kind is rejected when it would close a cycle
    within the kind's own subgraph (``Store._reject_edge_cycle``).

    ``True`` for every kind that models a graph WE own and keep a DAG:
    ``narrows``/``requires`` (Issue scheduling), ``produced_by`` (provenance),
    ``supersedes`` (knowledge surgery), ``proves``/``favors`` (citation of our
    own claims). ``False`` only for kinds that record an EXTERNAL fact we do not
    control -- ``cites_paper``, where two real papers can legitimately cite each
    other (companion papers, errata, cross-version references), so a cycle is
    data to store, not an error. The self-loop bar (``from_id <> to_id``, also a
    schema CHECK) is unconditional and independent of this flag."""


EDGE_POLICIES: Mapping[Edge.Kind, EdgeKindPolicy] = MappingProxyType(
    {
        "narrows": EdgeKindPolicy(
            description=(
                "narrows stores narrower -> broader (child -> parent), so the "
                "broader parent issue depends on its narrower children."
            ),
            forward_label="narrows",
            inverse_label="narrowed_by",
            from_kinds="issue",
            to_kinds="issue",
            cascade_dependent="to",
        ),
        "requires": EdgeKindPolicy(
            description=(
                "requires stores requirer -> prerequisite (child -> parent): A "
                "requires B means B must be done first (B is do-time older). The "
                "dependent requirer is on the from-side (default), alerted when "
                "its prerequisite moves."
            ),
            forward_label="requires",
            inverse_label="required_by",
            from_kinds="issue",
            to_kinds="issue",
        ),
        "produced_by": EdgeKindPolicy(
            description=(
                "produced_by is Inquiry -> Inquiry, stored child -> parent (from "
                "= the produced/younger vertex, to = its producer/older origin). "
                "Any inquiry can be produced by any other, so an Issue is a valid "
                "from-side. Backs the first-edge younger-produced_by-older rule "
                "(see Inquiry.produced_by)."
            ),
            forward_label="produced_by",
            inverse_label="produces",
            from_kinds="inquiry",
            to_kinds="inquiry",
        ),
        "proves": EdgeKindPolicy(
            description=(
                "proves/favors store Artifact -> {Belief, Experiment}: the citing "
                "evidence (the younger child, gathered to bear on the claim) "
                "points up to the older claim it supports. The dependent is the "
                "cited claim on the to-side: a claim leaning on this evidence is "
                "re-assessed when the evidence moves. For-vs-against is the sign "
                "of Edge.valence, not a separate edge kind. proves is load-bearing."
            ),
            forward_label="proves",
            inverse_label="proved_by",
            from_kinds="artifact",
            to_kinds="claimable",
            cascade_dependent="to",
        ),
        "favors": EdgeKindPolicy(
            description=(
                "favors is the context sibling of proves (same Artifact -> claim "
                "shape); it informs without voting in the proof predicate. The "
                "cited claim on the to-side is the dependent, re-assessed when the "
                "favoring evidence moves."
            ),
            forward_label="favors",
            inverse_label="favored_by",
            from_kinds="artifact",
            to_kinds="claimable",
            cascade_dependent="to",
        ),
        "supersedes": EdgeKindPolicy(
            description=(
                "supersedes stores successor -> predecessor (child -> parent). "
                "New (from) supersedes old (to), so the old predecessor drops out "
                "of the scheduler and out of 'currently true' evidence."
            ),
            forward_label="supersedes",
            inverse_label="superseded_by",
            from_kinds="inquiry",
            to_kinds="inquiry",
            skips_scheduler_on="to",
            invalidates_currency_on="to",
        ),
        "cites_paper": EdgeKindPolicy(
            description=(
                "cites_paper stores Paper -> Paper (from = citing/younger, to = "
                "cited/older): a historical/bibliographic citation, distinct from "
                "the epistemic proves/favors. Inert -- default cascade, no "
                "scheduler/currency effect, no valence. Provenance-neutral "
                "(absent from both PRODUCED_INFERENCE_* tables) and "
                "acyclicity-EXEMPT: a bibliography is an external fact we record, "
                "not a DAG we own, so mutual citation is valid data, not a cycle."
            ),
            forward_label="cites",
            inverse_label="cited_by",
            from_kinds="paper",
            to_kinds="paper",
            enforces_acyclicity=False,
        ),
    }
)
"""The full ``edge_kind -> EdgeKindPolicy`` table; the single source of truth
for every per-edge-kind behavior AND relation label.

A read-only :class:`~types.MappingProxyType` keyed by :data:`Edge.Kind`. Read by
``Store._cascade_dependency_changed`` (alert endpoint), ``proves_belief`` SQL
(drop superseded artifacts from "currently true"), ``next_issue`` SQL (skip
superseded Issues), ``_reject_edge_cycle`` (acyclicity), and the relation-label
consumers -- ``trax.render._relation_title``, the SPA edge picker
(``/api/meta/edges``) -- which all read :attr:`~EdgeKindPolicy.forward_label` /
:attr:`~EdgeKindPolicy.inverse_label` here rather than a parallel table.

A new edge kind needs three edits: add it to :data:`Edge.Kind`, add it to the
schema's edge CHECK, and add an entry here. The ``sql_fragments.py`` helpers
generate the cascade, scheduler, and currency clauses from this table, so the
common cases need no SQL change.
"""


# Precedence for first-edge provenance inference (Store._infer_produced_on_conn).
# Inference is universal ONLY over STRUCTURAL edge kinds (this list): each stores
# younger -> older AND implies the older produced the younger, so the first such
# edge between a pair infers the same ``younger produced_by older`` edge. This
# list does NOT decide direction or whether to stamp -- only WHICH present kind's
# name labels the audit reason when several connect the pair (a cosmetic choice:
# ordered strongest-origination-signal first). ``produced_by`` leads because it
# already *is* provenance, so its presence means "already recorded" and (via
# PRODUCED_INFERENCE_SUPPRESSED below) suppresses a redundant second stamp.
#
# CITATION kinds (``proves``, ``favors``, ``cites_paper``) are deliberately NOT
# here -- see PRODUCED_INFERENCE_NEUTRAL: a citation is not a production claim.
PRODUCED_INFERENCE_PRECEDENCE: Final[tuple[Edge.Kind, ...]] = (
    "produced_by",
    "narrows",
    "requires",
    "supersedes",
)

# Idempotency skip: a pair that already carries a ``produced_by`` needs no
# second one. ``produced_by`` ranks first in PRECEDENCE (so it wins the reason
# label) AND is the lone SUPPRESSED member (so a pre-existing provenance edge
# vetoes inferring a duplicate). Citation kinds are handled by NEUTRAL below,
# not here: they must never veto a coexisting structural edge's inference.
PRODUCED_INFERENCE_SUPPRESSED: frozenset[Edge.Kind] = frozenset({"produced_by"})

# Provenance-NEUTRAL edge kinds: deliberately absent from PRECEDENCE (and so
# from SUPPRESSED). A pair whose only edge is a neutral kind infers NO
# produced_by (no precedence member matches, so the inference winner is None),
# yet the kind never appears in SUPPRESSED either, so it cannot veto inference
# from a coexisting non-neutral edge.
#
# All THREE citation kinds are neutral, for one reason: a citation records that
# the citer points AT a claim/source, never that the target produced the citer.
# - ``cites_paper``: "A cites B" is not "B produced A".
# - ``proves`` / ``favors``: an Artifact (Paper/Experiment) citing a Belief did
#   not get produced BY that Belief -- the evidence predates and is independent
#   of the claim it is later marshalled to support. Ranking them in PRECEDENCE
#   mis-stamped every citation as ``younger_claim produced_by older_paper`` (or
#   vice-versa by age), giving a cited Belief one bogus ``produced_by`` parent
#   per citing Paper. Provenance for a Belief comes from the Issue/Belief that
#   spawned it (a real ``produced_by`` edge), not from its evidence.
# Invariant (edge_topology_test): PRECEDENCE and NEUTRAL partition Edge.Kind --
# every kind either ranks in the inference or is explicitly neutral, so a new
# kind cannot silently fall through with undefined provenance behavior.
PRODUCED_INFERENCE_NEUTRAL: frozenset[Edge.Kind] = frozenset(
    {"cites_paper", "proves", "favors"}
)


def edge_topology() -> dict[str, dict[str, list[str]]]:
    """``edge_kind -> {from_kinds, to_kinds}`` with concrete kind lists.

    The single source of truth for which kinds an edge admits on each stored
    endpoint. Served at ``/api/meta/edges`` so the SPA derives its edge picker
    instead of hard-coding directions; pinned against the schema CHECK by a
    drift test so the two cannot diverge (a citation-direction change must
    update this one place, not a stale hand-typed SPA copy).
    """
    return {
        kind: {
            "from_kinds": list(kind_group_members(policy.from_kinds)),
            "to_kinds": list(kind_group_members(policy.to_kinds)),
        }
        for kind, policy in EDGE_POLICIES.items()
    }


def edge_labels() -> dict[str, dict[str, str]]:
    """``edge_kind -> {forward, inverse}`` relation labels per direction.

    The single source of the human relation name for each direction, from
    :attr:`EdgeKindPolicy.forward_label` / :attr:`~EdgeKindPolicy.inverse_label`.
    Served at ``/api/meta/edges`` alongside the topology so the SPA's
    ``edgeDisplayName`` derives its labels here instead of a hand-typed copy --
    the same single-source rule the topology already follows.
    """
    return {
        kind: {"forward": policy.forward_label, "inverse": policy.inverse_label}
        for kind, policy in EDGE_POLICIES.items()
    }


_MISSING_POLICY = set(get_args(Edge.Kind.__value__)) - set(EDGE_POLICIES)
if _MISSING_POLICY:
    raise RuntimeError(
        f"EDGE_POLICIES missing an entry for {sorted(_MISSING_POLICY)}; every "
        "Edge.Kind needs a policy (labels + endpoints + behavior)."
    )
