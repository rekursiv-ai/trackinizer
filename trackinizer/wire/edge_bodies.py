"""Request bodies for edge mutation routes.

Edge identity (``from_id``, ``edge_kind``, ``to_id``) lives in the URL
path, so these bodies carry only the payload. Per-field edge edits reuse
the inquiry field bodies; the only edge-specific bodies are create (the
annotations sent at create time) and batch create, which must restate
identity per item since one URL serves the whole batch.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Issue
from trackinizer.wire.bodies import (
    BATCH_MAX_ITEMS,
    ActorMixin,
)


class CreateEdge(ActorMixin):
    """Edge-create body: optional annotations.

    All fields are optional; a bare ``{}`` creates an unannotated edge.
    """

    priority: Issue.Priority | None = Field(default=None, ge=0)
    note: str = ""
    valence: float | None = Field(default=None, ge=-1.0, le=1.0)
    labels: list[str] | None = None
    """Edge labels. ``None`` means unset; the route coerces it to the
    empty list before calling the Store."""
    reason: str = ""


class CreateEdgeItem(CreateEdge):
    """One :class:`CreateEdgeBatch` item, with edge identity in the body."""

    from_id: uuid.UUID
    to_id: uuid.UUID
    edge_kind: Edge.Kind


class CreateEdgeBatch(BaseModel):
    """Batch-create body: add many edges in one round-trip.

    Each item runs in its own transaction and the batch is fail-stop: on
    the first item that fails, the edges already created stay committed,
    the failing item reports its error, and every remaining item is
    skipped (not attempted). Handy for attaching several edges at once.
    """

    items: list[CreateEdgeItem] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)
