"""Request bodies for inquiry submit and edit routes.

Each ``SubmitX`` mirrors its dataclass minus the auto-generated fields
(id, seq, status, marginal_cost, edge projections, created, modified),
which the Store fills in or edges supply. Per-field edits share three
uniform bodies (:class:`FieldSet`, :class:`FieldOp`,
:class:`FieldMutation`) keyed by HTTP method; the field's concrete type
lives in ``types/inquiries.py``, not here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, Self

import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from trackinizer.types.cost import Cost
from trackinizer.types.edges import (
    EDGE_POLICIES,
    Edge,
    KindGroup,
    kind_group_members,
)
from trackinizer.types.inquiries import (
    CITATION_VALENCE_DEFAULT,
    Artifact,
    Belief,
    Inquiry,
    Issue,
    Paper,
    is_valid_source,
)


BATCH_MAX_ITEMS = (
    1000  # config-globals: ignore -- wire batch/size limit, protocol contract
)


__all__ = [
    "BATCH_MAX_ITEMS",
    "ActorMixin",
    "BatchEdge",
    "Citation",
    "FieldMutation",
    "FieldOp",
    "FieldSet",
    "SubmitAgentSession",
    "SubmitArtifact",
    "SubmitBase",
    "SubmitBatch",
    "SubmitBelief",
    "SubmitCodeChange",
    "SubmitExperiment",
    "SubmitIssue",
    "SubmitItem",
    "SubmitPaper",
    "SubmitWebResult",
    "SubmitWebSearch",
]


class _CostFields(BaseModel):
    """Mixin: optional per-action USD cost attribution."""

    marginal_cost: Cost = Field(default_factory=Cost)
    """USD spend attributed to this action (agent + resource axes)."""


def _reject_blank_strings(value: list[str], *, noun: str) -> list[str]:
    """Reject empty or whitespace-only entries; ``noun`` names them in errors.

    Shared by every list-of-string submit field so the "no blank entries"
    rule lives in one place.
    """
    for entry in value:
        if not entry.strip():
            raise ValueError(f"{noun} must be non-empty")
    return value


def _validate_room_names(value: list[str]) -> list[str]:
    """Reject blank or comma-bearing room names.

    A room name must be a single clean token: non-blank (it is matched
    verbatim against ``agentsession_rooms``) and free of ``,`` (``trax run``
    exports rooms comma-joined into ``TRAX_ROOMS``, so a room ``'a,b'`` is
    indistinguishable from two rooms ``'a'`` and ``'b'`` to an agent inside the
    session). The rule lives here so ``SubmitAgentSession.rooms`` -- and its
    ``SessionStart.rooms`` mirror in ``wire_sessions`` -- agree.
    """
    _reject_blank_strings(value, noun="rooms")
    for room in value:
        if "," in room:
            raise ValueError(f"room name must not contain a comma: {room!r}")
    return value


def _blank_scalar_to_none(value: str | None) -> str | None:
    """Collapse a whitespace-only optional scalar to ``None`` (unset is NULL).

    Shared by every optional free-text / identifier submit scalar
    (``sha``, ``url``, ``query``, ``provider``, ``venue``, ``subvenue``,
    ``abstract``). A blank is "unset", so it clears to ``None`` -- the same
    "unset is one encoding: SQL NULL" rule the edit path
    (``Store._set_field`` -> ``empty_optional_to_none``) and the Paper insert
    already follow, and that :meth:`SubmitPaper._validate_source` applies to
    ``source``. Folding it here makes one rule hold across submit and edit
    instead of submit passing blanks raw to storage.
    """
    return None if value is None or not value.strip() else value


def _dedupe_preserving_order[T](
    value: list[T], *, key: Callable[[T], object]
) -> list[T]:
    """Drop later duplicates by ``key``, keeping the first occurrence's order.

    Shared by the submit fields whose duplicate entries would drive an
    insert-then-upsert phantom audit (``requires`` ids, ``proved_by`` /
    ``favored_by`` citations). One edge per parent is the stored truth, so a
    repeated reference is a client redundancy to collapse, not an error.
    """
    seen: set[object] = set()
    deduped: list[T] = []
    for entry in value:
        marker = key(entry)
        if marker not in seen:
            seen.add(marker)
            deduped.append(entry)
    return deduped


class SubmitBase(_CostFields):
    """Inquiry-base fields every submit body carries.

    Only ``title`` is required at the API boundary. Every other base
    field is optional and stored as SQL NULL when unset -- NULL is the
    single encoding of "absent" (an unset ``owner`` is genuinely unowned,
    not actor-stamped). Defaults for per-kind columns are a client
    decision, so a bare-HTTP submit gets the minimal-information row.
    """

    title: str = Field(min_length=1)
    """Required short label; see :attr:`Inquiry.title`."""

    @field_validator("title", mode="after")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must be non-empty")
        return value

    description: str | None = None
    status: Inquiry.Status | None = None
    """Optional lifecycle status at creation. ``None`` is born ``active`` (the
    server default); an explicit value (e.g. ``complete``) is honored so a
    finished artifact -- a run search, a read paper -- can be created already
    complete instead of needing a follow-up edit. The DB CHECK validates it,
    and kind-specific lifecycle CHECKs still apply (an ``AgentSession`` cannot
    be born ``complete`` without ``ended``)."""
    owner: Inquiry.Actor | None = None
    account: Inquiry.Actor | None = None
    """The active user this row is attributed to. ``None`` lets the server
    default to the creator's authenticated email; a non-``None`` value must
    be a live active user (validated server-side) and overrides that
    default. Distinct from ``owner`` (responsibility) -- this is the auth
    identity the row is held under, and every stored row carries one."""

    @field_validator("account", mode="after")
    @classmethod
    def _validate_account(cls, value: str | None) -> str | None:
        """Reject a blank (whitespace-only) account as malformed input.

        A provided account must name a user; ``""`` / ``"  "`` is malformed
        and is rejected here (422) so it never reaches the active-user probe,
        whose "not an active user" message would misdescribe the cause.
        ``None`` (unset, server defaults to the creator) stays valid.
        """
        if value is not None and not value.strip():
            raise ValueError("account must be non-empty")
        return value

    actor: str | None = None
    """Audit actor recorded on the ``created`` change. ``None`` lets the
    server default from the authenticated principal's email."""

    @field_validator("actor", mode="after")
    @classmethod
    def _validate_actor(cls, value: str | None) -> str | None:
        """Reject a blank (whitespace-only) actor as malformed input.

        ``submit_batch`` does ``item.actor or actor``, so a whitespace-only
        ``actor`` would silently fall through to the batch actor -- the same
        malformed-input hazard ``_validate_account`` guards against. ``None``
        (server defaults from the principal) stays valid.
        """
        if value is not None and not value.strip():
            raise ValueError("actor must be non-empty")
        return value

    idempotency_key: uuid.UUID | None = None
    """Optional client-supplied idempotency key.

    When set, the server uses this UUID as the ``change_log.id`` of the
    ``created`` event. A repeat submit with the same key returns the
    original inquiry's id without writing a duplicate, so a client can
    safely retry after a network timeout ate the response. Omit it to get
    a fresh id per submit, in which case the request is not retry-safe.

    Either way the new inquiry's ``id`` is server-minted; the client
    learns it from the response.
    """

    labels: list[str] | None = None
    subscribers: list[Inquiry.Actor] | None = None

    @field_validator("subscribers", mode="after")
    @classmethod
    def _validate_subscribers(cls, value: list[str] | None) -> list[str] | None:
        return (
            None if value is None else _reject_blank_strings(value, noun="subscribers")
        )


