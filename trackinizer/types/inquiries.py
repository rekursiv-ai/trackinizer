"""The Inquiry hierarchy: the domain noun everything else is built around.

Everything in the system is an :class:`Inquiry`, which has two variants: an
:class:`Issue` is a unit of "work" and an :class:`Artifact` is the output of
that work. Giving both a single type is what lets the same edges relate
them -- work can produce knowledge, and knowledge can occasion work, without
crossing a type boundary.

Each relationship lives on the class that owns its meaning: decomposition
on :class:`Issue`, citations on :class:`Artifact`, supersession and
provenance on the base since any inquiry can supersede, produce, or be
produced. Those fields are projections; the real storage is
:class:`~trackinizer.types.edges.Edge`, which the Store reads to fill them.

Each row below lists the fields that class adds; every kind also has
everything above it::

    Inquiry                  # An effort, ongoing or completed.
    │   id
    │   seq
    │   owner
    │   account
    │   status
    │   title
    │   description
    │   labels
    │   marginal_cost
    │   subscribers
    │   superseded_by
    │   supersedes
    │   produces
    │   produced_by
    │   created
    │   modified
    │
    ├── Issue                # Work to pursue.
    │       issue_kind
    │       validation
    │       priority
    │       narrows
    │       narrowed_by
    │       requires
    │       required_by
    │
    └── Artifact             # Knowledge produced and cited.
        │   proves
        │   favors
        │
        ├── Experiment       # Empirical measurement.
        │       codechanges
        │       outcome
        │       config
        │       proved_by
        │       favored_by
        │
        ├── Belief           # Proposition.
        │       judgement
        │       confidence
        │       proved_by
        │       favored_by
        │
        ├── Paper            # Bibliographic source.
        │       abstract
        │       authors
        │       publication_type
        │       venue
        │       subvenue
        │       publish_date
        │       source
        │       google_scholar_cluster_id
        │       google_scholar_cites_id
        │       cites
        │       cited_by
        │
        ├── CodeChange       # One git commit.
        │       sha
        │
        ├── WebSearch        # One query.
        │       query
        │       provider
        │
        ├── WebResult        # One URL.
        │       url
        │
        └── AgentSession     # A captured CLI run.
                cli
                cli_session_id
                started
                ended
                rooms
                opened_by_api_key_id
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, Final, Literal, Self, cast, get_args
from uuid import UUID, uuid4

import re
import sys

from trackinizer.types.columns import (
    ColumnSpec,
    Row,
    column_specs,
    storage_name,
)
from trackinizer.types.cost import Cost


CITATION_VALENCE_DEFAULT: Final[float] = 0.5
"""The signed valence a ``proves`` / ``favors`` citation takes when written
without an explicit value: mild support.

A citation always carries a concrete valence (:attr:`Edge.valence` is NULL only
on structural edges), so every layer that mints a citation -- the
inline-citation submit path, the trax citation aliases, the projection's inbound
view -- reads this one constant instead of repeating the literal. Lives here
rather than beside :class:`Edge` because ``edges.py`` imports this module, not
the reverse."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InquiryEdge:
    """One projected edge endpoint: the related inquiry plus this edge's
    context, viewed from one vertex.

    A relationship projection field on an Inquiry is a tuple of these, one per
    stored ``Edge`` row touching the vertex. Every projected edge carries the
    peer's id and kind plus the branch-agnostic annotations (``note``,
    ``labels``). The two Inquiry branches subclass this to add the annotation
    their edges carry: :class:`IssueEdge` adds the contextual ``priority`` (on
    Issue-to-Issue ``narrows`` / ``requires`` edges), :class:`ArtifactEdge` adds
    the signed ``valence`` (on ``proves`` / ``favors`` citations). The base type
    backs the branch-neutral ``produces`` and ``supersedes`` relations.

    This replaces the ad-hoc mix of bare ``UUID`` tuples and
    ``(UUID, kind)`` / ``(UUID, priority)`` pairs with one named-field type.
    """

    id: UUID
    """The peer inquiry's row id."""

    kind: Inquiry.InquiryKind
    """The peer inquiry's kind discriminator."""

    note: str | None = None
    """Free text saying what the peer means in this relationship; the edge's
    ``note`` annotation."""

    labels: tuple[str, ...] | None = None
    """Edge-local labels for why this relationship matters here."""


@dataclass(frozen=True, slots=True, kw_only=True)
class IssueEdge(InquiryEdge):
    """An Issue-to-Issue edge endpoint (``narrows`` / ``requires``), carrying the
    contextual ``priority`` the edge may override. ``None`` means the edge adds
    no override, so the endpoint Issue's own :attr:`Issue.priority` applies.
    """

    priority: Issue.Priority | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactEdge(InquiryEdge):
    """A citation edge endpoint (``proves`` / ``favors`` and their ``proved_by``
    / ``favored_by`` inverses), carrying the signed :attr:`Edge.valence` --
    positive supports the claim, negative argues against it; magnitude is the
    evidential weight. Defaults to :data:`CITATION_VALENCE_DEFAULT` (mild
    support).
    """

    valence: float = CITATION_VALENCE_DEFAULT


