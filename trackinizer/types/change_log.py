"""Append-only audit row plus its per-side Snapshot value type."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from trackinizer.types.columns import Row
from trackinizer.types.cost import Cost
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import (
    Belief,
    Inquiry,
    Issue,
    Paper,
)


# Snapshot fields stored as Postgres arrays, normalized with
# tuple(value or ()) on read. Same idea as _TUPLE_COLUMNS in inquiries.py,
# but these are the change_log mirror columns rather than projections.
_SNAPSHOT_TUPLE_FIELDS: frozenset[str] = frozenset(
    {
        "labels",
        "subscribers",
        "issue_kind",
        "experiment_codechanges",
        "edge_labels",
        "agentsession_rooms",
        "paper_authors",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    """One side of a :class:`Change` delta.

    Held as ``Change.old`` and ``Change.new`` so an event reads as new
    versus old. Each field defaults to ``None`` and is filled only when the
    event touched that column. The Python nesting flattens to ``old_X`` /
    ``new_X`` columns in SQL.

    Note that ``Snapshot.description`` (the inquiry's description on either
    side of a ``description`` change delta) is not :attr:`Change.reason`
    (the per-event annotation).
    """

    title: str | None = None
    """Mirrors :attr:`Inquiry.title`."""

    description: str | None = None
    """Mirrors :attr:`Inquiry.description`."""

    labels: tuple[str, ...] | None = None
    """Mirrors :attr:`Inquiry.labels`."""

    owner: Inquiry.Actor | None = None
    """Mirrors :attr:`Inquiry.owner`."""

    account: Inquiry.Actor | None = None
    """Mirrors :attr:`Inquiry.account`. ``None`` on any change side that is
    not an ``account`` edit; the field is required on the row but a snapshot
    side is populated only for the matching change kind."""

    peer_id: UUID | None = None
    """The edge neighbor's id, on edge and dependency events."""

    peer_kind: Inquiry.InquiryKind | None = None
    """The edge neighbor's row kind."""

    peer_edge_kind: Edge.Kind | None = None
    """The connecting edge's kind; see :data:`Edge.Kind`."""

    status: Inquiry.Status | None = None
    """Mirrors :attr:`Inquiry.status`."""

    belief_judgement: Belief.Judgement | None = None
    belief_confidence: float | None = None

    edge_priority: Issue.Priority | None = None
    """The edge's contextual priority, on an edge mutation."""

    edge_note: str | None = None
    """The edge's annotation text, on an edge mutation."""

    edge_valence: float | None = None
    """The edge's signed valence in ``[-1, 1]``, on an edge mutation."""

    edge_labels: tuple[str, ...] | None = None
    """The edge's labels, on an edge mutation."""

    issue_kind: tuple[Issue.Kind, ...] | None = None
    """Mirrors :attr:`Issue.issue_kind`. Already names its owner, so it
    cannot be confused with :attr:`Change.kind`, the row discriminator."""

    issue_validation: str | None = None
    issue_priority: Issue.Priority | None = None
    experiment_outcome: str | None = None
    experiment_config: dict[str, object] | None = None
    paper_abstract: str | None = None
    paper_authors: tuple[str, ...] | None = None
    paper_publication_type: Paper.PublicationType | None = None
    paper_venue: str | None = None
    paper_subvenue: str | None = None
    paper_publish_date: datetime | None = None
    paper_source: str | None = None
    paper_google_scholar_cluster_id: str | None = None
    paper_google_scholar_cites_id: str | None = None
    codechange_sha: str | None = None
    webresult_url: str | None = None
    websearch_query: str | None = None
    websearch_provider: str | None = None
    experiment_codechanges: tuple[UUID, ...] | None = None
    agentsession_cli: str | None = None
    agentsession_cli_session_id: str | None = None
    agentsession_started: datetime | None = None
    agentsession_ended: datetime | None = None
    agentsession_rooms: tuple[str, ...] | None = None

    subscribers: tuple[Inquiry.Actor, ...] | None = None
    """Mirrors :attr:`Inquiry.subscribers`."""

    marginal_cost: Cost | None = None
    """:attr:`Inquiry.marginal_cost` on this side of the event. The
    ``new - old`` diff is the per-event spend, exposed as
    :attr:`Change.marginal_cost`."""

    def __bool__(self) -> bool:
        """True when any field is populated."""
        return any(getattr(self, f.name) is not None for f in fields(self))

    @classmethod
    def from_row(cls, row: Row, *, prefix: Literal["old_", "new_"]) -> Self:
        """Build one side of the delta from the ``prefix``-named columns.

        Each field is read from ``prefix + name``. A NULL and a missing
        column both become ``None``, which matters for the list columns
        (``labels``, ``subscribers``, ``codechanges``): a NULL must stay
        ``None`` ("absent / untouched") rather than collapse to ``()``. Since
        the write path now stores SQL NULL for an unset *or* cleared list, both
        serialize as ``None`` here -- a stored ``'{}'`` (decoding to ``()``) is
        no longer produced, but the decoder still maps one through faithfully
        if legacy data carries it. ``marginal_cost`` is the one composite,
        rebuilt via :meth:`Cost.from_row`.
        """
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            col = prefix + f.name
            if f.name == "marginal_cost":
                if (
                    prefix + "marginal_cost_agent_usd" in row
                    or prefix + "marginal_cost_resource_usd" in row
                ):
                    kwargs["marginal_cost"] = Cost.from_row(row, prefix=prefix)
            elif col in row:
                value = row[col]
                if value is not None and f.name in _SNAPSHOT_TUPLE_FIELDS:
                    value = tuple(value or ())
                kwargs[f.name] = value
        return cls(**kwargs)


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """An append-only audit row, chained to its cause via ``caused_by``.

    On the Python side the flat ``old_*`` / ``new_*`` columns collapse into
    two nested :class:`Snapshot` objects. The populated fields in each name
    the columns the event touched; a NULL on one side means the value came
    into being or ceased to exist. The schema CHECK constraints decide which
    deltas a given ``kind`` may carry.

    ``old`` / ``new`` is the temporal pair, kept distinct from
    ``edges.from_id`` / ``edges.to_id``, which are graph endpoints in a
    different table.

    The per-event USD spend is the difference between the sides:
    :attr:`marginal_cost` returns ``new.marginal_cost - old.marginal_cost``,
    treating a ``None`` side as ``Cost()``.
    """

    type Kind = Literal[
        # Milestones: no delta, the row's existence is the signal.
        "created",
        "purged",
        # Field edits. The kind is the field's flat storage name: bare for
        # a base field, <kind>_<field> for a kind-specific one. It lines up
        # with the inquiries column, the Snapshot field, and the old_*/new_*
        # mirror columns -- one identifier across every flat surface.
        # Inquiry-base (bare; applies to every kind).
        "status",
        "title",
        "description",
        "labels",
        "owner",
        "account",
        "subscribers",
        "marginal_cost",
        # Issue-only.
        "issue_kind",
        "issue_validation",
        "issue_priority",
        # Belief-only.
        "belief_judgement",
        "belief_confidence",
        # Experiment-only.
        "experiment_outcome",
        "experiment_config",
        "experiment_codechanges",
        # Paper-only.
        "paper_abstract",
        "paper_authors",
        "paper_publication_type",
        "paper_venue",
        "paper_subvenue",
        "paper_publish_date",
        "paper_source",
        "paper_google_scholar_cluster_id",
        "paper_google_scholar_cites_id",
        # CodeChange-only.
        "codechange_sha",
        # WebResult-only.
        "webresult_url",
        # WebSearch-only.
        "websearch_query",
        "websearch_provider",
        # AgentSession-only.
        "agentsession_cli",
        "agentsession_cli_session_id",
        "agentsession_started",
        "agentsession_ended",
        "agentsession_rooms",
        # Edge-table mutations (decomposition, sequencing, provenance,
        # citations, supersedes); the peer triple rides on the Snapshot's
        # peer_* fields. These name an event, not a column, so they keep their
        # verb.
        "edge_added",
        "edge_removed",
        "edge_annotation_changed",
        # Re-assessment alert raised on a parent when an edge child changes.
        "dependency_changed",
        # Foreman hooks for implicit subscription bundles.
        "implicit_subs_opened",
        "implicit_subs_closed",
    ]
    """Closed set; the schema CHECK enforces which columns each kind may
    populate."""

    id: UUID = field(default_factory=uuid4)
    """Row identifier."""

    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When this change was recorded."""

    actor: Inquiry.Actor = "system"
    """A free-form string for what made this change: a user's email, an
    agent label (``"claude-opus"``), a cron job id
    (``"cron/nightly-import"``), or the sentinel ``"librarian"`` for
    server-driven cascades. Separate from the server-stamped
    :attr:`api_key_id`; see the Auth section of ``docs/design.md``."""

    api_key_id: UUID | None = None
    """The ``api_keys.id`` of the bearer token behind this change.
    Server-stamped, never client-set; JOIN to ``api_keys.user_id`` for the
    acting user. ``None`` for pre-v2 rows and for internal cascade
    emissions, which carry no bearer credential."""

    subject_id: UUID | None = None
    """The :class:`Inquiry` this change is about. Deliberately FK-free so a
    ``purged`` change outlives its subject as a tombstone."""

    subject_kind: Inquiry.InquiryKind | None = None
    """The subject row's kind; see :data:`Inquiry.InquiryKind`."""

    kind: Kind | None = None
    """The change category; see :data:`Change.Kind`."""

    caused_by: UUID | None = None
    """The upstream change that triggered this one; ``None`` for changes
    that originate outside, like agent submissions or user actions."""

    reason: str = ""
    """A free-text "why" for this event, like a commit message. Set for
    abandon, supersede, purge, and state-flip; empty otherwise. Not the same
    as :attr:`Snapshot.description`, the inquiry's description column."""

    subscribers_snapshot: tuple[str, ...] = ()
    """The subscriber list as it was when the change was emitted. Kept on
    ``change_log`` so consumers (SSE fan-out, ``what_changed_for_me``) can
    route the event to whoever was subscribed then, without joining to
    ``inquiries`` -- a join that would lose ``purged`` subjects."""

    old: Snapshot = field(default_factory=Snapshot)
    """The column values before this event."""

    new: Snapshot = field(default_factory=Snapshot)
    """The column values after this event."""

    @property
    def marginal_cost(self) -> Cost:
        """Signed USD delta for this event: ``new - old``."""
        return (self.new.marginal_cost or Cost()) - (self.old.marginal_cost or Cost())

    @classmethod
    def from_row(cls, row: Row) -> Self:
        """Build from a ``change_log`` row.

        Identity columns are read directly; ``old`` and ``new`` are
        assembled from the flat ``old_*`` / ``new_*`` columns through
        :meth:`Snapshot.from_row`.
        """
        kwargs: dict[str, Any] = {
            "id": row["id"],
            "created": row["created"],
            "actor": row["actor"],
            "api_key_id": row.get("api_key_id"),
            "subject_id": row["subject_id"],
            "subject_kind": row["subject_kind"],
            "kind": row["kind"],
            "caused_by": row.get("caused_by"),
            "reason": row["reason"],
            "subscribers_snapshot": tuple(row.get("subscribers_snapshot") or ()),
            "old": Snapshot.from_row(row, prefix="old_"),
            "new": Snapshot.from_row(row, prefix="new_"),
        }
        return cls(**kwargs)
