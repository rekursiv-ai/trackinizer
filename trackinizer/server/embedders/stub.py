"""Embedding implementations used by the Trackinizer server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import hashlib


if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = ["StubEmbedder"]


class StubEmbedder:
    """Deterministic hash-based embedder for tests and offline bootstrap.

    Args:
      dim: Vector length. Defaults to 384 (``inquiry_embeddings``'s
        ``vector(384)``); pass 1024 for the ``session_embeddings``
        ``halfvec(1024)`` surface, whose sweep takes its own embedder rather
        than the Store's 384-dim one.

    """

    dim = 384
    """Default vector length, matching ``vector(384)`` on ``inquiry_embeddings``.
    A class attribute so ``StubEmbedder.dim`` resolves without an instance;
    ``__init__`` overrides it per-instance for the 1024-dim session surface."""

    def __init__(self, *, dim: int = 384) -> None:
        self.dim = dim

    @property
    def name(self) -> str:
        """Stored model identity: ``"stub"`` at 384, ``"stub-<dim>"`` otherwise.

        The 384 default MUST stay the literal ``"stub"``: it keys existing
        ``inquiry_embeddings`` rows (``model = 'stub'``), and a rename would
        orphan them. A non-default dim carries its width so a 384 stub and a
        1024 stub are distinguishable by name -- the config knob ``stub-1024``
        and the DB identity then agree.
        """
        return "stub" if self.dim == 384 else f"stub-{self.dim}"

    async def embed(self, text: str) -> list[float]:
        """Embed ``text`` into a vector."""
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        rng = _xorshift_floats(int.from_bytes(seed[:8], "little") or 1)
        vec = [next(rng) for _ in range(self.dim)]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    # Identical to ``embed`` for the stub: the corpus/query asymmetry a real
    # model needs (QwenEmbedder's instruction prefix) is meaningless for a hash,
    # since the same text must hash to the same vector on both sides. Present so
    # the stub satisfies the query-embedder contract the search route depends on.
    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query; identical to :meth:`embed` for the stub."""
        return await self.embed(text)


# Used by :class:`StubEmbedder` to produce stable per-text vectors without depending on
# numpy / random's global state.
def _xorshift_floats(seed: int) -> Iterator[float]:
    """Deterministic ``uint64 -> float64 in [-1, 1)`` generator."""
    state = seed & ((1 << 64) - 1) or 1
    while True:
        state ^= (state << 13) & ((1 << 64) - 1)
        state ^= state >> 7
        state ^= (state << 17) & ((1 << 64) - 1)
        # Map the top 53 bits to a double in [0, 1), then scale to [-1, 1).
        yield (state >> 11) * (2.0 / (1 << 53)) - 1.0