# Genuine Postgres array COLUMNS on ``inquiries`` that ``Inquiry.from_row``
# coerces to a Python tuple. The relationship projection fields (``produces``,
# ``proves``, ``narrowed_by``, ...) are NOT here: they are not row columns
# at all but tuples of :class:`InquiryEdge` built by the projection layer from the
# ``edges`` table, so ``from_row`` skips them (they default to ``()``).
_TUPLE_COLUMNS: frozenset[str] = frozenset(
    {
        "codechanges",
        "issue_kind",
        "rooms",
        "authors",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Inquiry:
    """The fields every kind shares; the base of the hierarchy.

    Each sub-kind adds fields only where its branch owns the meaning. The
    base owns supersession and both directions of production provenance
    (:attr:`produces` and :attr:`produced_by`), since any inquiry can
    supersede, produce, or be produced. The module docstring lists what
    each sub-kind adds.
    """

    type InquiryKind = Literal[
        "Issue",
        "Artifact",
        "Experiment",
        "Paper",
        "Belief",
        "CodeChange",
        "WebResult",
        "WebSearch",
        "AgentSession",
    ]
    """The row discriminator covering every instantiable Inquiry class.

    ``"Artifact"`` is the unspecialized artifact-branch row; ``"Issue"`` is
    the issue-branch row, whose workflow category lives in the set-valued
    :attr:`Issue.issue_kind`. Artifact subclasses narrow this with their own
    ``Kind`` re-declaration -- use :data:`Artifact.Kind` for the
    branch-narrow union.
    """

    type Status = Literal["active", "complete", "abandoned", "invalid"]
    """Whether the row is still relevant in the system.

    This is not truth-value (that is :attr:`Belief.judgement`) and not
    supersession (derived from the ``supersedes`` edges). Something is
    ``invalid`` when, say, an Experiment measurement is retracted, an Issue
    no longer applies, or a Belief's framing is withdrawn. Status and
    judgement are orthogonal: a Belief can be ``invalid`` and ``proven`` at
    once.
    """

    type Actor = str
    """A user or agent identity string, used for ownership, authorship, and
    subscriptions."""

    id: UUID = field(default_factory=uuid4)
    """Row identifier."""

    seq: int = 0
    """A short per-kind running number (``#1``, ``#2``, ...). The ``0``
    default is a placeholder; the Store assigns the real value via
    ``nextval('seq_<kind>')`` on INSERT, so it only means something after
    an insert or a :meth:`from_row`."""

    owner: Actor | None = field(
        default=None,
        metadata=ColumnSpec(sql_type="TEXT", compare_and_set=True),
    )
    """The actor responsible for this row.

    Never auto-stamped from the submitter or audit ``actor``: an unset owner
    is genuinely unowned (stored NULL), not back-filled.
    """

    account: Actor = field(
        default="",
        metadata=ColumnSpec(sql_type="TEXT", required=True),
    )
    """The authenticated user this row is attributed to. Required.

    Distinct from :attr:`owner` (who is *responsible* for the work):
    ``account`` is the *auth identity* the row is held under. Every row
    carries one -- it is NOT NULL and never blankable. On create it
    defaults to the submitter's authenticated email; it may be overridden
    to another active user, and re-pointed later through ``set_account``.
    Both create and edit validate the new value against the live ``users``
    table -- only an ``active`` user is accepted -- so the field always
    names a real account at the moment it is written. A user disabled
    *after* being stamped here is not swept: validation runs on the new
    value at write time, never retroactively.

    The ``""`` default is a placeholder for constructing an in-memory
    instance. It is the route layer (not the Store) that resolves the real
    account from the authenticated principal before submit; the Store
    *rejects* an unresolved (empty) account rather than stamping one, so a
    materialized row never holds the empty string.
    """

    status: Status = field(
        default="active",
        metadata=ColumnSpec(
            supports_reason=True,
            sql_type="TEXT",
            sql_check="status IN ('active', 'complete', 'abandoned', 'invalid')",
            required=True,
            compare_and_set=True,
        ),
    )
    """Where the row sits in its lifecycle; see :data:`Inquiry.Status`."""

    title: str = field(
        default="",
        metadata=ColumnSpec(sql_type="TEXT", required=True),
    )
    """The one-line statement of what this inquiry is. Required.

    Edited through ``Store.set_title``, which re-embeds inside the same
    transaction. The ColumnSpec stays here so the schema generator still
    emits the change_log mirror columns and populated-iff CHECKs.
    """

    description: str | None = field(
        default=None,
        metadata=ColumnSpec(
            sql_type="TEXT",
        ),
    )
    """Optional long-form detail; empty when the title says enough."""

    labels: tuple[str, ...] | None = field(
        default=None,
        metadata=ColumnSpec(sql_type="TEXT[]", list_verb_stem="label"),
    )
    """Free-form tags intrinsic to this row, filterable from the CLI/UI."""

    marginal_cost: Cost = field(
        default_factory=Cost,
        metadata=ColumnSpec(
            supports_reason=True,
            flatten=Cost,
            flatten_prefix="marginal_cost_",
        ),
    )
    """The running USD total of every ``Change`` against this row. It never
    rolls up through hierarchy, provenance, or citation edges; the Store's
    ``emit_change`` keeps it current with ``+=`` on the two flat columns.

    Stored flattened: ``flatten=Cost`` puts it in ``marginal_cost_agent_usd``
    and ``marginal_cost_resource_usd``, one per :class:`Cost` axis, which is
    the shape :func:`flat_column_specs` (and so the route table and filter
    whitelist) reads."""

    subscribers: tuple[Actor, ...] | None = field(
        default=None,
        metadata=ColumnSpec(
            sql_type="TEXT[]",
            list_verb_stem="subscriber",
        ),
    )
    """Actors notified on every change to this row. This column is the only
    storage -- there is no separate subscriptions table, and no open/close
    timestamps or explicit-vs-implicit distinction are kept."""

    superseded_by: tuple[InquiryEdge, ...] = ()
    """Successor inquiries that replace or refine this one.

    Supersession is many-to-many: one broad Inquiry can split into several
    sharper successors, and several narrow Inquiries can merge into one. This
    is the outbound view of ``supersedes`` edges pointing at this row.
    """

    supersedes: tuple[InquiryEdge, ...] = ()
    """Predecessor inquiries this one replaces, refines, or merges.

    Empty when this is not a replacement. Multiple entries mean this row
    carries forward several earlier pieces of work or knowledge.
    """

    produces: tuple[InquiryEdge, ...] = ()
    """The inquiries this inquiry's work produced.

    Provenance of origin, not containment or decomposition: each produced
    inquiry is evidence, output, or follow-on work caused by this one. It does
    not inherit this row's status, priority, or scheduling state. Any inquiry
    can produce any other -- an Issue produces the Beliefs it seeds and the
    narrower Issues it spawns, a Belief produces the searches and experiments
    its own inquiry spawns -- so this lives on the base and admits the full
    ``Inquiry`` set (an Issue is a valid produced row, not only an Artifact).
    Each :class:`InquiryEdge` carries the produced row's kind because that kind
    is polymorphic. This is the inverse view of the stored ``produced_by``
    edges; the forward (child's) view is :attr:`produced_by`.

    Provenance is the one relation with no child-as-subject active verb in
    English, so the stored edge kind is the passive ``produced_by`` (the child
    points up to its producer parent); ``produces`` is only this inverse
    projection. Every other edge names the child's relation to its parent with
    an active verb.

    Default inference rule (the definition of provenance):

        The FIRST edge between two vertices infers that the YOUNGER vertex (by
        :attr:`created`) was produced by the OLDER, stamping a ``produced_by``
        edge (younger -> older) automatically as edges land, so provenance is
        populated by default rather than relying on it being hand-recorded.
        Inference is UNIVERSAL over the *inference-participating* kinds: every
        such kind stores younger -> older, so a first edge of any of them
        between a pair implies ``younger produced_by older`` -- the inferred
        edge always agrees with the triggering one (``X supersedes Y`` implies X
        younger, hence ``X produced_by Y``, same direction). The younger/older
        ASSIGNMENT is fixed by the two rows' :attr:`created` timestamps (for two
        rows inserted in one batch, their INSERT order, broken deterministically
        by id), so the direction never depends on edge-arrival order.

        The participating kinds are exactly ``PRODUCED_INFERENCE_PRECEDENCE``;
        its complement is ``PRODUCED_INFERENCE_NEUTRAL`` (both in
        ``types/edges.py``), and the two PARTITION ``Edge.Kind``. The neutral
        kinds are exactly the CITATIONS -- ``cites_paper`` (Paper->Paper
        bibliography) and the epistemic ``proves``/``favors`` (Artifact->claim
        evidence) -- because a citation records that the citer points AT a target,
        never that the target produced the citer. A pair whose sole edge is
        neutral infers NO ``produced_by`` -- "A cites B" is not "B produced A",
        and a Paper favoring a Belief was not produced by that Belief. A neutral
        kind never vetoes inference either: if a participating edge also connects
        the pair, that edge drives the inference normally.

          - Idempotency. A pair that already carries a ``produced_by`` is never
            re-stamped: it ranks first in ``PRODUCED_INFERENCE_PRECEDENCE`` and
            is the lone member of ``PRODUCED_INFERENCE_SUPPRESSED`` (both in
            ``types/edges.py``), so a second one would add nothing.
          - Precedence is otherwise cosmetic. When several participating kinds
            connect the pair, every one yields the same younger -> older edge;
            ``PRODUCED_INFERENCE_PRECEDENCE`` only selects WHICH present kind's
            name labels the inference's audit reason, not whether or in which
            direction to stamp.

    The rule is deliberately broad, chosen for three properties at one cost:

        Pros:
          1. Precise -- a total function of ``created`` order: the first
             participating edge stamps the younger as produced by the older.
          2. Simple -- one rule over the participating kinds, with a single
             declared exception set (``PRODUCED_INFERENCE_NEUTRAL``); the code
             skips re-stamping a pair that already has a ``produced_by`` and
             skips pairs whose only edge is a neutral kind.
          3. Always default-populatable -- the provenance graph is never empty
             merely because no one recorded an origin by hand.

        Con (accepted):
          - Sometimes surprising -- when the deciding edge between two
            independently-conceived vertices is a participating peer link
            (``requires``, ``narrows``, ``favors``, ``supersedes``), the rule
            still stamps the younger as produced by the older, asserting a
            provenance that reflects creation order rather than true causation.
            This is correctable and judged cheaper than an unpopulated graph.
            (A ``cites_paper`` bibliography edge is neutral and never triggers
            this, precisely because its creation order carries no provenance.)
    """

    produced_by: tuple[InquiryEdge, ...] = ()
    """The inquiries whose work produced this one; the inverse of
    :attr:`produces`.

    It answers "what work caused this to exist?" Not a decomposition or
    containment edge -- the producer does not own this row, which does not
    inherit the producer's workflow state. Any inquiry can be produced (an
    Issue spawned by a broader Issue, an Artifact emitted by a Belief's
    inquiry), so this lives on the base and each :class:`InquiryEdge` carries
    the producer's kind. This is the forward (child -> parent) view, stored as
    the ``produced_by`` edge; populated by default through the first-edge rule
    documented on :attr:`produces`.
    """

    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When the inquiry was first recorded."""

    modified: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When the inquiry was last changed."""

    @classmethod
    def from_row(cls, row: Row) -> Self:
        """Build an instance of ``cls`` from a full inquiries row.

        If the row carries a ``kind``, it must match the class being built:
        calling ``Issue.from_row`` on a Belief row raises rather than build
        a junk Issue. ``Inquiry`` itself is the common parent and accepts any
        kind, which is how :meth:`Store.get_inquiry` dispatches through
        ``KIND_TO_CLASS``.
        """
        if cls is not Inquiry and "kind" in row:
            row_kind = row["kind"]
            if row_kind != cls.__name__:
                raise ValueError(f"{cls.__name__}.from_row called on a {row_kind} row")
        # Base identity/metadata columns are read unconditionally below. Gate
        # them first so a partial-projection row raises a clear ValueError
        # naming the missing column, matching the kind-specific loop's
        # ``col not in row`` contract -- not a bare KeyError mid-construction.
        # ``marginal_cost_*`` columns are not listed: ``Cost.from_row`` defaults
        # a missing axis to zero, so they are optional.
        base_row_columns = (
            "id",
            "seq",
            "owner",
            "account",
            "status",
            "title",
            "description",
            "labels",
            "subscribers",
            "created",
            "modified",
        )
        missing = [col for col in base_row_columns if col not in row]
        if missing:
            raise ValueError(
                f"{cls.__name__}.from_row: row missing base columns {missing}"
            )
        kwargs: dict[str, Any] = {
            "id": row["id"],
            "seq": row["seq"],
            "owner": row["owner"],
            "account": row["account"],
            "status": row["status"],
            "title": row["title"],
            "description": row["description"],
            "labels": None if row["labels"] is None else tuple(row["labels"]),
            "marginal_cost": Cost.from_row(row),
            "subscribers": (
                None if row["subscribers"] is None else tuple(row["subscribers"])
            ),
            "created": row["created"],
            "modified": row["modified"],
        }
        specs = column_specs(cls)
        for f in fields(cls):
            if f.name in kwargs:
                continue
            spec = specs.get(f.name)
            # The dataclass attribute stays bare (paper.source); the SQL
            # column is the flat storage name (paper_source). Read the
            # storage column, assign the bare attribute.
            col = storage_name(f.name, spec) if spec is not None else f.name
            if col not in row:
                continue
            value = row[col]
            if value is not None and f.name in _TUPLE_COLUMNS:
                value = tuple(value)
            kwargs[f.name] = value
        return cls(**kwargs)


@dataclass(frozen=True, slots=True, kw_only=True)
class Issue(Inquiry):
    """Schedulable work, or a desired outcome.

    Issues are the only inquiries that decompose into smaller work and the
    only ones that require each other (sequencing). An Issue can produce any
    Inquiry (another Issue, or an Artifact), but that edge records provenance,
    not containment: a produced row is not a narrower Issue and does not inherit
    Issue lifecycle, priority, or scheduling state.

    ``issue_kind`` is set-valued because real issues often mix categories,
    like feature plus question. Tags Trackinizer does not interpret stay in
    inherited :attr:`Inquiry.labels`.
    """

    type Kind = Literal[
        "feature",
        "bug",
        "task",
        "question",
    ]
    """The structured issue categories Trackinizer understands."""

    type Priority = int
    """How urgent an Issue is, lower meaning more urgent.

    The usual bands are 0, 10, 20, 30, 40 for P0-P4, with 20 the default;
    in-between integers are allowed for local ordering. The UI shows the
    Buganizer-style band as ``priority // 10``.
    """

    issue_kind: tuple[Kind, ...] | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Issue"}),
            sql_type="TEXT[]",
            sql_check=(
                "issue_kind <@ ARRAY['feature', 'bug', 'task', 'question']::TEXT[]"
            ),
            min_items=1,
            list_verb_stem="issue_kind",
        ),
    )
    """The Issue's workflow categories, as a set.

    Order carries no meaning; the tuple is just how a frozen dataclass holds
    a SQL array. Tags Trackinizer does not interpret go in
    :attr:`Inquiry.labels`. This is separate from :data:`Inquiry.InquiryKind`,
    which only says the row is an Issue.
    """

    validation: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Issue"}),
            sql_type="TEXT",
        ),
    )
    """What must be true before this Issue can be marked complete.

    Evidence that validation passed belongs in produced Artifacts -- usually
    Beliefs, Experiments, or CodeChanges.
    """

    priority: Priority | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Issue"}),
            sql_type="INTEGER",
            sql_check="priority >= 0",
        ),
    )
    """The Issue's own urgency, lower meaning more urgent (0 is highest).

    Root schedulers use this directly. Under a broader Issue, the
    decomposition edge may carry a contextual priority that overrides this
    one within that broader Issue only. Priority never applies to produced
    Artifacts or propagates through provenance.
    """

    narrows: tuple[IssueEdge, ...] = ()
    """The broader Issues this one decomposes under -- the forward view of
    ``narrows`` edges (stored narrower -> broader); this reads the outbound
    ``to`` endpoints.

    Each :class:`IssueEdge` carries the contextual priority the broader Issue
    assigns this one (``None`` keeps this Issue's default :attr:`priority`
    there). This is purely decomposition -- broader means "a smaller piece of
    that work," never "created by," "evidence for," or "required by." The
    inverse view is :attr:`narrowed_by`.
    """

    narrowed_by: tuple[IssueEdge, ...] = ()
    """The narrower Issues that decompose this one -- the inverse view of
    ``narrows`` edges (stored narrower -> broader); this reads the inbound
    ``from`` endpoints.

    Each :class:`IssueEdge` carries the contextual priority within this broader
    Issue. These are work units, not evidence -- code changes, experiments,
    beliefs, searches, and papers go in :attr:`produces`. The forward view is
    :attr:`narrows`.
    """

    requires: tuple[IssueEdge, ...] = ()
    """Issues that must clear before this one can be claimed or completed --
    this Issue's prerequisites (its do-time parents).

    The forward view of ``requires`` edges, stored child -> parent
    (from=requirer, to=prerequisite); this reads the outbound ``to`` endpoints.
    ``A requires B`` means B must be done first, so B is do-time older. Sequencing
    is not hierarchy: a prerequisite neither contains nor is broader than the
    Issue that requires it. The inverse view is :attr:`required_by`.
    """

    required_by: tuple[IssueEdge, ...] = ()
    """Issues that require this one -- whose scheduling this Issue gates.

    The inverse view of ``requires`` edges (stored from=requirer, to=this Issue);
    this reads the inbound ``from`` endpoints. The forward view is
    :attr:`requires`.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Artifact(Inquiry):
    """A knowledge artifact: evidence or output, not schedulable work.

    Artifacts are produced by inquiries, cite claims (Beliefs and Experiments),
    and are superseded by stronger Artifacts. They do not decompose, gate
    scheduling, carry priority, or own validation. :attr:`Inquiry.produced_by`
    records which inquiries produced this one -- provenance, not hierarchy.

    Artifact-to-artifact links are domain-specific rather than a single
    generic edge: a :class:`WebSearch`'s findings are produced_by it, an
    :class:`Artifact` cites claims, :class:`Experiment` has code states, and any
    inquiry can supersede another.

    Citations are an Artifact -> {Belief, Experiment} edge, so the two halves
    split across the classes: the active forward view (:attr:`proves` /
    :attr:`favors`, the claims this artifact cites) lives here on the base
    :class:`Artifact`, since any artifact can cite; the passive inverse
    (``proved_by`` / ``favored_by``, the artifacts citing a claim) lives on
    :class:`Belief` and :class:`Experiment`, the only citation targets.

    The asymmetry is deliberate, not an oversight. Every artifact can serve
    as evidence -- a URL, a commit, a paper are all things one can point at
    -- so the forward view belongs to all of them. But only some artifacts
    are themselves epistemically grounded, i.e. assert something that
    evidence can bear on: a Belief claims a proposition and an Experiment
    claims a measurement, and both can be argued for or against. A Paper or
    a WebResult asserts nothing of ours; it is a fact of the world we
    recorded, so "what proves this Paper" is not a question the model has,
    and giving it a ``proved_by`` would invite it.

    The sub-kinds are :class:`Experiment` (a measurement), :class:`Paper`
    (an external source), :class:`Belief` (a proposition),
    :class:`CodeChange` (a git commit), :class:`WebResult` (one URL), and
    :class:`WebSearch` (a query and the findings it produced).
    """

    type Kind = Literal[
        "Artifact",
        "Experiment",
        "Paper",
        "Belief",
        "CodeChange",
        "WebResult",
        "WebSearch",
        "AgentSession",
    ]
    """:data:`Inquiry.InquiryKind` narrowed to the artifact branch."""

    proves: tuple[ArtifactEdge, ...] = ()
    """The claims (Beliefs or Experiments) this artifact cites as load-bearing
    proof; the outbound view of ``proves`` edges (stored Artifact ->
    {Belief, Experiment}). Each :class:`ArtifactEdge` carries its signed
    :attr:`Edge.valence` (positive proves, negative disproves; magnitude is the
    evidential weight). The passive inverse is :attr:`Belief.proved_by` /
    :attr:`Experiment.proved_by`."""

    favors: tuple[ArtifactEdge, ...] = ()
    """The claims this artifact cites as context; the outbound view of
    ``favors`` edges. Like :attr:`proves` but non-load-bearing (informs without
    voting in the proof predicate); valence signs for-vs-against. The passive
    inverse is :attr:`Belief.favored_by` / :attr:`Experiment.favored_by`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Experiment(Artifact):
    """An empirical measurement produced by code at one or more commits.

    Lifecycle is on inherited :attr:`Inquiry.status` -- ``active`` while
    running, ``complete`` when done, ``invalid`` if retracted -- and the
    observed result is the free-text :attr:`outcome`. There is no
    confirmed/refuted field: whether an Experiment is load-bearing for or
    against a :class:`Belief` is the citer's stance, not the Experiment's.
    """

    codechanges: tuple[UUID, ...] | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Experiment"}),
            sql_type="UUID[]",
            references=frozenset({"CodeChange"}),
            list_verb_stem="codechange",
        ),
    )
    """The :class:`CodeChange` ids of the code states this ran at -- more
    than one when the experiment compares states."""

    outcome: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Experiment"}),
            sql_type="TEXT",
        ),
    )
    """What was observed, as free text (e.g. "87% accuracy on the held-out
    set", "training diverged after step 1200"). Empty until the experiment
    concludes."""

    config: dict[str, object] | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Experiment"}),
            sql_type="JSONB",
        ),
    )
    """The run's input settings: hyperparameters and configuration, as one
    JSON object (the wandb ``wandb.init(config=...)`` analogue). Opaque to
    trackinizer -- typically a ``configgle`` config dumped to JSON -- so it is
    stored and returned verbatim, filtered/grouped by the caller, never
    interpreted here. Distinct from :attr:`outcome` (the run's result) and from
    the time-series metrics logged against the run."""

    proved_by: tuple[ArtifactEdge, ...] = ()
    """Load-bearing citations: Artifacts that cite this experiment as proof;
    the inbound view of ``proves`` edges (stored Artifact -> Experiment). Each
    :class:`ArtifactEdge` carries the signed :attr:`Edge.valence` (positive
    proves, negative disproves; weights the evidence). The active forward
    counterpart is :attr:`Artifact.proves`. Mirrors :attr:`Belief.proved_by`."""

    favored_by: tuple[ArtifactEdge, ...] = ()
    """Context citations: Artifacts that cite this experiment without voting in
    the proof predicate; the inbound view of ``favors`` edges. Valence signs
    for-vs-against. Mirrors :attr:`Belief.favored_by`."""


