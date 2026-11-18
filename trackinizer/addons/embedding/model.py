"""The interface a model file implements: texts in, unit vectors out.

A model file turns text into vectors and knows nothing else: not the sweep, not
the vector table, not search. The embedding add-on builds the one model its config
names (``EmbeddingAddon.Config.model``). The shipped default is
:class:`~trackinizer.addons.embedding.qwen3_0p6b.Qwen06BEmbedder`; a site's
own model is a class of this shape, named in the site's factory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable


if TYPE_CHECKING:
    from collections.abc import Sequence


__all__ = ["Embedder"]


@runtime_checkable
class Embedder(Protocol):
    """Turns documents and queries into unit vectors of one fixed width.

    Batch-first: the sweep hands :meth:`embed_batch` as many documents as its
    config allows in one call, and search hands :meth:`embed_query` one query.

    Both methods are coroutines and must not block the event loop: a model that
    computes in-process runs its forward pass in a worker thread
    (``asyncio.to_thread``); a model that calls a server awaits the call.
    """

    dim: int
    """The width of every vector this model returns."""

    @property
    def name(self) -> str:
        """The model's identity, one input of the settings key on every vector row.

        A property so a model may compute it; a plain class attribute satisfies
        it too. Two models whose vectors differ must not share a name.
        """
        ...

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one document vector per text, in input order.

        Args:
          texts: Document texts, each non-empty; from one to tens of thousands.

        Returns:
          vectors: One unit-length vector of :attr:`dim` floats per text, in
            ``texts`` order.

        """
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Return a search query as a vector comparable with document vectors.

        A model whose card asks for a query instruction applies it here and never
        in :meth:`embed_batch`.

        Args:
          text: The search query.

        Returns:
          vector: One unit-length vector of :attr:`dim` floats.

        """
        ...
