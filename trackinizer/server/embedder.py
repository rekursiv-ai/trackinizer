"""Embedding implementations used by the Trackinizer server."""

from __future__ import annotations

from collections.abc import Iterator

import hashlib


__all__ = ["StubEmbedder"]


class StubEmbedder:
    """Deterministic hash-based embedder for tests and offline bootstrap."""

    name = "stub"
    dim = 384
    """Matches ``vector(384)`` on the ``inquiry_embeddings`` table."""

    async def embed(self, text: str) -> list[float]:
        """Embed ``text`` into a vector."""
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        rng = _xorshift_floats(int.from_bytes(seed[:8], "little") or 1)
        vec = [next(rng) for _ in range(self.dim)]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


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
