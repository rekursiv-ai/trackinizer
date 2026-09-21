"""The Embedder Protocol: turns text into a fixed-length unit vector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable


if TYPE_CHECKING:
    import torch


class ModelOutput(Protocol):
    """The one field of a transformer forward output the poolers read.

    Shared by the live embedder (:mod:`trackinizer.server.embedders.qwen_family`)
    and the batch backfill core
    (:mod:`trackinizer.server.tools.bucket_embed`); both call ``model(**batch)``
    and read ``last_hidden_state`` to last-token-pool. Defined here (not in
    either caller) so the two share one contract rather than duplicating it.
    """

    last_hidden_state: torch.Tensor


@runtime_checkable
class Embedder(Protocol):
    """Maps text to a fixed-dimension unit vector.

    ``name`` tags the rows an embedder writes to ``inquiry_embeddings``, so
    several embedders can coexist for one inquiry. The Protocol is
    ``@runtime_checkable`` so ``Store.__init__`` can ``isinstance``-narrow an
    ``Embedder | Sequence[Embedder]`` argument.
    """

    dim: int

    @property
    def name(self) -> str:
        """Stored model identity, keying this embedder's rows.

        A read-only property (not a bare attribute) so an implementation may
        COMPUTE it -- ``StubEmbedder`` derives ``stub`` / ``stub-<dim>`` from
        its dim -- while a plain class attribute (``QwenEmbedder.name``) still
        satisfies it.
        """
        ...

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a document unit vector of length :attr:`dim`.

        Args:
          text: Text.

        Returns:
          result: The list[float].

        """
        ...


@runtime_checkable
class QueryEmbedder(Embedder, Protocol):
    """An ``Embedder`` that also embeds search QUERIES.

    The session-search path needs a query-side embedding; a model with a
    corpus/query asymmetry (an instruction prefix) applies it in
    :meth:`embed_query` and NOT in :meth:`embed`. This is narrower than
    :class:`Embedder` on purpose: the inquiry-embedding path (``Store``) never
    queries, so its embedders need not implement this, and the session path
    (``build_session_embedder``) returns this type.
    """

    async def embed_query(self, text: str) -> list[float]:
        """Return a search query as a unit vector of length :attr:`dim`.

        Args:
          text: The search query.

        Returns:
          result: The list[float].

        """
        ...
