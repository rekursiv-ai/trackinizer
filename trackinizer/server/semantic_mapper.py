"""The semantic-mapping protocol: which text a record contributes to search.

The footprint decision (``docs/private/session_indexing.md``) is POLICY --
which kinds are indexed, head sizes, chunking -- and policy will be iterated
on. This module fixes the CONTRACT so alternatives swap in without touching
the store: a :class:`SemanticMapper` turns one stored record into zero or
more :class:`IndexUnit` values, and everything downstream (tsvector scoping,
``session_embeddings`` rows, backfill, live ingest) consumes units, never
records.

The same shape serves inquiries: title and description are two units of one
row (``field`` distinguishes them), which is what lets query-time scoring
weight them separately instead of fusing the text before embedding.

The mirror of :class:`~trackinizer.server.embedders.stub.StubEmbedder` /
``Embedder``: that protocol answers "text -> vector"; this one answers
"record -> which texts, under which addresses".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


__all__ = [
    "IndexUnit",
    "SemanticMapper",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexUnit:
    """One indexable span of text, addressed back to what produced it.

    Attributes:
      text: The span to index. Never empty -- a mapper emits no unit rather
        than an empty one.
      field: What the span IS of its source: ``"content"`` for a message
        body, ``"head"`` for a truncated tool-result head, ``"title"`` /
        ``"description"`` for inquiry fields. Query-time scoring may weight
        fields differently; storage keys on it.
      chunk: Zero-based chunk ordinal when one field splits across units
        (overlapping description chunks); ``0`` when the field fits whole.
      fts: Whether the span joins the term-search (tsvector) surface.
      embed: Whether the span gets a vector. A unit may be either or both --
        tool-result heads are ``fts`` and ``embed``; a long description
        chunk is ``embed`` only when its terms already ride on chunk 0.

    """

    text: str
    field: str = "content"
    chunk: int = 0
    fts: bool = True
    embed: bool = True


@runtime_checkable
class SemanticMapper(Protocol):
    """Map one stored session record to its indexable units.

    Implementations are pure and total: any record of any kind is accepted,
    and "not indexed" is the empty tuple, never an exception. The record
    arrives as its stored projection -- ``kind`` plus the ``text`` column the
    ingest already computed (``types/session_records.py::search_text``) --
    so a mapper never decodes payloads and never touches the database.
    """

    name: str
    """Identifies the policy, stored beside vectors so a policy change is
    distinguishable from a model change (``session_embeddings`` keys on
    ``(model, mapper)``)."""

    def units(self, *, kind: str, text: str) -> tuple[IndexUnit, ...]:
        """Return the indexable units of one record, possibly none.

        Args:
          kind: The ``session_records.kind`` (a record class name).
          text: The row's stored search projection.

        Returns:
          units: Zero or more spans to index, in field-then-chunk order.

        """
        ...
