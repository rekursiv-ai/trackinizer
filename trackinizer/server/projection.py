"""Row + edges -> typed :class:`Inquiry` with relationship projections filled."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from trackinizer.lib.postgres import Conn
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import (
    CITATION_VALENCE_DEFAULT,
    KIND_TO_CLASS,
    Artifact,
    ArtifactEdge,
    Belief,
    Experiment,
    Inquiry,
    InquiryEdge,
    Issue,
    IssueEdge,
    Paper,
)


# ``KIND_TO_CLASS`` is the canonical registry defined in ``types/inquiries.py``;
# ``materialize`` imports it for its own use. It is NOT re-exported here -- every
# consumer imports it straight from ``types.inquiries`` (the definition module).


if TYPE_CHECKING:
    import asyncpg


async def fetch_edges(
    conn: Conn,
    subject_id: UUID,
) -> tuple[list[asyncpg.Record], list[asyncpg.Record]]:
    """Return the outbound and inbound edge rows touching ``subject_id``.

    Returns:
      outbound: Rows where ``subject_id`` is the ``from`` side, each carrying
        the ``to`` endpoint and the edge annotations.
      inbound: Rows where ``subject_id`` is the ``to`` side, each carrying the
        ``from`` endpoint and the edge annotations.

    """
    outbound = await conn.fetch(
        "SELECT to_id, to_kind, edge_kind, priority, note, valence, labels "
        "FROM edges WHERE from_id = $1 "
        "ORDER BY edge_kind, to_id",
        subject_id,
    )
    inbound = await conn.fetch(
        "SELECT from_id, from_kind, edge_kind, priority, note, valence, labels "
        "FROM edges WHERE to_id = $1 "
        "ORDER BY edge_kind, from_id",
        subject_id,
    )
    return list(outbound), list(inbound)


async def fetch_edges_bulk(
    conn: Conn,
    subject_ids: Sequence[UUID],
) -> tuple[dict[UUID, list[asyncpg.Record]], dict[UUID, list[asyncpg.Record]]]:
    """Outbound and inbound edges for many subjects, bucketed by id.

    Two queries regardless of list size -- avoids the N+1 fan-out that
    ``fetch_edges`` would produce when called per row in
    ``list_kind`` / ``proves_belief``. Each row in the returned
    buckets has the same shape as the corresponding ``fetch_edges`` list.

    Returns:
      outbound: ``subject_id -> outbound edge rows`` (the subject is the
        ``from`` side), one bucket per requested id (empty when none).
      inbound: ``subject_id -> inbound edge rows`` (the subject is the ``to``
        side), one bucket per requested id (empty when none).

    """
    outbound: dict[UUID, list[asyncpg.Record]] = {sid: [] for sid in subject_ids}
    inbound: dict[UUID, list[asyncpg.Record]] = {sid: [] for sid in subject_ids}
    if not subject_ids:
        return outbound, inbound
    ids = list(subject_ids)
    out_rows = await conn.fetch(
        "SELECT from_id, to_id, to_kind, edge_kind, priority, note, valence, labels "
        "FROM edges WHERE from_id = ANY($1::uuid[]) "
        "ORDER BY edge_kind, to_id",
        ids,
    )
    in_rows = await conn.fetch(
        "SELECT to_id, from_id, from_kind, edge_kind, priority, note, valence, labels "
        "FROM edges WHERE to_id = ANY($1::uuid[]) "
        "ORDER BY edge_kind, from_id",
        ids,
    )
    for r in out_rows:
        outbound[r["from_id"]].append(r)
    for r in in_rows:
        inbound[r["to_id"]].append(r)
    return outbound, inbound


def _matching(
    rows: Sequence[asyncpg.Record], edge_kind: Edge.Kind
) -> list[asyncpg.Record]:
    """Edge rows of one kind, in fetch order."""
    return [r for r in rows if r["edge_kind"] == edge_kind]


def _peer(
    row: asyncpg.Record,
    *,
    id_col: Literal["from_id", "to_id"],
    kind_col: Literal["from_kind", "to_kind"],
) -> tuple[UUID, Inquiry.InquiryKind, str | None, tuple[str, ...] | None]:
    """The peer id/kind and the branch-agnostic ``note`` / ``labels``."""
    labels = row["labels"]
    return (
        cast(UUID, row[id_col]),
        cast(Inquiry.InquiryKind, row[kind_col]),
        cast("str | None", row["note"]),
        None if labels is None else tuple(cast("Sequence[str]", labels)),
    )


def _inquiry_edges(
    rows: Sequence[asyncpg.Record],
    *,
    edge_kind: Edge.Kind,
    id_col: Literal["from_id", "to_id"],
    kind_col: Literal["from_kind", "to_kind"],
) -> tuple[InquiryEdge, ...]:
    """:class:`InquiryEdge` refs (note/labels only) for one edge kind."""
    return tuple(
        InquiryEdge(id=pid, kind=pkind, note=note, labels=labels)
        for r in _matching(rows, edge_kind)
        for pid, pkind, note, labels in (_peer(r, id_col=id_col, kind_col=kind_col),)
    )


def _issue_edges(
    rows: Sequence[asyncpg.Record],
    *,
    edge_kind: Edge.Kind,
    id_col: Literal["from_id", "to_id"],
    kind_col: Literal["from_kind", "to_kind"],
) -> tuple[IssueEdge, ...]:
    """:class:`IssueEdge` refs (carry contextual ``priority``) for one edge kind."""
    return tuple(
        IssueEdge(
            id=pid,
            kind=pkind,
            note=note,
            labels=labels,
            priority=cast("Issue.Priority | None", r["priority"]),
        )
        for r in _matching(rows, edge_kind)
        for pid, pkind, note, labels in (_peer(r, id_col=id_col, kind_col=kind_col),)
    )


def _artifact_edges(
    rows: Sequence[asyncpg.Record],
    *,
    edge_kind: Edge.Kind,
    id_col: Literal["from_id", "to_id"],
    kind_col: Literal["from_kind", "to_kind"],
) -> tuple[ArtifactEdge, ...]:
    """:class:`ArtifactEdge` refs (carry signed ``valence``) for one edge kind.

    A NULL stored valence (a legacy citation row written before the column was
    populated) reads as :data:`CITATION_VALENCE_DEFAULT`; a citation written
    through the current paths always carries a concrete value.
    """
    return tuple(
        ArtifactEdge(
            id=pid,
            kind=pkind,
            note=note,
            labels=labels,
            valence=(
                CITATION_VALENCE_DEFAULT
                if r["valence"] is None
                else cast(float, r["valence"])
            ),
        )
        for r in _matching(rows, edge_kind)
        for pid, pkind, note, labels in (_peer(r, id_col=id_col, kind_col=kind_col),)
    )


def materialize(
    row: asyncpg.Record,
    outbound_buckets: dict[UUID, list[asyncpg.Record]],
    inbound_buckets: dict[UUID, list[asyncpg.Record]],
) -> Inquiry:
    """Build a fully-projected Inquiry from a row + bulk edge buckets.

    Used by every list / lookup path so the projection-aware view that
    ``get_inquiry`` returns is the only view callers ever see -- no
    silently truncated fields on bulk responses.
    """
    cls = KIND_TO_CLASS[cast(Inquiry.InquiryKind, row["kind"])]
    base = cls.from_row(row)
    rid = cast(UUID, row["id"])
    return project_relationships(
        base, outbound_buckets.get(rid, []), inbound_buckets.get(rid, [])
    )


def project_relationships(
    base: Inquiry,
    outbound: list[asyncpg.Record],
    inbound: list[asyncpg.Record],
) -> Inquiry:
    """Fill ``Inquiry`` row-resident projections from edge rows.

    Every edge stores child -> parent, so a vertex's parent-pointing (forward)
    fields read its OUTBOUND ``to`` endpoints and its child-pointing (inverse)
    fields read its INBOUND ``from`` endpoints.
    """
    base = replace(
        base,
        # supersedes stored successor(child) -> predecessor(parent).
        supersedes=_inquiry_edges(
            outbound, edge_kind="supersedes", id_col="to_id", kind_col="to_kind"
        ),
        superseded_by=_inquiry_edges(
            inbound, edge_kind="supersedes", id_col="from_id", kind_col="from_kind"
        ),
        # produced_by stored produced(child) -> producer(parent). The forward
        # ``produced_by`` lists this vertex's producer parents (outbound to-side);
        # the inverse ``produces`` lists what it produced (inbound from-side).
        produced_by=_inquiry_edges(
            outbound, edge_kind="produced_by", id_col="to_id", kind_col="to_kind"
        ),
        produces=_inquiry_edges(
            inbound, edge_kind="produced_by", id_col="from_id", kind_col="from_kind"
        ),
    )
    if isinstance(base, Issue):
        base = replace(
            base,
            # narrows stored narrower(child) -> broader(parent): the forward
            # ``narrows`` lists the broader parents this issue narrows (outbound
            # to-side); the inverse ``narrowed_by`` lists its narrower children
            # (inbound from-side).
            narrows=_issue_edges(
                outbound, edge_kind="narrows", id_col="to_id", kind_col="to_kind"
            ),
            narrowed_by=_issue_edges(
                inbound, edge_kind="narrows", id_col="from_id", kind_col="from_kind"
            ),
            # requires stored requirer(child) -> prerequisite(parent).
            requires=_issue_edges(
                outbound, edge_kind="requires", id_col="to_id", kind_col="to_kind"
            ),
            required_by=_issue_edges(
                inbound, edge_kind="requires", id_col="from_id", kind_col="from_kind"
            ),
        )
    if isinstance(base, Artifact):
        # proves/favors stored evidence(child Artifact) -> claim(parent
        # Belief/Experiment). The forward view (claims this artifact cites) is
        # the artifact's OUTBOUND to-side.
        base = replace(
            base,
            proves=_artifact_edges(
                outbound, edge_kind="proves", id_col="to_id", kind_col="to_kind"
            ),
            favors=_artifact_edges(
                outbound, edge_kind="favors", id_col="to_id", kind_col="to_kind"
            ),
        )
    if isinstance(base, Belief | Experiment):
        # The inverse view (artifacts citing this claim) is its INBOUND
        # from-side. Only Belief and Experiment are citation targets.
        base = replace(
            base,
            proved_by=_artifact_edges(
                inbound, edge_kind="proves", id_col="from_id", kind_col="from_kind"
            ),
            favored_by=_artifact_edges(
                inbound, edge_kind="favors", id_col="from_id", kind_col="from_kind"
            ),
        )
    if isinstance(base, Paper):
        # cites_paper stored citing(child) -> cited(parent), Paper -> Paper. The
        # forward ``cites`` (this paper's bibliography) is its OUTBOUND to-side;
        # the inverse ``cited_by`` is its INBOUND from-side. Historical citation
        # carries no valence, so both use the base ``InquiryEdge`` (note/labels).
        base = replace(
            base,
            cites=_inquiry_edges(
                outbound, edge_kind="cites_paper", id_col="to_id", kind_col="to_kind"
            ),
            cited_by=_inquiry_edges(
                inbound, edge_kind="cites_paper", id_col="from_id", kind_col="from_kind"
            ),
        )
    return base
