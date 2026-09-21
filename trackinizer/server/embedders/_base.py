r"""Shared recipe for the Qwen3-Embedding family (Qwen 0.6B/4B/8B, Octen).

Every model in this family embeds identically: run the ONNX graph with
``padding_side="left"``, last-token pooling, Matryoshka-truncate the pooled
vector to :attr:`~QwenFamilyEmbedder.dim` and ONLY THEN L2-normalize (the slice
must precede the normalize so the truncated vector stays unit-norm), with the
corpus/query asymmetry the cards document: a QUERY is wrapped
``Instruct: <task>\nQuery:<text>`` while a DOCUMENT is embedded with an optional
per-model document prefix (bare for stock Qwen; ``"- "`` for Octen, see
:mod:`~trackinizer.server.embedders.octen_8b`).

Per-model modules (``qwen3_4b``, ``qwen3_0p6b``, ``qwen3_8b``, ``octen_8b``)
subclass :class:`QwenFamilyEmbedder`, wire the model's constants, and implement
:meth:`~QwenFamilyEmbedder._load_model` to call their OWN module-level ``_load``
seam -- so a fake-model test patches ``<module>._load`` and the subclass picks it
up at call time.

Inference is ONNX Runtime, never torch: the graph runs on the CPU provider for
live ingest and the CUDA provider for backfill, and the tokenizer is the
model's ``tokenizer.json`` loaded by the ``tokenizers`` library. Pooling,
truncation, and normalization are np. The heavy ``onnxruntime`` /
``tokenizers`` imports are deferred behind ``wrapt.lazy_import`` here, so
importing a model module at config time costs nothing and the session loads
lazily on first embed.

NOT registered in ``build_embedder`` / ``Store.embedders``: the Store binds its
embedders to ``inquiry_embeddings``'s ``vector(384)`` and rejects any other dim.
These embedders serve
:func:`~trackinizer.server.store.session_embed.sweep_session_embeddings`,
which takes its 1024-dim embedder as an argument.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final, cast

import asyncio

from trackinizer.server.tools import bucket_embed


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from onnxruntime import InferenceSession
    from tokenizers import Tokenizer

    import numpy as np
    import numpy.typing as npt

    type IntArray = npt.NDArray[np.int64]
    type FloatArray = npt.NDArray[np.float32]
else:
    from wrapt import lazy_import

    # `numpy` is light, but keep it lazy so importing a model module at config
    # time pulls nothing heavy; the pooling math needs it only on first embed.
    np = lazy_import("numpy")


__all__ = ["QUERY_INSTRUCT", "QwenFamilyEmbedder"]


# Model max is 32k; the mapper hands us <=1500-char chunks, so a shorter window
# never truncates real input and keeps the padded batch tensor small.
_MAX_TOKENS: Final = 8_192

QUERY_INSTRUCT: Final = (
    "Given a web search query, retrieve relevant passages that answer the query"
)
r"""The one-sentence retrieval task prepended to a QUERY, per every
Qwen3-Embedding card. A query is embedded ``Instruct: <task>\nQuery:<text>`` (a
measured 1-5% retrieval gain); a document is embedded without it. Prefixing the
corpus side would move every document vector off the query manifold -- the
asymmetry is load-bearing, not decorative."""


class QwenFamilyEmbedder:
    """Last-token-pool, truncate-then-normalize embedder over the Qwen recipe.

    Subclasses set the class attributes below and implement :meth:`_load_model`.

    Attributes:
      slug: The model's bare stored-name prefix (``"qwen3-embedding-4b"``); the
        stored :attr:`name` is ``f"{slug}@{dim}"``, so a rename orphans rows.
      default_dim: The registered dimension a bare name resolves to (the width
        the shipped partial HNSW index casts to).
      doc_prefix: Prepended to every DOCUMENT before embedding (never to a
        query). ``""`` for stock Qwen; ``"- "`` for Octen's documented
        workaround.

    Args:
      device: Inference device string (``"cpu"``, ``"cuda:0"``). CPU for live
        ingest; GPU for backfill. Selects the ONNX Runtime provider.
      batch_size: Texts per forward pass when callers batch via
        :meth:`embed_batch`.
      dim: The (Matryoshka-truncated) output width. ``None`` uses
        :attr:`default_dim`; the registry validates any override against the
        model's supported range before constructing.

    """

    slug: ClassVar[str]
    default_dim: ClassVar[int]
    doc_prefix: ClassVar[str] = ""

    def __init__(
        self,
        *,
        device: str = "cpu",
        batch_size: int = 16,
        dim: int | None = None,
    ) -> None:
        # Instance identity: the stored name and cast width follow the chosen dim,
        # so a Matryoshka override mints its own ``@dim`` identity (a new corpus
        # key) while a bare name keeps the registered default.
        self.dim = self.default_dim if dim is None else dim
        self.name = f"{self.slug}@{self.dim}"
        self._device = device
        self._batch_size = batch_size
        self._session: InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None
        # Guards the lazy load so concurrent first-embeds build one session, not
        # N. Created here (not at class scope) so each instance owns its lock.
        self._load_lock = asyncio.Lock()

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a document unit vector of length :attr:`dim`.

        Runs the forward pass in a worker thread (``asyncio.to_thread``) so the
        event loop never blocks on CPU/GPU inference.

        Args:
          text: Document text. Embedded with :attr:`doc_prefix` and no query
            instruction; last-token pooled, truncated, then unit-normalized.

        Returns:
          vector: Unit-normalized, :attr:`dim` floats.

        """
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` as documents in forward-pass batches of ``batch_size``.

        The DOCUMENT path: :attr:`doc_prefix`, never the query instruction. The
        batch path exists because backfill throughput is dominated by per-call
        overhead at batch=1; the sweep uses it when available.

        Args:
          texts: Document texts, order preserved.

        Returns:
          vectors: One unit vector per text, in input order.

        """
        if not texts:
            return []
        await self._ensure_loaded()
        prefixed = [self.doc_prefix + text for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), self._batch_size):
            chunk = prefixed[start : start + self._batch_size]
            vectors.extend(await asyncio.to_thread(self._embed_sync, chunk))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        r"""Embed a SEARCH QUERY, with the retrieval instruction prefixed.

        The query counterpart to :meth:`embed`: it wraps ``text`` as
        ``Instruct: <task>\nQuery:<text>`` per the card, which the document path
        deliberately does NOT do. Same pooling, truncation, and normalization.

        Args:
          text: The user's search query.

        Returns:
          vector: Unit-normalized, :attr:`dim` floats, on the query manifold.

        """
        await self._ensure_loaded()
        prompt = f"Instruct: {QUERY_INSTRUCT}\nQuery:{text}"
        vectors = await asyncio.to_thread(self._embed_sync, [prompt])
        return vectors[0]

    async def embed_bucketed_batch(
        self,
        texts: list[str],
        *,
        edges: Sequence[int],
        rows: Mapping[int, int],
    ) -> list[list[float]]:
        """Embed ``texts`` (documents) via the length-bucketed static-shape core.

        The GPU-backfill DOCUMENT path: applies :attr:`doc_prefix`, then routes
        through :func:`trackinizer.server.tools.bucket_embed.embed_bucketed` so
        every forward is one of ``len(edges)`` fixed shapes. Returns unit vectors
        in input order.

        Args:
          texts: Document texts.
          edges: Ascending static bucket edges (the padded shape set).
          rows: ``edge -> rows per forward`` (the runner's derived plan).

        Returns:
          vectors: One unit vector per text, in input order.

        """
        if not texts:
            return []
        await self._ensure_loaded()
        if self._session is None or self._tokenizer is None:
            raise ValueError("Expected the model to be loaded before embedding.")
        batcher = _HfTokenBatcher(self._tokenizer, device=self._device)
        prefixed = [self.doc_prefix + text for text in texts]
        return await bucket_embed.embed_bucketed(
            self._session,
            batcher,
            prefixed,
            edges=edges,
            rows=rows,
            dim=self.dim,
        )

    # Implemented per subclass to call its OWN module-level ``_load`` so a fake-model
    # test that patches ``<module>._load`` is picked up here.
    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        """Build this model's tokenizer + ONNX session on ``device``."""
        del device  # Abstract seam; each subclass uses the argument.
        raise NotImplementedError

    async def _ensure_loaded(self) -> None:
        """Load tokenizer + session once, under the lock, off the event loop."""
        if self._session is not None:
            return
        async with self._load_lock:
            # Double-checked locking: a first-embed that lost the lock race
            # returns here. The outer guard narrowed the type, but a concurrent
            # loader may have set the attribute since -- ty cannot see that.
            session_loaded = self._session is not None
            if session_loaded:  # ty: ignore[redundant-condition-strict] -- double-checked lock; a racing loader may have set _session.
                return
            tokenizer, session = await asyncio.to_thread(
                self._load_model,
                self._device,
            )
            self._tokenizer = tokenizer
            self._session = session

    # Runs entirely inside ``asyncio.to_thread``: the ONNX run is CPU/GPU-bound,
    # so it must not touch the event loop.
    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Tokenize, run, last-token pool, truncate, then L2-normalize."""
        if self._tokenizer is None or self._session is None:
            raise ValueError("Expected the model to be loaded before embedding.")
        batch = _encode(self._tokenizer, texts, max_length=_MAX_TOKENS)
        hidden: FloatArray = self._session.run(["last_hidden_state"], batch)[0]
        pooled = _last_token_pool(hidden, batch["attention_mask"])
        # Slice BEFORE normalize: Matryoshka nests the informative dims in the
        # prefix, and the truncated vector must be renormalized to unit length
        # (normalizing the full native vector then slicing leaves the slice off
        # the unit sphere).
        truncated = pooled[:, : self.dim]
        normalized = _l2_normalize(truncated)
        # ``ndarray.tolist()`` is typed ``Any``; the 2-D float32 array yields
        # exactly ``list[list[float]]``.
        return cast(list[list[float]], normalized.astype(np.float32).tolist())


def _encode(
    tokenizer: Tokenizer,
    texts: list[str],
    *,
    max_length: int,
) -> dict[str, IntArray]:
    """Left-pad-tokenize ``texts`` into an ONNX int64 batch (ids + mask)."""
    tokenizer.enable_truncation(max_length=max_length)
    tokenizer.enable_padding(direction="left")
    encodings = tokenizer.encode_batch(texts)
    input_ids: IntArray = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask: IntArray = np.array(
        [e.attention_mask for e in encodings],
        dtype=np.int64,
    )
    return {"input_ids": input_ids, "attention_mask": attention_mask}


# Per the Qwen recipe: with left padding the last real token is at position -1
# for every row; with right padding it is at ``attention_mask.sum(-1) - 1``. We
# tokenize left-padded, so the first branch runs, but both are kept so a caller
# that flips padding still pools correctly.
def _last_token_pool(
    last_hidden_states: FloatArray,
    attention_mask: IntArray,
) -> FloatArray:
    """Return each sequence's last non-pad hidden state."""
    rows = int(attention_mask.shape[0])  # pyright: ignore[reportAny] -- NumPy shape indexing is dtype-erased.
    left_padded = bool(attention_mask[:, -1].sum() == rows)  # pyright: ignore[reportAny] -- NumPy reduction result is dtype-erased.
    if left_padded:
        return last_hidden_states[:, -1]
    lengths = attention_mask.sum(axis=1) - 1
    return last_hidden_states[np.arange(rows), lengths]


# A zero row keeps a unit-1 denominator so it stays all-zero rather than dividing by
# zero.
def _l2_normalize(vectors: FloatArray) -> FloatArray:
    """Return ``vectors`` L2-normalized along the last axis (float32)."""
    as_float = vectors.astype(np.float32)
    norms = np.linalg.norm(as_float, axis=1, keepdims=True)
    return as_float / np.clip(norms, a_min=1e-12, a_max=None)


class _HfTokenBatcher:
    """A :class:`bucket_embed.TokenBatcher` over a real ``tokenizers`` tokenizer.

    ``count`` tokenizes one text unpadded (the routing key -- TRUE tokens, never
    a char estimate); ``pad_batch`` builds one bucket's forward inputs padded to
    the FULL ``rows`` with masked pad rows, left-padded per the Qwen recipe.
    """

    def __init__(self, tokenizer: Tokenizer, *, device: str) -> None:
        self._tokenizer = tokenizer
        self._device = device

    def count(self, text: str) -> int:
        """Return ``text``'s true token count, capped at the model window.

        Args:
          text: The text to tokenize (unpadded).

        Returns:
          tokens: Token count, truncated at the model's max window.

        """
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        self._tokenizer.no_padding()
        return len(self._tokenizer.encode(text).ids)

    def pad_batch(
        self,
        texts: list[str],
        *,
        rows: int,
        seq: int,
    ) -> dict[str, np.ndarray]:
        """Return the ``(rows, seq)`` forward batch, tail rows masked-padded.

        Args:
          texts: The bucket's texts (at most ``rows``).
          rows: The bucket's full row count; tail rows are masked-padded.
          seq: The bucket's padded sequence length.

        Returns:
          batch: ``input_ids`` + ``attention_mask``, each ``(rows, seq)`` int64.

        """
        self._tokenizer.enable_truncation(max_length=seq)
        self._tokenizer.enable_padding(direction="left", length=seq)
        encodings = self._tokenizer.encode_batch(texts)
        input_ids: IntArray = np.array([e.ids for e in encodings], dtype=np.int64)
        mask: IntArray = np.array(
            [e.attention_mask for e in encodings],
            dtype=np.int64,
        )
        pad_rows = rows - int(input_ids.shape[0])  # pyright: ignore[reportAny] -- NumPy shape indexing is dtype-erased.
        if pad_rows > 0:
            # Append all-zero (fully masked) rows so the forward hits the full
            # declared ``(rows, seq)`` shape; the caller slices them back off.
            input_ids = np.concatenate(
                [input_ids, np.zeros((pad_rows, seq), dtype=np.int64)],
            )
            mask = np.concatenate(
                [mask, np.zeros((pad_rows, seq), dtype=np.int64)],
            )
        return {"input_ids": input_ids, "attention_mask": mask}
