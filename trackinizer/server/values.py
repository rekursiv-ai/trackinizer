"""Value-shape helpers used by the setter dispatch and write path.

Pure functions of the value -- no database connection needed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import cast


__all__ = [
    "byline_strs",
    "canonical_strs",
    "empty_optional_to_none",
    "list_or_none",
    "manifest_bound",
    "vec_to_text",
    "vetted_sql",
]


def manifest_bound(alias: str) -> tuple[str, str]:
    """Return the SQL to bound a record scan by its part's live manifest prefix.

    A compaction-restart can SHRINK a part (``session_manifests.records`` drops),
    leaving tail rows ``idx >= records`` inert -- never deleted, so every reader
    MUST exclude them (``schema.sql`` states the contract: readers take
    ``idx < records``). This returns the two fragments a record-reading query
    splices: an INNER JOIN to ``session_manifests`` and the ``idx < m.records``
    predicate. The JOIN is inner on purpose -- a part whose manifest is missing
    has no bound and drops to empty, which is the "reads as empty" rule rather
    than a branch (see the reference site ``session_ir.read_session_records``).

    Args:
      alias: The record-row table alias in the caller's query (e.g. ``"r"``);
        the manifest is always aliased ``m``.

    Returns:
      join: The ``JOIN session_manifests m ON ...`` clause (place after FROM).
      predicate: The ``<alias>.idx < m.records`` clause (AND into the WHERE).

    """
    join = (
        f"JOIN session_manifests m "
        f"ON m.session_id = {alias}.session_id AND m.part = {alias}.part "
    )
    predicate = f"{alias}.idx < m.records"
    return join, predicate


def vetted_sql(*parts: str) -> str:
    """Concatenate SQL fragments whose dynamic pieces are closed-set, not user input.

    Every caller assembles a query from a static template plus fragments derived
    from closed sets (column names from ``COLUMN_SPECS``, clause lists built from
    bound ``$N`` placeholders, sequence names from ``SEQ_FOR_KIND``) -- never from
    user input. Passing the dynamic pieces as separate arguments here, rather than
    interpolating them into an f-string literal, keeps the SQL-injection lint
    (ruff S608) satisfied in ONE vetted place instead of a suppression at every
    call site. The values still flow to the DB as bound parameters.

    Args:
      *parts: SQL text fragments from closed-set sources.

    Returns:
      query: Concatenated SQL string.

    """
    return "".join(parts)


def empty_optional_to_none(value: object) -> object:
    """Collapse an empty value to ``None`` so "unset" has one encoding: NULL.

    The single rule for nullable columns at the storage boundary: an empty
    string, a whitespace-only string, or an empty sequence is *absence*, so
    it stores SQL NULL rather than an empty sentinel (``''`` / ``'{}'``).
    A falsy-but-valid scalar (``0`` priority, ``0.0`` confidence) is not a
    string or sequence, so it passes through untouched. The caller decides
    whether a column is nullable; this only normalizes the empty shape.

    Args:
      value: Object from a nullable column or form field.

    Returns:
      normalized: None for empty strings/sequences, value unchanged otherwise.

    """
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, Sequence) and len(value) == 0:
        return None
    return value


def vec_to_text(vec: Sequence[float]) -> str:
    """Format a vector as the pgvector text input form: ``[v1,v2,...]``.

    Args:
      vec: Vec.

    Returns:
      result: The str.

    """
    return "[" + ",".join(repr(x) for x in vec) + "]"


def list_or_none[T](value: Sequence[T] | None) -> list[T] | None:
    """Pass ``None`` through; materialize any other sequence as ``list``.

    Args:
      value: Sequence or None.

    Returns:
      result: list(value) if value is not None, else None.

    """
    return None if value is None else list(value)


def byline_strs[T: str](values: Iterable[T]) -> tuple[T, ...]:
    """Normalize a byline: strip per element, drop blanks, keep order + dups.

    The one rule for ``paper_authors`` on BOTH the submit and the edit path
    (unlike :func:`canonical_strs`, which also dedups): order and a genuinely
    repeated author are significant, but a blank/whitespace element is absence
    and is dropped. Sharing this normalizer keeps ``submit(["A","A"])`` and
    ``set_authors(("A","A"))`` on one contract.

    Args:
      values: Author names (each may be whitespace-padded).

    Returns:
      authors: Tuple of stripped names in order, blanks removed, dups kept.

    """
    return tuple(s for s in (cast(T, v.strip()) for v in values) if s)


def canonical_strs[T: str](values: Iterable[T]) -> tuple[T, ...]:
    """Canonicalize a free-form string list: strip, drop blanks, dedup.

    Insertion order preserved; generic over the element type so callers
    passing typed literals (``Issue.Kind``, ``Inquiry.Actor``) keep their
    narrowing. Applied uniformly to every list-valued inquiries column
    (``labels``, ``subscribers``, ``issue_kind``) at the storage boundary
    so wire input and DB state agree on canonical form.

    Args:
      values: Strings to canonicalize (each may be whitespace-padded).

    Returns:
      unique: Tuple of stripped, deduplicated strings in insertion order.

    """
    seen: dict[T, None] = {}
    for v in values:
        stripped = cast(T, v.strip())
        if stripped:
            seen.setdefault(stripped, None)
    return tuple(seen)
