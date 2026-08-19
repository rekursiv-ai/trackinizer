""":class:`_StoreShared` -- the concrete base every ``Store`` mixin inherits.

Holds the shared instance state (``engine``, ``embedders``,
``_last_used_bumped_at``) plus the construction and embedding primitives every
mixin relies on. Cross-mixin *method* visibility is provided by the mixin
inheritance chain (each mixin inherits the mixin whose methods it calls), so
this base only needs to own the genuinely shared state and the leaf
:meth:`_embed_all` helper -- no stubs, no ``Any``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

import asyncio

from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.auth import AuthIdentity
from trackinizer.types.embedder import Embedder


__all__ = [
    "EMBEDDING_DIM",
    "_StoreShared",
]


EMBEDDING_DIM: Final = 384  # MiniLM-L6-v2 sentence-embedding dim.


class _StoreShared:
    """Shared instance state + embedding helper for every ``Store`` mixin.

    The composed :class:`Store` is built from mixins split by concern; each
    inherits this base so ``self.engine`` / ``self.embedders`` and the
    :meth:`_embed_all` helper resolve uniformly. Cross-mixin method calls
    resolve through the mixin inheritance chain, not through this base.
    """

    def __init__(
        self,
        engine: DatabaseEngine,
        embed: Embedder | Sequence[Embedder],
    ) -> None:
        """Construct a Store bound to ``engine`` with one or more embedders.

        Multiple embedders populate ``inquiry_embeddings`` in parallel per
        submit/edit; each needs a unique ``name`` and ``dim == EMBEDDING_DIM``,
        matching ``vector(384)`` on the embeddings table.

        Validating embedders here turns what would otherwise be opaque
        mid-transaction failures (NOT NULL / UNIQUE / pgvector dim mismatch)
        into a ``ValueError`` at construction time.

        Raises:
          ValueError: ``embed`` is empty, two embedders share a ``name``, or
            any embedder's ``dim`` differs from ``EMBEDDING_DIM``.

        """
        # The runtime_checkable Protocol narrows a single Embedder; a str,
        # bytes, or other Sequence-shaped non-Embedder falls through to
        # ``tuple(embed)`` and is caught by the validation below.
        self.embedders: tuple[Embedder, ...] = (
            (embed,) if isinstance(embed, Embedder) else tuple(embed)
        )
        if not self.embedders:
            raise ValueError("Store requires at least one embedder.")
        names = [e.name for e in self.embedders]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"Embedder names must be unique; duplicates: {duplicates}."
            )
        bad_dim = [(e.name, e.dim) for e in self.embedders if e.dim != EMBEDDING_DIM]
        if bad_dim:
            raise ValueError(
                f"All embedders must have dim={EMBEDDING_DIM}; got"
                f" mismatched (name, dim) pairs: {bad_dim}."
            )
        self.engine = engine
        # Throttle for ``api_keys.last_used_at`` UPDATE coalescing; see
        # :meth:`should_bump_api_key_last_used`. Per-instance so separate
        # Stores keep independent bookkeeping and the memory releases when
        # the Store is collected.
        self._last_used_bumped_at: dict[UUID, float] = {}
        # Verified-bearer cache; see :meth:`cached_bearer_identity`. Keyed by
        # sha256 of the presented secret so no plaintext token is held in
        # memory. Per-instance for the same reasons as the throttle above.
        self._verified_bearers: dict[bytes, tuple[AuthIdentity, float]] = {}

    async def _embed_all(self, text: str) -> list[tuple[str, list[float]]]:
        """Embed ``text`` with every registered embedder in parallel.

        Returns ``(embedder.name, vector)`` pairs in registration order.
        """
        vecs = await asyncio.gather(*(e.embed(text) for e in self.embedders))
        return [(e.name, v) for e, v in zip(self.embedders, vecs, strict=True)]