class SubmitIssue(SubmitBase):
    """Submit body for a new Issue."""

    kind: Literal["Issue"] = "Issue"
    issue_kind: list[Issue.Kind] | None = Field(default=None, min_length=1)
    validation: str | None = None
    priority: Issue.Priority | None = Field(default=None, ge=0)
    narrows: list[tuple[uuid.UUID, Annotated[Issue.Priority, Field(ge=0)] | None]] = (
        Field(default_factory=list)
    )
    """Broader Issues this one decomposes under (its ``narrows`` parents), each
    with the contextual priority the broader Issue assigns it. The contextual
    priority carries the same nonnegative bound as :attr:`priority` and the
    edges ``CHECK``, so a negative value is a clean 422 at the wire rather than
    a mid-transaction DB violation."""
    requires: list[uuid.UUID] = Field(default_factory=list)
    """Prerequisite Issues this one requires done first (its do-time parents)."""

    @field_validator("requires", mode="after")
    @classmethod
    def _dedupe_requires(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return _dedupe_preserving_order(value, key=lambda id_: id_)


class SubmitArtifact(SubmitBase):
    """Submit body for a generic, unspecialized Artifact."""

    kind: Literal["Artifact"] = "Artifact"


class SubmitExperiment(SubmitBase):
    """Submit body for a new Experiment."""

    kind: Literal["Experiment"] = "Experiment"
    codechanges: list[uuid.UUID] | None = None
    """:class:`CodeChange` ids of the code states this experiment ran at."""

    outcome: str | None = None

    config: dict[str, object] | None = None
    """The run's input settings / hyperparameters as one JSON object (the
    wandb ``config`` analogue); stored verbatim, opaque to trackinizer."""


class SubmitPaper(SubmitBase):
    """Submit body for a new Paper."""

    kind: Literal["Paper"] = "Paper"
    abstract: str | None = None
    authors: list[str] | None = None
    publication_type: Paper.PublicationType | None = None
    venue: str | None = None
    subvenue: str | None = None
    publish_date: datetime | None = None
    source: str | None = None
    google_scholar_cluster_id: str | None = None
    google_scholar_cites_id: str | None = None

    @field_validator(
        "abstract",
        "venue",
        "subvenue",
        "google_scholar_cluster_id",
        "google_scholar_cites_id",
        mode="after",
    )
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return _blank_scalar_to_none(value)

    """A scheme-tagged identifier: ``<scheme>:<rest>`` (``arXiv:...``,
    ``doi:...``, ``http(s)://...``, ``isbn:...``). The wire enforces the
    shape, not a closed scheme list, so any well-formed identifier passes
    but a bare value (``2405.16391``) that drops the scheme is rejected."""

    @field_validator("source", mode="after")
    @classmethod
    def _validate_source(cls, value: str | None) -> str | None:
        # A whitespace-only / empty source is "no source" -> clear to None,
        # matching ``Store.set_source`` so create and edit agree. A non-empty
        # value must carry a scheme (``<scheme>:<rest>``).
        if value is None or not value.strip():
            return None
        if not is_valid_source(value):
            raise ValueError(
                "source must be a scheme-tagged identifier '<scheme>:<rest>' "
                "(e.g. arXiv:2405.16391, doi:10.1/x, https://example.com/p); "
                f"got {value!r}"
            )
        return value


class Citation(BaseModel):
    """One inline citation on a :class:`SubmitBelief`: which artifact, how strong.

    The signed ``valence`` is bounded to the same ``[-1, 1]`` the edges column
    enforces, so a malformed-weight citation is rejected at the wire boundary
    rather than surfacing as a mid-transaction CHECK violation. The polarity is
    already resolved here (the trax ``dis*`` aliases negate before the wire), so
    this is the final stored sign.
    """

    artifact_id: uuid.UUID
    """The citing Artifact's id (the edge's stored from-side)."""

    artifact_kind: Artifact.Kind
    """The citing Artifact's declared kind, verified against the stored row.

    ``Artifact.Kind`` deliberately includes ``Belief`` / ``Experiment``: a claim
    is itself an Artifact, so it may also *cite* another claim (e.g. one Belief
    cited as evidence for another). The schema edge CHECK admits this -- the
    citation target stays ``{Belief, Experiment}`` while the citer may be any
    Artifact kind, claims included."""

    valence: float = Field(default=CITATION_VALENCE_DEFAULT, ge=-1.0, le=1.0)
    """Signed evidential weight: positive for, negative against, magnitude
    strength. Defaults to :data:`CITATION_VALENCE_DEFAULT` (mild support)."""


class SubmitBelief(SubmitBase):
    """Submit body for a new Belief."""

    kind: Literal["Belief"] = "Belief"
    judgement: Belief.Judgement | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    proved_by: list[Citation] = Field(default_factory=list)
    """Load-bearing citations: each Artifact citing this belief as proof. Stamps
    an Artifact -> Belief ``proves`` edge carrying the signed valence (positive
    proves, negative disproves)."""
    favored_by: list[Citation] = Field(default_factory=list)
    """Context citations: each Artifact citing this belief as context. Stamps an
    Artifact -> Belief ``favors`` edge carrying the signed valence (positive
    favors, negative disfavors)."""

    @field_validator("proved_by", "favored_by", mode="after")
    @classmethod
    def _dedupe_citations(cls, value: list[Citation]) -> list[Citation]:
        # One edge per citing artifact is the stored truth; a repeated
        # artifact_id would drive an insert-then-upsert phantom audit.
        return _dedupe_preserving_order(value, key=lambda c: c.artifact_id)


class SubmitCodeChange(SubmitBase):
    """Submit body for a new CodeChange."""

    kind: Literal["CodeChange"] = "CodeChange"
    sha: str | None = None
    """Git SHA."""

    @field_validator("sha", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return _blank_scalar_to_none(value)


class SubmitWebResult(SubmitBase):
    """Submit body for a new WebResult."""

    kind: Literal["WebResult"] = "WebResult"
    url: str | None = None
    """Page URL."""

    @field_validator("url", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return _blank_scalar_to_none(value)


class SubmitWebSearch(SubmitBase):
    """Submit body for a new WebSearch."""

    kind: Literal["WebSearch"] = "WebSearch"
    query: str | None = None
    provider: str | None = None

    @field_validator("query", "provider", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return _blank_scalar_to_none(value)


class SubmitAgentSession(SubmitBase):
    """Submit body for a new AgentSession (a captured ``trax run`` session)."""

    kind: Literal["AgentSession"] = "AgentSession"
    cli: str | None = Field(default=None, min_length=1)
    """Wrapped CLI: ``claude`` / ``gemini`` / ``codex`` / ``cursor``."""

    cli_session_id: str | None = Field(default=None, min_length=1)
    """The CLI's own session id, for correlation with vendor records."""

    started: datetime | None = None
    # No ``ended`` at create: a session is born live (``ended IS NULL``).
    # ``ended`` is stamped only by ``POST /api/sessions/{id}/end``, which sets
    # it together with ``status = 'complete'`` -- the lifecycle CHECK on
    # ``inquiries`` forbids the (ended, non-complete) zombie a create-time
    # ``ended`` would have minted.

    rooms: list[str] | None = None
    """Initial room membership (``--room``); namespaces the session can be
    addressed within. See :attr:`AgentSession.rooms`."""

    @field_validator("cli", "cli_session_id", mode="after")
    @classmethod
    def _reject_blank_scalars(cls, value: str | None) -> str | None:
        """Reject a whitespace-only ``cli`` / ``cli_session_id``.

        ``min_length=1`` alone admits ``"   "``; both are matched/stored
        verbatim, so a whitespace value is a client bug (mirrors the rooms and
        wire_sessions blank-rejection rule).
        """
        if value is not None and not value.strip():
            raise ValueError("value must be non-empty")
        return value

    @field_validator("rooms", mode="after")
    @classmethod
    def _validate_rooms(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_room_names(value)


# Discriminated union over every concrete ``SubmitX`` body, keyed on the
# ``kind`` Literal so Pydantic picks the matching model automatically.
SubmitItem = Annotated[
    SubmitIssue
    | SubmitArtifact
    | SubmitExperiment
    | SubmitPaper
    | SubmitBelief
    | SubmitCodeChange
    | SubmitWebResult
    | SubmitWebSearch
    | SubmitAgentSession,
    Field(discriminator="kind"),
]


class BatchEdge(BaseModel):
    """One edge in a :class:`SubmitBatch`.

    Each endpoint is named EITHER by batch item index (``from_index`` /
    ``to_index``, 0-based into ``items``) for a row created in this batch,
    OR by an existing row's ``from_id`` / ``to_id`` UUID. Exactly one of
    index/id must be set per endpoint. Index references are the only way to
    link two brand-new rows -- inline-created targets have no id until the
    batch commits. Annotations mirror :class:`CreateEdge`.
    """

    from_index: int | None = Field(default=None, ge=0)
    from_id: uuid.UUID | None = None
    to_index: int | None = Field(default=None, ge=0)
    to_id: uuid.UUID | None = None
    edge_kind: Edge.Kind
    priority: Issue.Priority | None = Field(default=None, ge=0)
    note: str = ""
    valence: float | None = Field(default=None, ge=-1.0, le=1.0)
    labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_per_endpoint(self) -> BatchEdge:
        if (self.from_index is None) == (self.from_id is None):
            raise ValueError("edge needs exactly one of from_index / from_id")
        if (self.to_index is None) == (self.to_id is None):
            raise ValueError("edge needs exactly one of to_index / to_id")
        return self


class SubmitBatch(BaseModel):
    """Batch-submit body: collapse N submit round-trips into one.

    Each item routes by its ``kind`` discriminator just like an
    individual submit route. The batch is ALL-OR-NOTHING: every item and
    every edge is created in one shared transaction, so a single failure
    rolls the whole batch back and no row or edge is persisted. Per-item
    idempotency keys are required so a retried batch re-probes and returns
    the originally created ids instead of duplicating rows.

    ``edges`` wire newly-created rows together by item index, so one
    request can create a row plus an inline-created neighbour and link
    them atomically -- the case a flat list of rows cannot express.
    """

    items: list[SubmitItem] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)
    edges: list[BatchEdge] = Field(default_factory=list, max_length=BATCH_MAX_ITEMS)

    @field_validator("items", mode="after")
    @classmethod
    def _require_idempotency_keys(cls, value: list[SubmitItem]) -> list[SubmitItem]:
        missing = [i for i, item in enumerate(value) if item.idempotency_key is None]
        if missing:
            raise ValueError(
                "submit_batch items require idempotency_key for retry-safe "
                f"idempotent replay; missing indexes {missing}"
            )
        return value

    @field_validator("items", mode="after")
    @classmethod
    def _reject_duplicate_idempotency_keys(
        cls, value: list[SubmitItem]
    ) -> list[SubmitItem]:
        """Reject items sharing one idempotency_key, naming the dup indexes.

        ``submit_batch`` runs items on one transaction; a later item with a
        key an earlier item already wrote pre-probes, finds that earlier
        item's ``created`` change_log row, and returns its id -- collapsing
        two items into one row and mis-targeting any edges that reference the
        later item by index. Rejecting the collision here keeps it a clean
        422 instead of a silent collapse.
        """
        seen: dict[uuid.UUID, int] = {}
        duplicates: list[int] = []
        for i, item in enumerate(value):
            key = item.idempotency_key
            if key is None:  # missing keys reported by _require_idempotency_keys.
                continue
            if key in seen:
                duplicates.append(i)
            else:
                seen[key] = i
        if duplicates:
            raise ValueError(
                "submit_batch items must each carry a distinct "
                f"idempotency_key; duplicate idempotency_key at indexes {duplicates}"
            )
        return value

    @model_validator(mode="after")
    def _edge_indexes_in_range(self) -> SubmitBatch:
        n = len(self.items)
        for i, edge in enumerate(self.edges):
            for which, idx in (
                ("from_index", edge.from_index),
                ("to_index", edge.to_index),
            ):
                if idx is not None and idx >= n:
                    raise ValueError(
                        f"edge {i} {which}={idx} out of range for {n} items"
                    )
            self._check_new_row_endpoint_kinds(i, edge)
        return self

    def _check_new_row_endpoint_kinds(self, i: int, edge: BatchEdge) -> None:
        """Validate an index-endpoint edge's kinds against its edge policy.

        Only new-row endpoints (named by ``from_index`` / ``to_index``) carry a
        kind the wire can see -- ``items[idx].kind``. An incompatible
        ``(from_kind, to_kind, edge_kind)`` triple would otherwise pass the wire
        and surface as a mid-transaction DB ``CHECK`` 500; rejecting it here
        keeps it a clean 422. ``from_id`` / ``to_id`` endpoints reference
        already-stored rows whose kind the wire cannot resolve, so they are left
        to the Store's reference validation.
        """
        policy = EDGE_POLICIES[edge.edge_kind]
        self._check_endpoint_kind(i, edge, "from", edge.from_index, policy.from_kinds)
        self._check_endpoint_kind(i, edge, "to", edge.to_index, policy.to_kinds)

    def _check_endpoint_kind(
        self,
        i: int,
        edge: BatchEdge,
        side: str,
        idx: int | None,
        group: KindGroup,
    ) -> None:
        """Reject a new-row endpoint whose kind the edge policy forbids."""
        if idx is None:  # id endpoint: kind unknown at wire, left to the Store.
            return
        kind = self.items[idx].kind
        allowed = kind_group_members(group)
        if kind not in allowed:
            raise ValueError(
                f"edge {i} {side}_index={idx} is a {kind}, but edge_kind "
                f"{edge.edge_kind!r} requires its {side}-side in {list(allowed)}"
            )


# -- Field mutation bodies --------------------------------------------------
#
# Three uniform bodies cover every per-field inquiry mutation, keyed off
# the HTTP method:
#
# * PUT    -> FieldSet      (overwrite)
# * PATCH  -> FieldOp       (add / sub)
# * DELETE -> FieldMutation (unset)
#
# ``value`` is generic JSON. The field's concrete type lives in
# ``types/inquiries.py`` and is enforced by Store coercion and the SQL
# CHECK constraints (surfacing as HTTP 409 / 422), so one body shape
# serves every field and ``types`` stays the source of truth.


class ActorMixin(BaseModel):
    """Mixin: an ``actor`` provenance string for the audit row.

    ``actor`` is free-form (``"jvdillon"``, ``"claude-opus"``,
    ``"cron/nightly-import"``). ``None`` lets the route default to the
    authenticated principal's email; pass it explicitly to record a
    distinct provenance, such as an agent acting for a human.
    """

    actor: str | None = None


class FieldMutation(ActorMixin):
    """Base body for every per-field mutation: ``actor`` plus ``reason``.

    This is the whole body for DELETE (unset). PUT and PATCH extend it
    with ``value`` (and PUT adds ``expected`` for compare-and-set).
    """

    reason: str = ""


class FieldSet[T](FieldMutation):
    """PUT body: overwrite one field, or compare-and-set it.

    Generic over the field's value type ``T``. Each route binds
    ``FieldSet[<column type>]`` from ``types/inquiries.py``, so the HTTP
    boundary validates the value against the real column type (a bad value
    is a 422) without the body restating any field type.

    ``mode`` is the explicit intent discriminator:

    * ``"set"`` (default) -- blind overwrite; ``expected`` must be absent.
    * ``"cas"`` -- compare-and-set; ``expected`` is required and the route
      409s if the live value differs. Valid only on ``status`` and
      ``judgement`` (the route 400s elsewhere).

    The discriminator is what makes a typo'd guard a hard error instead of
    a silent blind write: ``extra="forbid"`` 422s an unknown field (e.g. a
    misspelled ``expected``), and a ``cas`` without ``expected`` (or a
    ``set`` carrying one) is rejected by :meth:`_check_mode`. Dispatching on
    a stray ``expected`` key, as the route once did, let either mistake
    silently degrade compare-and-set to overwrite.
    """

    model_config = ConfigDict(extra="forbid")

    value: T
    expected: T | None = None
    mode: Literal["set", "cas"] = "set"

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        """Couple ``mode`` to ``expected``: cas needs it, set forbids it."""
        has_expected = "expected" in self.model_fields_set
        if self.mode == "cas" and not has_expected:
            raise ValueError("mode='cas' requires 'expected'")
        if self.mode == "set" and has_expected:
            raise ValueError(
                "'expected' is only valid with mode='cas'; "
                "omit it for a blind set or pass mode='cas'"
            )
        return self


class FieldOp[T](FieldMutation):
    """PATCH body: augment one field.

    Generic over the element or delta type ``T``. ``op`` is ``add``
    (append to a list / add to a numeric axis) or ``sub`` (the inverse).
    ``value`` is the single element or numeric delta, validated against
    ``T`` at the boundary.
    """

    op: Literal["add", "sub"]
    value: T
