r"""Shared recipe for the Qwen3-Embedding family (Qwen 0.6B/4B/8B, Octen).

Every model in this family embeds identically: ``AutoModel`` with
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
up at call time. The heavy ``transformers`` / torch imports are deferred behind
``wrapt.lazy_import`` here, so importing any model module at config time costs
nothing and the weights load lazily on first embed.

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
    from collections.abc import Callable, Mapping, Sequence

    from torch.nn import functional
    from transformers import AutoModel, AutoTokenizer, PreTrainedTokenizerBase

    import torch

    from trackinizer.types.embedder import ModelOutput
else:
    from wrapt import lazy_import

    # ~900 ms and up to ~15 GB of weights behind them; deferred so importing a
    # model module (config time) costs nothing and the model loads on first embed.
    torch = lazy_import("torch")
    functional = lazy_import("torch.nn.functional")
    AutoModel = lazy_import("transformers", "AutoModel")
    AutoTokenizer = lazy_import("transformers", "AutoTokenizer")


__all__ = ["QUERY_INSTRUCT", "QwenFamilyEmbedder", "hf_load", "weights_cached"]


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
      device: Torch device string (``"cpu"``, ``"cuda:0"``). CPU for live
        ingest; GPU for backfill.
      batch_size: Texts per forward pass when callers batch via
        :meth:`embed_batch`.
      dim: The (Matryoshka-truncated) output width. ``None`` uses
        :attr:`default_dim`; the registry validates any override against the
        model's supported range before constructing.
      compile_forward: Compile the forward (cuda backfill only).

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
        compile_forward: bool = False,
    ) -> None:
        # Instance identity: the stored name and cast width follow the chosen dim,
        # so a Matryoshka override mints its own ``@dim`` identity (a new corpus
        # key) while a bare name keeps the registered default.
        self.dim = self.default_dim if dim is None else dim
        self.name = f"{self.slug}@{self.dim}"
        self._device = device
        self._batch_size = batch_size
        self._compile_forward = compile_forward
        # ``configure_recompile_limits`` must run ONCE per process, not per
        # ``embed_bucketed_batch`` call: re-pinning the GLOBAL accumulated limit
        # mid-run could clip legitimate warmup (dead run, live 2026-09-20).
        self._recompile_configured = False
        # ``torch.compile`` returns a callable wrapper, not always a Module: the
        # monorepo torch stub narrows it back to the input Module, but the
        # public export relies on installed torch's own types, where it is
        # ``Callable[..., object] | Module``. Declare the union so the compiled
        # assignment type-checks in BOTH trees; every read casts to the concrete
        # ``ForwardModel`` / ``ModelOutput`` it needs.
        self._model: torch.nn.Module | Callable[..., object] | None = None
        self._tokenizer: PreTrainedTokenizerBase | None = None
        # Guards the lazy load so concurrent first-embeds build one model, not
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
        every forward is one of ``len(edges)`` fixed shapes -- required for a
        static ``torch.compile``. Pins the dynamo recompile limits once per
        process. Returns unit vectors in input order.

        Args:
          texts: Document texts.
          edges: Ascending static bucket edges (the compiled shape set).
          rows: ``edge -> rows per forward`` (the runner's derived plan).

        Returns:
          vectors: One unit vector per text, in input order.

        """
        if not texts:
            return []
        await self._ensure_loaded()
        if self._model is None or self._tokenizer is None:
            raise ValueError("Expected the model to be loaded before embedding.")
        if not self._recompile_configured:
            bucket_embed.configure_recompile_limits(edges)
            self._recompile_configured = True
        batcher = _HfTokenBatcher(self._tokenizer, device=self._device)
        prefixed = [self.doc_prefix + text for text in texts]
        return await bucket_embed.embed_bucketed(
            cast("bucket_embed.ForwardModel", self._model),
            batcher,
            prefixed,
            edges=edges,
            rows=rows,
            dim=self.dim,
        )

    # Implemented per subclass to call its OWN module-level ``_load`` so a fake-model
    # test that patches ``<module>._load`` is picked up here.
    def _load_model(
        self,
        device: str,
    ) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
        """Build this model's tokenizer + model on ``device``."""
        del device  # Abstract seam; each subclass uses the argument.
        raise NotImplementedError

    async def _ensure_loaded(self) -> None:
        """Load tokenizer + model once, under the lock, off the event loop."""
        if self._model is not None:
            return
        async with self._load_lock:
            # Double-checked locking: a first-embed that lost the lock race
            # returns here. The outer guard narrowed the type, but a concurrent
            # loader may have set the attribute since -- ty cannot see that.
            model_loaded = self._model is not None
            if model_loaded:  # ty: ignore[redundant-condition-strict] -- double-checked lock; a racing loader may have set _model.
                return
            tokenizer, model = await asyncio.to_thread(self._load_model, self._device)
            if self._compile_forward:
                # ``dynamic=True`` for THIS path: it embeds exact-length batches
                # whose shape varies per call, so specializing compilation would
                # recompile per shape (measured 0.06x -- 17x SLOWER). The 2.07x
                # gain (48.2 -> 99.8 vec/s, fp16, batch 48) was measured on the
                # BATCH backfill path BEFORE the length-bucketed static-shape core
                # moved to ``server/tools/bucket_embed.py``; the live single-text gain
                # here is unmeasured. Bulk backfill now uses the static core, not
                # this dynamic path.
                model = torch.compile(model, dynamic=True)
            self._tokenizer = tokenizer
            self._model = model

    # Runs entirely inside ``asyncio.to_thread``: torch is synchronous and the
    # forward pass is CPU/GPU-bound, so it must not touch the event loop.
    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Tokenize, forward, last-token pool, truncate, then L2-normalize."""
        if self._tokenizer is None or self._model is None:
            raise ValueError("Expected the model to be loaded before embedding.")
        batch = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=_MAX_TOKENS,
            return_tensors="pt",
        ).to(self._device)
        with torch.no_grad():
            # The forward output is transformers' BaseModelOutput, but the lazy
            # ``AutoModel`` import erases it to ``Any``; name the one attribute
            # we read so the access is typed rather than ``reportAny``.
            output = cast("ModelOutput", self._model(**batch))
        # ``BatchEncoding.__getitem__`` is typed as tokenizers' ``Encoding`` in
        # the stub, but under return_tensors="pt" it is a torch ``Tensor``.
        # Route through ``object`` so the cast is a widen-then-narrow, not the
        # disjoint ``Encoding -> Tensor`` ty rejects.
        mask = cast("torch.Tensor", cast(object, batch["attention_mask"]))
        pooled = _last_token_pool(output.last_hidden_state, mask)
        # Slice BEFORE normalize: Matryoshka nests the informative dims in the
        # prefix, and the truncated vector must be renormalized to unit length
        # (normalizing the full native vector then slicing leaves the slice off
        # the unit sphere).
        truncated = pooled[:, : self.dim]
        normalized = functional.normalize(truncated, p=2, dim=1)
        # ``Tensor.tolist`` is typed ``list[object]`` in the stub; the 2-D float
        # tensor yields exactly ``list[list[float]]``. Route through ``object``
        # so the cast is not the disjoint one ty rejects.
        listed = cast(object, normalized.to(torch.float32).cpu().tolist())
        return cast("list[list[float]]", listed)


# Module-level so the heavy ``transformers`` import stays out of import time and
# off the class body; each model module's ``_load`` seam delegates here.
# No ``cache_dir=``: transformers reads ``HF_HOME`` (ops/env) so every checkout
# shares one provisioned cache; an explicit dir would make it inert.
def hf_load(
    model_id: str,
    device: str,
) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
    """Build the left-padded tokenizer and the model for ``model_id`` on ``device``.

    Args:
      model_id: The Hugging Face repo id, resolved through the provisioned cache.
      device: Torch device string; selects the half precision (bf16 on CPU,
        fp16 on GPU).

    Returns:
      tokenizer: Left-padded tokenizer for last-token pooling.
      model: The eval-mode model on ``device``.

    """
    tokenizer = AutoTokenizer.from_pretrained(model_id, padding_side="left")
    # bf16 on CPU: measured 2026-09-19 on this host, a 16-text batch of
    # ~1500-char chunks ran 1.19x faster at bf16 (60.8s vs 72.5s) with cosine
    # agreement 2.8e-3 against fp32 -- well inside the halfvec storage
    # quantization, which is itself fp16. GPU uses fp16 (its native half).
    dtype = torch.bfloat16 if device == "cpu" else torch.float16
    model = AutoModel.from_pretrained(model_id, dtype=dtype)
    _ = model.to(device)
    _ = model.eval()
    return tokenizer, model


def weights_cached(model_id: str) -> bool:
    """Whether ``model_id``'s weights are already in the HF cache (no network).

    Consults ``try_to_load_from_cache`` for the model's ``config.json`` -- a
    cache HIT means the snapshot was downloaded, so the lazy load will not reach
    the network. Used at startup to decide degrade-vs-warm, never to trigger a
    download. Reads ``HF_HOME`` (ops/env); passes no ``cache_dir``.

    Args:
      model_id: The Hugging Face repo id to probe.

    Returns:
      present: ``True`` when the model's ``config.json`` is already cached.

    """
    # Imported here, not at module top, so huggingface_hub stays off the
    # config-time import path -- only this startup check needs it.
    import huggingface_hub  # noqa: PLC0415 -- deferred so config-time imports never pull huggingface_hub.

    cached = huggingface_hub.try_to_load_from_cache(model_id, "config.json")
    return isinstance(cached, str)


# Per the Qwen recipe: with left padding the last real token is at position -1
# for every row; with right padding it is at ``attention_mask.sum(-1) - 1``. We
# tokenize left-padded, so the first branch runs, but both are kept so a caller
# that flips padding still pools correctly.
def _last_token_pool(
    last_hidden_states: torch.Tensor,
    attention_mask: torch.Tensor,
) -> torch.Tensor:
    """Return each sequence's last non-pad hidden state."""
    left_padded = bool((attention_mask[:, -1].sum() == attention_mask.shape[0]).item())
    if left_padded:
        return last_hidden_states[:, -1]
    lengths = attention_mask.sum(dim=1) - 1
    return last_hidden_states[
        torch.arange(last_hidden_states.shape[0], device=last_hidden_states.device),
        lengths,
    ]