# A scheme-tagged ``Paper.source`` identifier: an RFC-3986 scheme (a letter then
# letters/digits/+/-/.), a ``:``, and a remainder carrying at least one
# non-whitespace character. The scheme set is OPEN
# (arXiv/doi/http(s)/isbn/pmid/...): the shape is enforced, the scheme name is
# not, so any well-formed identifier passes but a bare value that drops the
# scheme -- or one whose remainder is only whitespace (``"doi: "``) -- is
# rejected. ``\s*\S`` requires that non-whitespace character; a bare ``.+``
# would accept a lone space.
_SOURCE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:\s*\S")


def is_valid_source(value: str) -> bool:
    """Whether ``value`` is a well-formed scheme-tagged ``Paper.source``.

    One rule for both the create boundary (``SubmitPaper``) and the edit boundary
    (``Store.set_source``): a ``<scheme>:<rest>`` with a non-empty scheme and a
    non-empty remainder. See :data:`_SOURCE_SCHEME_RE`.
    """
    return _SOURCE_SCHEME_RE.match(value) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class Paper(Artifact):
    """An external written source with first-class bibliography fields.

    The base :attr:`Inquiry.title` is the paper title. :attr:`publication_type`
    is the closed-set BibTeX entry type (``article``, ``inproceedings``,
    ``book``, ``thesis``, ``techreport``, ``misc``); :attr:`venue` is the
    free-text series/journal name (``"NeurIPS"``, ``"Nature"``, ``"KDD"``) and
    :attr:`subvenue` the free-text track/workshop within it. :attr:`source` is
    one self-describing identifier whose scheme prefix names its kind
    (``arXiv:...``, ``doi:...``, ``http(s)://...``, ``isbn:...``). The scheme
    set is open; only the ``<scheme>:<rest>`` shape is enforced (see
    :func:`is_valid_source`).
    """

    type PublicationType = Literal[
        "article",
        "inproceedings",
        "book",
        "thesis",
        "techreport",
        "misc",
    ]
    """The closed-set BibTeX entry type. ``article`` is a journal paper,
    ``inproceedings`` a conference paper, ``thesis`` collapses
    ``@phdthesis``/``@mastersthesis``, ``techreport`` a report, and ``misc``
    the catch-all (preprints, anything else). The series/journal name itself
    is the free-text :attr:`venue`, never an enum member."""

    abstract: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
        ),
    )
    """The paper's abstract."""

    authors: tuple[str, ...] | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT[]",
            list_verb_stem="author",
            is_byline=True,
        ),
    )
    """Ordered author list. A SQL array like :attr:`Inquiry.labels`; order
    is significant (it is the byline order)."""

    publication_type: PublicationType | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
            sql_check=(
                "publication_type IN ('article', 'inproceedings', 'book', "
                "'thesis', 'techreport', 'misc')"
            ),
        ),
    )
    """The BibTeX entry type; see :data:`Paper.PublicationType`."""

    venue: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
        ),
    )
    """Free-text series/journal name ("NeurIPS", "Nature", "KDD"). Open set;
    the closed publication category is :attr:`publication_type`."""

    subvenue: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
        ),
    )
    """Free-text track/workshop within the :attr:`venue` ("Main", "Workshop
    on X"), or the book title / school for a book / thesis."""

    publish_date: datetime | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TIMESTAMPTZ",
        ),
    )
    """The full publication date."""

    source: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
        ),
    )
    """One self-describing identifier whose scheme prefix names its kind:
    ``arXiv:2405.16391``, ``doi:10.1145/3292500``, ``http://...``,
    ``https://...``."""

    google_scholar_cluster_id: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
        ),
    )
    """The Google Scholar CLUSTER handle (``data-cid``), when this paper is known
    on Scholar. The paper's stable Scholar IDENTITY -- present for EVERY indexed
    paper, cited or not. Distinct from :attr:`google_scholar_cites_id` (the
    cited-by pivot, which exists only once a paper has citations). Kept SEPARATE
    from :attr:`source` (DOI/arXiv id). Opaque -- stored/returned verbatim, fed to
    a Scholar ``related`` lookup, never parsed."""

    google_scholar_cites_id: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Paper"}),
            sql_type="TEXT",
        ),
    )
    """The Google Scholar CITED-BY handle (``cites_id``), when this paper has
    citations on Scholar. The pivot for fetching a paper's citation graph
    (``gs.cited_by(cites_id)``). Exists ONLY for cited papers -- an uncited paper
    has a :attr:`google_scholar_cluster_id` but no cites_id. Opaque -- stored and
    returned verbatim, fed back to a Scholar cited-by lookup, never parsed."""

    cites: tuple[InquiryEdge, ...] = ()
    """The papers this paper historically cites -- its bibliography; the forward
    (outbound) view of ``cites_paper`` edges (stored citing -> cited, so this
    reads the outbound ``to`` endpoints).

    This is a HISTORICAL, bibliographic fact, deliberately distinct from the
    epistemic :attr:`Artifact.proves` / :attr:`Artifact.favors`: it records that
    this external source cites another, not our judgement about whether the cited
    paper supports a claim. It therefore carries no valence -- each
    :class:`InquiryEdge` carries only the branch-neutral ``note`` / ``labels``
    (e.g. "see \xa73", "prior art"). ``Paper -> Paper`` only. The inverse view is
    :attr:`cited_by`."""

    cited_by: tuple[InquiryEdge, ...] = ()
    """The papers that historically cite this one -- the inverse (inbound) view
    of ``cites_paper`` edges (stored citing -> cited, so this reads the inbound
    ``from`` endpoints). The forward view is :attr:`cites`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Belief(Artifact):
    """A proposition whose support comes from cited Artifacts.

    Citations split one way: load-bearing (``proves``) versus context
    (``favors``). Both store ``Artifact -> Belief`` (child -> parent: the citing
    evidence, gathered to bear on the claim, points up to this older Belief).
    The Belief reads them through its inbound :attr:`proved_by` / :attr:`favored_by`
    projections; the active forward view lives on :attr:`Artifact.proves` /
    :attr:`Artifact.favors`. For-vs-against is not a separate edge kind; it is
    the sign of :attr:`Edge.valence` -- positive supports the belief, negative
    argues against it, and the magnitude is the evidential weight. A ``proves``
    citation votes in the proof predicate; a ``favors`` citation is context that
    informs without voting.

    Folding polarity into valence collapses the old four-way split
    (proves/disproves/favors/disfavors) and the separate refutation edge into
    two kinds carrying a signed weight.

    :attr:`judgement` is the author's coarse verdict and :attr:`confidence`
    their current probability that the proposition is true; citations inform
    both but never set them automatically. Status and supersession are
    orthogonal to these: a ``proven`` belief can be superseded by a sharper
    one, and an ``invalid`` (retracted framing) belief can still read
    ``proven``.
    """

    type Judgement = Literal["proven", "disproven", "unproven", "undecidable"]
    """The author's coarse verdict bucket.

    ``proven`` / ``disproven`` mark a deliberate resolution; ``unproven`` is
    the normal state for an active musing; ``undecidable`` says the
    proposition cannot be settled on available terms. Orthogonal to status,
    confidence, and supersession.
    """

    judgement: Judgement | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Belief"}),
            supports_reason=True,
            sql_type="TEXT",
            sql_check=(
                "judgement IN ('proven', 'disproven', 'unproven', 'undecidable')"
            ),
            compare_and_set=True,
        ),
    )
    """The author's coarse verdict on the proposition. Evidence informs it,
    but nothing sets it automatically."""

    confidence: float | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"Belief"}),
            supports_reason=True,
            sql_type="DOUBLE PRECISION",
            sql_check="confidence >= 0 AND confidence <= 1",
        ),
    )
    """The author's current probability that the proposition is true.

    Judgement is the coarse bucket; confidence keeps the calibrated detail
    within and across buckets. ``0.5`` is the neutral prior.
    """

    proved_by: tuple[ArtifactEdge, ...] = ()
    """Load-bearing citations: Artifacts that cite this belief as proof and
    vote in the proof predicate; the inbound view of ``proves`` edges (stored
    Artifact -> Belief, so these are the inbound ``from`` endpoints, the citing
    artifacts). Each :class:`ArtifactEdge` carries the signed :attr:`Edge.valence`
    (positive proves, negative disproves; weights the evidence). The active
    forward counterpart is :attr:`Artifact.proves`."""

    favored_by: tuple[ArtifactEdge, ...] = ()
    """Context citations: Artifacts that cite this belief without voting in the
    proof predicate; the inbound view of ``favors`` edges. Each
    :class:`ArtifactEdge` carries the signed :attr:`Edge.valence` (positive
    favors, negative disfavors; weights the context). The active forward
    counterpart is :attr:`Artifact.favors`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeChange(Artifact):
    """A git commit, citeable like any other Artifact.

    Purpose tags (``bugfix``, ``feature``, ``refactor``, and so on) go in
    inherited :attr:`Inquiry.labels`. Being a first-class Artifact lets a
    commit ``proves`` / ``favors`` a :class:`Belief`, be subscribed to, and
    be label-filtered through the same machinery as every other kind.
    """

    sha: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"CodeChange"}),
            sql_type="TEXT",
        ),
    )
    """The git SHA identifying the commit."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WebResult(Artifact):
    """A single web page, citeable like any other Artifact.

    The page title goes in inherited :attr:`Inquiry.title`; excerpts or
    notes go in :attr:`Inquiry.description`. It can stand alone as evidence
    that ``proves`` / ``favors`` a claim, or be produced by a
    :class:`WebSearch`.
    """

    url: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"WebResult"}),
            sql_type="TEXT",
        ),
    )
    """The page URL."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WebSearch(Artifact):
    """A record of a web search and the references it returned.

    Like any artifact it can cite a claim: a WebSearch ``proves`` a Belief with
    a positive valence ("a standard search confirms this is widely reported") or
    a negative valence ("the position appears in no major index"). The query is
    kept so the search can be reproduced.

    The findings a search surfaced are recorded as ``produced_by`` edges
    (WebResult/Paper -> WebSearch: the finding is the younger child of the
    search that surfaced it), not a column: found Artifacts are many-to-one (two
    searches surfacing the same Paper share one node, each carrying its own
    ``produced_by`` edge to its search), so membership lives on the edge set.
    """

    query: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"WebSearch"}),
            sql_type="TEXT",
        ),
    )
    """The search string as issued."""

    provider: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"WebSearch"}),
            sql_type="TEXT",
        ),
    )
    """The search engine (``google``, ``duckduckgo``, ``arxiv``, ...). Empty
    when unknown or untracked."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSession(Artifact):
    """A captured agent-CLI session, citeable like any other Artifact.

    Produced by ``trax run <cli>``: the harness wraps the CLI, tails its
    native session log, and ingests turn-grained events. This row is the
    queryable handle -- edge-able to the Issues/CodeChanges the session bore
    on, supersede-able when a session is resumed. The per-turn events live in
    the separate append-only ``agent_session_events`` table (outside
    ``inquiries``), scoped to this row by ``session_id``.

    The session transcript proper (user/assistant/thinking/tool turns) is not
    on this row; only the envelope is. The session title goes in inherited
    :attr:`Inquiry.title`.
    """

    cli: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"AgentSession"}),
            sql_type="TEXT",
        ),
    )
    """Which CLI was wrapped (``claude``, ``gemini``, ``codex``, ``cursor``)."""

    cli_session_id: str | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"AgentSession"}),
            sql_type="TEXT",
        ),
    )
    """The CLI's own session identifier (claude ``sessionId``, codex thread
    id), for correlation with the vendor's own records.

    Model and working directory are intentionally **not** fields: both can
    change mid-session, so a single value would be lossy. Per-turn model
    lives on ``agent_session_events.model``."""

    started: datetime | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"AgentSession"}),
            sql_type="TIMESTAMPTZ",
        ),
    )
    """When the wrapped session began. Distinct from inherited
    :attr:`Inquiry.created` (when this row was recorded)."""

    ended: datetime | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"AgentSession"}),
            sql_type="TIMESTAMPTZ",
            # Written only by the atomic lifecycle moves -- ``Store.end_session``
            # sets it (with ``status='complete'``) and ``Store._resume_session``
            # clears it (with ``status='active'``) on a re-open; a blind field
            # PUT would desync the lifecycle CHECK, so no route.
            route_editable=False,
        ),
    )
    """When the wrapped session ended; empty while it is still live.

    Written only by the atomic lifecycle moves -- ``Store.end_session`` sets it
    (with ``status='complete'``) and ``Store._resume_session`` clears it (with
    ``status='active'``) on resume -- never via a standalone field edit, so the
    lifecycle CHECK that ties ``ended`` to ``status`` always holds."""

    rooms: tuple[str, ...] | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"AgentSession"}),
            sql_type="TEXT[]",
            list_verb_stem="room",
        ),
    )
    """Namespaces this session can be addressed within (``@actor:room``).

    A session may join several rooms; messaging resolves a target name to a
    live session scoped to a room. Reassignable like any list field (the
    ``room`` add/del verbs); empty means the session is in no room yet."""

    opened_by_api_key_id: UUID | None = field(
        default=None,
        metadata=ColumnSpec(
            applies_to_inquiry_kinds=frozenset({"AgentSession"}),
            sql_type="UUID",
            immutable=True,
        ),
    )
    """The ``api_keys.id`` that opened this session; the inbound-drain route
    requires a matching credential, so only the session's own poller can drain
    its queue. ``None`` when opened in ``--no-auth`` mode (a None==None match
    keeps demo mode draining its own sessions). ``immutable``: stamped once at
    submit, never edited -- so it carries no setter, route, or change_log
    mirror."""


def _kind_to_class() -> dict[Inquiry.InquiryKind, type[Inquiry]]:
    """Map each row discriminator to the concrete class ``inquiries`` exports.

    Walks the ``Inquiry`` subclass tree so a new kind registers itself with no
    parallel edit, then resolves each walked class through this module's
    namespace. ``@dataclass(slots=True)`` rebuilds a decorated class (slots
    cannot be added in place), leaving the pre-rebuild "ghost" alive in
    ``__subclasses__()`` until it is GC'd; keying a bare ``{cls.__name__: cls}``
    comprehension would non-deterministically pick the ghost, whose
    ``isinstance`` identity differs from the exported class. Resolving through
    ``sys.modules[__name__]`` pins each kind to the canonical export.
    """
    module = sys.modules[__name__]
    mapping: dict[Inquiry.InquiryKind, type[Inquiry]] = {}
    # Breadth-first in declaration order (``__subclasses__`` preserves
    # definition order), so the mapping is deterministic across runs -- the
    # route/filter tables iterate it and a churned order would reshuffle
    # route registration. ``Issue`` precedes the ``Artifact`` branch.
    queue: list[type[Inquiry]] = list(Inquiry.__subclasses__())
    while queue:
        cls = queue.pop(0)
        queue.extend(cls.__subclasses__())
        canonical = getattr(module, cls.__name__, cls)
        mapping[cast(Inquiry.InquiryKind, cls.__name__)] = canonical
    return mapping


KIND_TO_CLASS: dict[Inquiry.InquiryKind, type[Inquiry]] = _kind_to_class()
"""The one canonical row-discriminator -> concrete dataclass registry.

Every kind-dispatch site reads this -- ``server.projection.materialize``, the
wire route/filter tables, ``trax`` grammar, the query API -- so adding an
``Inquiry`` subclass needs no parallel registry edit. Built by walking
``Inquiry.__subclasses__()`` and resolving each kind through this module's
namespace, so a ``@dataclass(slots=True)`` rebuild ghost never shadows the
exported class (see :func:`_kind_to_class`).

``Inquiry`` itself is the abstract base (the common-parent ``from_row``
target) and is intentionally absent: it is never a stored row ``kind``.
"""

_MISSING_KINDS = set(get_args(Inquiry.InquiryKind.__value__)) - set(KIND_TO_CLASS)
assert not _MISSING_KINDS, (
    f"KIND_TO_CLASS missing concrete subclasses for {sorted(_MISSING_KINDS)}"
)


INQUIRY_CLASSES: tuple[type[Inquiry], ...] = (Inquiry, *KIND_TO_CLASS.values())
"""``Inquiry`` plus every concrete kind, for a walk over the whole hierarchy.

Derived beside the registry rather than rebuilt per consumer: two wire modules
each carried their own identical tuple, and a test existed only to assert the
copies had not drifted. One definition removes both.
"""
