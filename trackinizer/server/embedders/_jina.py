r"""Shared recipe for Jina embeddings v5 text models (nano, small).

LICENSE: jina-embeddings-v5 is CC BY-NC 4.0 -- NON-COMMERCIAL USE ONLY. Every
model module in this family repeats that banner; an operator enabling one must
have a non-commercial use or a separate commercial licence from Jina AI.

Jina v5 uses MEAN pooling over the attention mask (not the Qwen family's
last-token pool), so it does not subclass :class:`QwenFamilyEmbedder`. We serve
the task-specific ``-retrieval`` ONNX export, whose retrieval adapter is merged
into the base weights -- a plain ``(input_ids, attention_mask) -> last_hidden_state``
graph, no PEFT and no ``trust_remote_code`` at inference. The query/document
asymmetry is applied as a text prompt prefix, mirroring the Qwen family.

Inference is ONNX Runtime, never torch; imports are deferred so importing a
model module at config time costs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final, cast

import asyncio

from trackinizer.server.embedders._onnx import load_onnx_model, weights_cached


if TYPE_CHECKING:
    from onnxruntime import InferenceSession
    from tokenizers import Tokenizer

    import numpy as np
    import numpy.typing as npt

    from trackinizer.server.embedders._onnx import OnnxSource

    type IntArray = npt.NDArray[np.int64]
    type FloatArray = npt.NDArray[np.float32]
else:
    from wrapt import lazy_import

    np = lazy_import("numpy")


__all__ = ["JinaV5Embedder"]


# Model max window; Jina v5 text handles long context, but the mapper hands us
# <=1500-char chunks, so a shorter window never truncates real input.
_MAX_TOKENS: Final = 8_192

# The retrieval-task prompt prefixes the merged ``-retrieval`` model expects. A
# query carries the query prompt; a document the passage prompt. The asymmetry
# is load-bearing: dropping it moves the two onto different manifolds.
_QUERY_PREFIX: Final = "Represent the query for retrieving supporting documents: "
_DOCUMENT_PREFIX: Final = "Represent the document for retrieval: "


class JinaV5Embedder:
    """Jina embeddings v5 text model via its ``-retrieval`` ONNX, unit-normed.

    Subclasses set :attr:`slug`, :attr:`default_dim`, :attr:`onnx`, and implement
    :meth:`_load_model` to call their module ``_load`` seam.

    Attributes:
      slug: The bare stored-name prefix; the stored :attr:`name` is
        ``f"{slug}@{dim}"``, frozen since a rename orphans rows.
      default_dim: The model's native embedding dimension (no truncation); a Jina
        model is fixed-dim, so the registry accepts only this dim.
      onnx: The model's ONNX source.

    Args:
      device: Inference device string; selects the ONNX Runtime provider.
      batch_size: Texts per forward pass when batching via :meth:`embed_batch`.
      dim: Output width; ``None`` uses :attr:`default_dim`. Jina is fixed-dim, so
        the registry only ever passes the native dim (it rejects any other).

    """

    slug: ClassVar[str]
    default_dim: ClassVar[int]
    onnx: ClassVar[OnnxSource]

    def __init__(
        self,
        *,
        device: str = "cpu",
        batch_size: int = 16,
        dim: int | None = None,
    ) -> None:
        self.dim = self.default_dim if dim is None else dim
        self.name = f"{self.slug}@{self.dim}"
        self._device = device
        self._batch_size = batch_size
        self._session: InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None
        self._load_lock = asyncio.Lock()

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a document unit vector of length :attr:`dim`."""
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` as DOCUMENTS (passage prompt), in order.

        Args:
          texts: Document texts, order preserved.

        Returns:
          vectors: One unit vector per text, in input order.

        """
        if not texts:
            return []
        await self._ensure_loaded()
        prefixed = [_DOCUMENT_PREFIX + text for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), self._batch_size):
            chunk = prefixed[start : start + self._batch_size]
            vectors.extend(await asyncio.to_thread(self._embed_sync, chunk))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a SEARCH QUERY (query prompt) as a unit vector."""
        await self._ensure_loaded()
        vectors = await asyncio.to_thread(self._embed_sync, [_QUERY_PREFIX + text])
        return vectors[0]

    def _load_model(self, device: str) -> tuple[Tokenizer, InferenceSession]:
        """Load the tokenizer + ONNX session on ``device`` via the ``_load`` seam."""
        del device  # Abstract seam; each subclass uses the argument.
        raise NotImplementedError

    async def _ensure_loaded(self) -> None:
        """Load the session once, under the lock, off the event loop."""
        if self._session is not None:
            return
        async with self._load_lock:
            # Double-checked locking: a first-embed that lost the lock race
            # returns here. ty cannot see a concurrent loader set the attribute.
            session_loaded = self._session is not None
            if session_loaded:  # ty: ignore[redundant-condition-strict] -- double-checked lock; a racing loader may have set _session.
                return
            tokenizer, session = await asyncio.to_thread(
                self._load_model,
                self._device,
            )
            self._tokenizer = tokenizer
            self._session = session

    # Runs inside ``asyncio.to_thread``: the forward is CPU/GPU-bound and must
    # not touch the event loop.
    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Tokenize, run, mean-pool over the mask, then L2-normalize."""
        if self._tokenizer is None or self._session is None:
            raise ValueError("Expected the model to be loaded before embedding.")
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        self._tokenizer.enable_padding()
        encodings = self._tokenizer.encode_batch(texts)
        input_ids: IntArray = np.array([e.ids for e in encodings], dtype=np.int64)
        mask: IntArray = np.array(
            [e.attention_mask for e in encodings],
            dtype=np.int64,
        )
        batch = {"input_ids": input_ids, "attention_mask": mask}
        hidden: FloatArray = self._session.run(["last_hidden_state"], batch)[0]
        pooled = _mean_pool(hidden, mask)
        truncated = pooled[:, : self.dim]
        # ``ndarray.tolist()`` is typed ``Any``; the 2-D float32 array yields
        # exactly ``list[list[float]]``.
        return cast(
            list[list[float]],
            _l2_normalize(truncated).astype(np.float32).tolist(),
        )


def load_jina_onnx(
    source: OnnxSource,
    device: str,
) -> tuple[Tokenizer, InferenceSession]:
    """Load a Jina ``-retrieval`` ONNX export and tokenizer on ``device``.

    Args:
      source: The model's ONNX repo location.
      device: Inference device string.

    Returns:
      tokenizer: The repo's tokenizer.
      session: The ONNX Runtime session on the chosen provider.

    """
    return load_onnx_model(source, device)


def jina_weights_cached(source: OnnxSource) -> bool:
    """Whether ``source``'s ONNX graph is already in the HF cache (no network)."""
    return weights_cached(source)


# Mean pooling over the attention mask: sum the masked hidden states and divide
# by the true token count. Jina v5 pools this way (unlike the Qwen family's
# last-token pool); using the wrong pooling silently wrecks retrieval.
def _mean_pool(
    last_hidden_states: FloatArray,
    attention_mask: IntArray,
) -> FloatArray:
    """Return the mask-weighted mean hidden state per sequence."""
    mask = attention_mask[:, :, None].astype(np.float32)
    summed = (last_hidden_states.astype(np.float32) * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), a_min=1.0, a_max=None)
    return summed / counts


# A zero row keeps a unit-1 denominator so it stays all-zero rather than dividing by
# zero.
def _l2_normalize(vectors: FloatArray) -> FloatArray:
    """Return ``vectors`` L2-normalized along the last axis (float32)."""
    as_float = vectors.astype(np.float32)
    norms = np.linalg.norm(as_float, axis=1, keepdims=True)
    return as_float / np.clip(norms, a_min=1e-12, a_max=None)
