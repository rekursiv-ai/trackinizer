"""Units: the texts a source contributes to the vector table, and the settings key.

A session record of a configured kind contributes its text; an inquiry of a
configured kind contributes its configured fields joined into one text; an edge's
note is a text of the inquiry the edge starts from. A text is cut into overlapping
chunks when long; each chunk is one unit and gets one vector. Every vector row
carries the key of the settings it was made under (:func:`settings_key`), so a
change to any setting makes every row stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import hashlib


if TYPE_CHECKING:
    from collections.abc import Sequence


__all__ = [
    "IndexUnit",
    "Settings",
    "settings_key",
    "text_units",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexUnit:
    """One span of text that gets one vector, addressed back to its source.

    Attributes:
      text: The span. Never empty: a source with no text has no unit.
      field: What the span is of its owner: ``"content"`` for a session record's
        text, ``"text"`` for an inquiry's joined fields, and
        ``"edge:<edge_kind>:<to_id>"`` for an edge's note.
      chunk: Zero-based chunk ordinal when the text splits; ``0`` when it fits.

    """

    text: str
    field: str
    chunk: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """What every vector row is made under.

    Attributes:
      key: :func:`settings_key` of the rest and the model's name; stored on
        every row.
      dim: The model's vector width.
      chunk_chars: Characters per chunk of a long text.
      overlap_chars: Characters adjacent chunks share.
      session_kinds: The session record kinds that get vectors.
      inquiry_fields: Per inquiry kind, the columns joined into its one text, in
        order; none, and no inquiry gets a vector.
      edge_notes: Whether each edge's note gets a vector.

    """

    key: str
    dim: int
    chunk_chars: int
    overlap_chars: int
    session_kinds: tuple[str, ...]
    inquiry_fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    edge_notes: bool = False


def settings_key(
    *,
    model: str,
    dim: int,
    chunk_chars: int,
    overlap_chars: int,
    session_kinds: Sequence[str],
    inquiry_fields: Sequence[tuple[str, Sequence[str]]] = (),
    edge_notes: bool = False,
) -> str:
    """Return the key every vector written under these settings carries.

    Each input changes what a vector is, so each is in the key: a change to any
    makes every row stale. The float type is the column's, ``halfvec``, written in
    so a future change of column re-keys too. Kinds are sorted; an inquiry kind's
    fields are not, since their order is the order they are joined in.

    Args:
      model: The model's name.
      dim: Its vector width.
      chunk_chars: Characters per chunk of a long text.
      overlap_chars: Characters adjacent chunks share.
      session_kinds: The session record kinds that get vectors.
      inquiry_fields: Per inquiry kind, the columns joined into its one text.
      edge_notes: Whether each edge's note gets a vector.

    Returns:
      key: 16 hex digits of the SHA-256 of the settings, safe to write into an
        index name and a partial-index predicate.

    """
    described = "\n".join(
        (
            f"model={model}",
            f"dim={dim}",
            "float=halfvec",
            f"chunk={chunk_chars}",
            f"overlap={overlap_chars}",
            f"sessions={','.join(sorted(session_kinds))}",
            *(
                f"inquiry.{kind}={'+'.join(fields)}"
                for kind, fields in sorted(inquiry_fields)
            ),
            f"edge_notes={edge_notes}",
        ),
    )
    return hashlib.sha256(described.encode("utf-8")).hexdigest()[:16]


def text_units(text: str, *, field: str, settings: Settings) -> tuple[IndexUnit, ...]:
    """Split a source's text into overlapping chunks; one unit when it fits.

    Args:
      text: The source's text.
      field: What the text is of its owner (:attr:`IndexUnit.field`).
      settings: Its chunk size and overlap.

    Returns:
      units: In chunk order; none for an empty text.

    """
    if len(text) <= settings.chunk_chars:
        return (IndexUnit(text=text, field=field),) if text else ()
    step = settings.chunk_chars - settings.overlap_chars
    return tuple(
        IndexUnit(
            text=text[start : start + settings.chunk_chars],
            field=field,
            chunk=ordinal,
        )
        for ordinal, start in enumerate(range(0, len(text), step))
    )
