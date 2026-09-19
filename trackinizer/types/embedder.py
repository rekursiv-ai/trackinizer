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

    **Optional attribute:** ``is_semantic: bool``. Set it ``True`` on a real
    embedding model to declare that its vectors carry MEANING, which is what
    ``Store.find_similar`` requires -- it reads the flag via ``getattr`` with a
    ``False`` default and refuses to rank against an embedder that does not
    claim it. A hash-based stand-in (:class:`~...store.core.StubEmbedder`)
    satisfies the column's dimension and NOT NULL constraints and keeps the
    write path exercised, but paraphrases land in unrelated directions, so any
    ranking over its vectors is arbitrary.

    It is deliberately NOT declared as a member above: this Protocol is
    ``@runtime_checkable``, and ``isinstance`` against such a Protocol requires
    every declared member to be present, so adding one would reject every
    existing implementer that predates it.
    """

    name: str

    dim: int

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a unit vector of length :attr:`dim`.

        Args:
          text: Text.

        Returns:
          result: The list[float].

        """
        ...
