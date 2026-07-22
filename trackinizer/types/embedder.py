"""The Embedder Protocol: turns text into a fixed-length unit vector."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Maps text to a fixed-dimension unit vector.

    ``name`` tags the rows an embedder writes to ``inquiry_embeddings``, so
    several embedders can coexist for one inquiry. The Protocol is
    ``@runtime_checkable`` so ``Store.__init__`` can ``isinstance``-narrow an
    ``Embedder | Sequence[Embedder]`` argument.
    """

    name: str
    dim: int

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a unit vector of length :attr:`dim`."""
        ...
