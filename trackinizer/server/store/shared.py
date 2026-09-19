""":class:`_StoreShared` -- the concrete base every ``Store`` mixin inherits.

Holds the shared instance state (``engine``, ``embedders``,
``_last_used_bumped_at``) plus the construction and embedding primitives every
mixin relies on. Cross-mixin *method* visibility is provided by the mixin
inheritance chain (each mixin inherits the mixin whose methods it calls), so
this base only needs to own the genuinely shared state and the leaf
:meth:`_embed_all` helper -- no stubs, no ``Any``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

from trackinizer.types.embedder import Embedder


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.auth import AuthIdentity


__all__ = [
    "_StoreShared",
    "embeddable_text",
]


def embeddable_text(title: str, description: str | None) -> str:
    """Compose the text an inquiry is indexed by.

    One function so every write path -- submit, title edit, description edit --
    produces byte-identical input for the same row. If they diverged, a row's
    stored vector would depend on which field was touched last.

    Joined with ". " rather than a newline: the separator is tokenized by the
    embedder, and sentence-ending punctuation is what its training data uses
    between a heading and its body.
    """
    body = (description or "").strip()
    head = title.strip()
    if not body:
        return head
    return f"{head}. {body}" if not head.endswith((".", "!", "?")) else f"{head} {body}"


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
        *,
        embedding_dim: int = 384,
    ) -> None:
        """Construct a Store bound to ``engine`` with one or more embedders.

        Multiple embedders populate ``inquiry_embeddings`` in parallel per
        submit/edit; each needs a unique ``name`` and ``dim == embedding_dim``
        (default 384, matching ``vector(384)`` on the embeddings table).

        Validating embedders here turns what would otherwise be opaque
        mid-transaction failures (NOT NULL / UNIQUE / pgvector dim mismatch)
        into a ``ValueError`` at construction time.

        Raises:
          ValueError: ``embed`` is empty, two embedders share a ``name``, or
            any embedder's ``dim`` differs from ``embedding_dim``.

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
                f"Embedder names must be unique; duplicates: {duplicates}.",
            )
        bad_dim = [(e.name, e.dim) for e in self.embedders if e.dim != embedding_dim]
        if bad_dim:
            raise ValueError(
                f"All embedders must have dim={embedding_dim}; got"
                f" mismatched (name, dim) pairs: {bad_dim}.",
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

    # Returns ``(embedder.name, vector)`` pairs in registration order.
    async def _embed_all(self, text: str) -> list[tuple[str, list[float]]]:
        """Embed ``text`` with every registered embedder in parallel."""
        vecs = await asyncio.gather(*(e.embed(text) for e in self.embedders))
        return [(e.name, v) for e, v in zip(self.embedders, vecs, strict=True)]

    async def _embed_inquiry(
        self,
        title: str,
        description: str | None,
    ) -> list[tuple[str, list[float]]]:
        """Embed an inquiry's searchable text: title AND description.

        ``docs/design.md`` specifies "embedding search over title/description",
        and the description is where the substance lives -- a title states the
        claim ("x10 overfits past step 12.5k"), the description carries the
        reasoning and the numbers that distinguish it from its neighbours.
        Titles alone are short and, within one investigation, topically almost
        identical, so they discriminate poorly.

        Measured on 1702 papers with a 384-dim model: indexing title alone
        retrieved the right row 64% of the time at top-1; title plus
        description, 81%. The lexical baseline moved further still (49% to
        88%). It is the single largest retrieval win available here, ahead of
        model choice.
        """
        return await self._embed_all(embeddable_text(title, description))
