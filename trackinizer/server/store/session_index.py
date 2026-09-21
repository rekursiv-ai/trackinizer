"""Idempotent per-model HNSW index maintenance for ``session_embeddings``.

The ``session_embeddings.embedding`` column is dimension-free ``halfvec`` so
models of different native dims coexist; each model is searched through a PARTIAL
HNSW index casting the column to that model's fixed dim
(``(embedding::halfvec(N)) halfvec_cosine_ops WHERE model = '<slug>'``). The
seven shipped models' indexes live in the baseline + schema.023 (parity-gated).

:func:`ensure_model_index` creates a model's partial index on demand -- the sweep
and the backfill driver call it when they START maintaining a model -- so a
future model needs no new numbered migration. It is ``CREATE INDEX IF NOT
EXISTS`` with the SAME deterministic name and cast the baseline uses, so it
no-ops on the shipped seven and on a second call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import re


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = ["ensure_model_index", "index_name_for"]


# The stored ``model`` slug is code-controlled (a registry key or ``stub-<dim>``),
# never user input, but the value is interpolated into DDL that cannot bind an
# identifier or a partial-index predicate literal. This pattern is the contract:
# a slug outside it is a programming error, not a runtime input to sanitize away.
_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@-]*\Z")


def index_name_for(model: str) -> str:
    """Return the deterministic partial-index name for ``model``.

    The sanitization the baseline and schema.023 encode: every non-alphanumeric
    character becomes ``_`` and the result is lowercased, prefixed
    ``idx_session_embeddings_hnsw_``. Deterministic so ``CREATE INDEX IF NOT
    EXISTS`` keys correctly against the pre-created shipped indexes and the parity
    test can assert exact names.

    Args:
      model: The stored model slug (a registry key or ``stub-<dim>``).

    Returns:
      name: The index identifier for ``model``'s partial HNSW index.

    """
    if not _SLUG_RE.match(model):
        raise ValueError(f"model slug {model!r} is not a valid index-name source")
    sanitized = re.sub(r"[^a-z0-9]+", "_", model.lower())
    return f"idx_session_embeddings_hnsw_{sanitized}"


async def ensure_model_index(conn: Conn, model: str, dim: int) -> None:
    """Create ``model``'s partial HNSW index if it does not already exist.

    Idempotent: ``CREATE INDEX IF NOT EXISTS`` with the deterministic name, cast,
    and predicate the baseline uses, so it no-ops on the seven shipped models and
    on a repeat call. Called by the sweep / backfill when they start maintaining
    a model, so a new model is searchable without a numbered migration.

    Args:
      conn: A database connection.
      model: The stored model slug keying the partial (``WHERE model = ...``).
      dim: The model's stored dimension; the column is cast to ``halfvec(dim)``
        so the index matches the query path's cast.

    """
    if not _SLUG_RE.match(model):
        raise ValueError(f"model slug {model!r} is not a valid index source")
    if dim < 1:
        raise ValueError(f"dim must be positive, got {dim}")
    name = index_name_for(model)
    # ``model`` matched ``_SLUG_RE`` (no quote/backslash) and ``dim`` is an int,
    # so this interpolation cannot break out of the DDL; identifiers and a
    # partial-index predicate literal cannot be bound parameters.
    await conn.execute(
        f"CREATE INDEX IF NOT EXISTS {name} "
        f"ON session_embeddings USING hnsw "
        f"((embedding::halfvec({dim})) halfvec_cosine_ops) "
        f"WHERE model = '{model}'",
    )
