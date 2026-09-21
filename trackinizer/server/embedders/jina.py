r"""Shared recipe for Jina embeddings v5 text models (nano, small).

LICENSE: jina-embeddings-v5 is CC BY-NC 4.0 -- NON-COMMERCIAL USE ONLY. Every
model module in this family repeats that banner; an operator enabling one must
have a non-commercial use or a separate commercial licence from Jina AI.

Jina v5 does NOT follow the Qwen family's raw ``AutoModel`` + manual last-token
pool. It ships a CUSTOM model class loaded with ``trust_remote_code=True`` whose
``encode(texts=..., task="retrieval", prompt_name="query"/"document")`` owns the
pooling, the task adapter, and the query/document prompt asymmetry. We call that
API directly rather than reimplement it, and store the model's NATIVE dimension
(no Matryoshka truncation) so the query and corpus vectors match.

``trust_remote_code=True`` executes code shipped in the model repo. We PIN a
specific commit (:data:`_REVISION` per module) so the code we run is immutable --
never a floating ``main`` that could change under us between deploys. The pin is
load-bearing for supply-chain safety, not a convenience.

Deps: ``transformers``, ``torch``, and ``peft`` (the task adapters are PEFT
modules); all deferred behind ``wrapt.lazy_import`` so importing a model module
at config time costs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final, Protocol, cast

import asyncio


if TYPE_CHECKING:
    from torch.nn import functional
    from transformers import AutoModel

    import torch
else:
    from wrapt import lazy_import

    torch = lazy_import("torch")
    functional = lazy_import("torch.nn.functional")
    AutoModel = lazy_import("transformers", "AutoModel")


__all__ = ["JinaV5Embedder", "jina_load", "weights_cached"]


# The retrieval task and the two prompt names Jina v5's ``encode`` accepts. A
# query is encoded with ``prompt_name="query"``, a document with
# ``prompt_name="document"``; the model's own code applies the asymmetry, unlike
# the Qwen family where we prepend the instruction ourselves.
_TASK: Final = "retrieval"
_QUERY_PROMPT: Final = "query"
_DOCUMENT_PROMPT: Final = "document"


class JinaV5Embedder:
    """Jina embeddings v5 text model via its custom ``encode`` API, unit-normed.

    Subclasses set :attr:`slug`, :attr:`default_dim`, and implement
    :meth:`_load_model` to call their module ``_load`` seam (pinned revision +
    ``trust_remote_code``).

    Attributes:
      slug: The bare stored-name prefix; the stored :attr:`name` is
        ``f"{slug}@{dim}"``, frozen since a rename orphans rows.
      default_dim: The model's native embedding dimension (no truncation); a Jina
        model is fixed-dim, so the registry accepts only this dim.

    Args:
      device: Torch device string.
      batch_size: Texts per ``encode`` call when batching via
        :meth:`embed_batch`.
      dim: Output width; ``None`` uses :attr:`default_dim`. Jina is fixed-dim, so
        the registry only ever passes the native dim (it rejects any other).

    """

    slug: ClassVar[str]
    default_dim: ClassVar[int]

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
        self._model: object | None = None
        self._load_lock = asyncio.Lock()

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a document unit vector of length :attr:`dim`."""
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` as DOCUMENTS (``prompt_name="document"``), in order.

        Args:
          texts: Document texts, order preserved.

        Returns:
          vectors: One unit vector per text, in input order.

        """
        if not texts:
            return []
        await self._ensure_loaded()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            vectors.extend(
                await asyncio.to_thread(self._encode_sync, chunk, _DOCUMENT_PROMPT),
            )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a SEARCH QUERY (``prompt_name="query"``) as a unit vector."""
        await self._ensure_loaded()
        vectors = await asyncio.to_thread(self._encode_sync, [text], _QUERY_PROMPT)
        return vectors[0]

    def _load_model(self, device: str) -> object:
        """Load the model on ``device`` via this module's ``_load`` seam."""
        del device  # Abstract seam; each subclass uses the argument.
        raise NotImplementedError

    async def _ensure_loaded(self) -> None:
        """Load the model once, under the lock, off the event loop."""
        if self._model is not None:
            return
        async with self._load_lock:
            # Double-checked locking: a first-embed that lost the lock race
            # returns here. ty cannot see a concurrent loader set the attribute.
            model_loaded = self._model is not None
            if model_loaded:  # ty: ignore[redundant-condition-strict] -- double-checked lock; a racing loader may have set _model.
                return
            self._model = await asyncio.to_thread(self._load_model, self._device)

    # Runs inside ``asyncio.to_thread``: the forward pass is CPU/GPU-bound and
    # must not touch the event loop.
    # We normalize ourselves so the stored vectors are unit-norm regardless of whether
    # the custom ``encode`` already normalizes (it is idempotent on an already-unit
    # vector); the cosine index and query path both assume unit vectors.
    def _encode_sync(self, texts: list[str], prompt_name: str) -> list[list[float]]:
        """Call the model's ``encode`` for ``prompt_name``, then L2-normalize."""
        if self._model is None:
            raise ValueError("Expected the model to be loaded before embedding.")
        # ``encode`` is defined in the repo's trust_remote_code module, so it is
        # untyped here; name the call and route its result through ``torch`` for
        # the normalize. ``convert_to_tensor`` keeps it on-device as a Tensor.
        encode = cast("_Encoder", self._model)
        raw = encode.encode(
            texts,
            task=_TASK,
            prompt_name=prompt_name,
            convert_to_tensor=True,
        )
        tensor = cast("torch.Tensor", raw)
        normalized = functional.normalize(tensor, p=2, dim=1)
        listed = cast(object, normalized.to(torch.float32).cpu().tolist())
        return cast("list[list[float]]", listed)


# Module-level so the heavy import stays off import time; each Jina module's
# ``_load`` seam delegates here with its pinned revision. No ``cache_dir=``:
# transformers reads ``HF_HOME`` (ops/env) so every checkout shares one cache.
def jina_load(model_id: str, revision: str, device: str) -> object:
    """Load a Jina v5 model at a PINNED revision with ``trust_remote_code``.

    Args:
      model_id: The Hugging Face repo id.
      revision: The pinned commit SHA -- the immutable remote code we execute.
      device: Torch device string.

    Returns:
      model: The eval-mode model on ``device`` (its custom ``encode`` API).

    """
    dtype = torch.bfloat16 if device == "cpu" else torch.float16
    model = AutoModel.from_pretrained(
        model_id,
        revision=revision,
        trust_remote_code=True,
        dtype=dtype,
    )
    _ = model.to(device)
    _ = model.eval()
    return model


def weights_cached(model_id: str, revision: str) -> bool:
    """Whether ``model_id`` at ``revision`` is already in the HF cache (no network).

    Args:
      model_id: The Hugging Face repo id.
      revision: The pinned commit SHA to probe.

    Returns:
      present: ``True`` when the pinned snapshot's ``config.json`` is cached.

    """
    import huggingface_hub  # noqa: PLC0415 -- deferred so config-time imports never pull huggingface_hub.

    cached = huggingface_hub.try_to_load_from_cache(
        model_id,
        "config.json",
        revision=revision,
    )
    return isinstance(cached, str)


class _Encoder(Protocol):
    """The one method the pooler calls on Jina's trust_remote_code model."""

    def encode(
        self,
        texts: list[str],
        *,
        task: str,
        prompt_name: str,
        convert_to_tensor: bool,
    ) -> object:
        """Return embeddings for ``texts`` under ``task`` / ``prompt_name``."""
        ...