class _HfTokenBatcher:
    """A :class:`bucket_embed.TokenBatcher` over a real HF tokenizer.

    ``count`` tokenizes one text unpadded (the routing key -- TRUE tokens, never
    a char estimate); ``pad_batch`` builds one bucket's forward inputs padded to
    the FULL ``rows`` with masked pad rows, left-padded per the Qwen recipe.
    """

    def __init__(
        self,
        tokenizer: PreTrainedTokenizerBase,
        *,
        device: str,
    ) -> None:
        self._tokenizer = tokenizer
        self._device = device

    def count(self, text: str) -> int:
        """Return ``text``'s true token count, capped at the model window.

        Args:
          text: The text to tokenize (unpadded).

        Returns:
          tokens: Token count, truncated at the model's max window.

        """
        ids = cast(
            object,
            self._tokenizer(text, truncation=True, max_length=_MAX_TOKENS)["input_ids"],
        )
        return len(cast("list[int]", ids))

    def pad_batch(
        self,
        texts: list[str],
        *,
        rows: int,
        seq: int,
    ) -> dict[str, torch.Tensor]:
        """Return the ``(rows, seq)`` forward batch, tail rows masked-padded.

        Args:
          texts: The bucket's texts (at most ``rows``).
          rows: The bucket's full row count; tail rows are masked-padded.
          seq: The bucket's padded sequence length.

        Returns:
          batch: ``input_ids`` + ``attention_mask``, each ``(rows, seq)`` on the
            embedder's device.

        """
        encoded = self._tokenizer(
            texts,
            padding="max_length",
            truncation=True,
            max_length=seq,
            return_tensors="pt",
        )
        input_ids = cast("torch.Tensor", cast(object, encoded["input_ids"]))
        mask = cast("torch.Tensor", cast(object, encoded["attention_mask"]))
        pad_rows = rows - input_ids.shape[0]
        if pad_rows > 0:
            # Append all-zero (fully masked) rows so the forward hits the full
            # declared ``(rows, seq)`` shape; the caller slices them back off.
            input_ids = torch.cat(
                [input_ids, torch.zeros((pad_rows, seq), dtype=input_ids.dtype)],
            )
            mask = torch.cat([mask, torch.zeros((pad_rows, seq), dtype=mask.dtype)])
        return {
            "input_ids": input_ids.to(self._device),
            "attention_mask": mask.to(self._device),
        }
