r"""Qwen3-Embedding-0.6B: the embedding add-on's default model, in one file.

The recipe the card documents: ``AutoModel`` with ``padding_side="left"``,
last-token pooling, then L2-normalize, with the corpus/query asymmetry: a QUERY is
wrapped ``Instruct: <task>\nQuery:<text>`` while a DOCUMENT is embedded bare. Its
native width is 1024 (MRL 32..1024), which is the stored width, so the slice to
:attr:`~Qwen06BEmbedder.dim` before the normalize is a no-op here but keeps a
truncating subclass correct.

On a CPU the forwards run eager in worker threads, shortest texts first. On a GPU
every forward is one of :data:`_EDGES` fixed shapes, at the rows the card's memory
holds (:func:`~trackinizer.addons.embedding.bucket_embed.derive_rows`), so a
compiled forward runs full; that path is the re-embed's.

The heavy ``transformers`` / torch imports are deferred behind
``wrapt.lazy_import``, so building the add-on costs nothing and the weights load
on the first embed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final, Protocol, cast

import asyncio
import os

from configgle import Fig

from trackinizer.addons.embedding import bucket_embed
from trackinizer.addons.embedding.bucket_embed import ModelShape


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from torch.nn import functional
    from transformers import AutoModel, AutoTokenizer, PreTrainedTokenizerBase

    import torch

    from trackinizer.addons.embedding.bucket_embed import ModelOutput
else:
    from wrapt import lazy_import

    # ~900 ms and ~1.2 GB of weights behind them; deferred so building the add-on
    # costs nothing and the model loads on first embed.
    torch = lazy_import("torch")
    functional = lazy_import("torch.nn.functional")
    AutoModel = lazy_import("transformers", "AutoModel")
    AutoTokenizer = lazy_import("transformers", "AutoTokenizer")


__all__ = ["QUERY_INSTRUCT", "QWEN_MODEL_ID", "Qwen06BEmbedder", "hf_load"]


QWEN_MODEL_ID: Final = "Qwen/Qwen3-Embedding-0.6B"
"""Resolved through the provisioned HF cache (``HF_HOME`` via ops/env); never
pass ``cache_dir=`` -- an explicit dir overrides the shared cache."""

# Model max is 32k; the add-on hands us <=1500-char chunks, so a shorter window
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


class Qwen06BEmbedder:
    """Qwen3-Embedding-0.6B at its native 1024 dims, unit-normalized.

    Attributes:
      doc_prefix: Prepended to every DOCUMENT before embedding (never to a
        query). ``""`` for stock Qwen; a subclass that sets it sets its own
        ``name`` too, since its vectors differ.

    """

    class Config(Fig["Qwen06BEmbedder"]):
        device: str = "cpu"
        """Torch device: ``"cpu"`` in the server; ``"cuda:0"`` for the re-embed."""

        threads: int = 0
        """Torch CPU threads; 0 is a quarter of the host's CPUs, from 1 to 16."""

        batch_size: int = 16
        """Texts per forward on the CPU."""

    name = "qwen3-embedding-0.6b@1024"
    dim = 1_024
    doc_prefix: ClassVar[str] = ""

    def __init__(self, config: Config) -> None:
        self._device = config.device
        # A quarter of the CPUs, at most 16: one forward gains little past 16
        # threads (measured on a 64-core host), and the server's work needs the rest.
        self._threads = config.threads or min(16, max(1, (os.cpu_count() or 1) // 4))
        self._batch_size = config.batch_size
        # ``torch.compile`` returns a callable wrapper, not always a Module: the
        # monorepo torch stub narrows it back to the input Module, but the
        # public export relies on installed torch's own types, where it is
        # ``Callable[..., object] | Module``. Declare the union so the compiled
        # assignment type-checks in BOTH trees; every read casts to the concrete
        # ``ForwardModel`` / ``ModelOutput`` it needs.
        self._model: torch.nn.Module | Callable[..., object] | None = None
        self._tokenizer: PreTrainedTokenizerBase | None = None
        # Rows per forward for each of ``_EDGES`` on a GPU, from the card's memory
        # at load; a bucket that runs out of memory halves its own for good.
        self._rows: dict[int, int] = {}
        # Guards the lazy load so concurrent first-embeds build one model, not
        # N. Created here (not at class scope) so each instance owns its lock.
        self._load_lock = asyncio.Lock()

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` as documents: :attr:`doc_prefix`, never the instruction.

        On a CPU, shortest first in forwards of ``batch_size``, so a short text
        never pads out to a long one's length; elsewhere, in length buckets of
        fixed shape.

        Args:
          texts: Document texts.

        Returns:
          vectors: One unit vector per text, in input order.

        """
        if not texts:
            return []
        await self._ensure_loaded()
        if self._device != "cpu":
            return await bucket_embed.embed_bucketed(
                cast("bucket_embed.ForwardModel", self._model),
                _HfTokenBatcher(
                    cast("PreTrainedTokenizerBase", self._tokenizer),
                    device=self._device,
                    prefix=self.doc_prefix,
                ),
                texts,
                edges=_EDGES,
                rows=self._rows,
                dim=self.dim,
            )
        order = sorted(range(len(texts)), key=lambda slot: len(texts[slot]))
        prefixed = [self.doc_prefix + texts[slot] for slot in order]
        vectors: list[list[float]] = []
        for start in range(0, len(prefixed), self._batch_size):
            chunk = prefixed[start : start + self._batch_size]
            vectors.extend(await asyncio.to_thread(self._embed_sync, chunk))
        by_slot = dict(zip(order, vectors, strict=True))
        return [by_slot[slot] for slot in range(len(texts))]

    async def embed_query(self, text: str) -> list[float]:
        r"""Embed a SEARCH QUERY, with the retrieval instruction prefixed.

        It wraps ``text`` as ``Instruct: <task>\nQuery:<text>`` per the card, which
        the document path deliberately does NOT do. Same pooling, truncation, and
        normalization.

        Args:
          text: The user's search query.

        Returns:
          vector: Unit-normalized, :attr:`dim` floats, on the query manifold.

        """
        await self._ensure_loaded()
        prompt = f"Instruct: {QUERY_INSTRUCT}\nQuery:{text}"
        vectors = await asyncio.to_thread(self._embed_sync, [prompt])
        return vectors[0]

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
            tokenizer, model = await asyncio.to_thread(
                _load,
                self._device,
                threads=self._threads,
            )
            if self._device != "cpu":
                # Only the GPU compiles: the server's unit may write nothing for
                # Inductor to cache, so its CPU runs eager. Measured 2.79x on a 5090.
                bucket_embed.configure_recompile_limits(_EDGES)
                memory = cast(
                    "_DeviceProperties",
                    torch.cuda.get_device_properties(self._device),
                ).total_memory
                self._rows.update(
                    (
                        edge,
                        bucket_embed.derive_rows(
                            _SHAPE,
                            edge,
                            vram_gb=memory / 1e9,
                            dim=self.dim,
                        ),
                    )
                    for edge in _EDGES
                )
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
# off the class body.
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
    # An embedder reads one forward's last hidden state; the KV cache a decoder
    # forward builds by default is never read. Measured on a 5090 (2026-10-31, 0.6B,
    # fp16): it held ~114 KB a token, 5-6x the rest of the forward, so every GPU
    # backfill's bucket plan ran out of memory on its first batch.
    cast("_ModelConfig", model.config).use_cache = False
    _ = model.to(device)
    _ = model.eval()
    return tokenizer, model


# The module ``_load`` seam: fake-model tests patch this.
def _load(
    device: str,
    *,
    threads: int,
) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
    """Build the tokenizer and the model on ``device``, torch on ``threads`` threads."""
    torch.set_num_threads(threads)
    return hf_load(QWEN_MODEL_ID, device)


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


_EDGES: Final = (64, 160, 288, 512, 704, 992, 1792, 3584, 5376, 8192)
"""The GPU forward's token lengths, from the session corpus's token histogram
(2026-09-20) under this model's forward cost: each text pads to the first that
holds it."""

_SHAPE: Final = ModelShape(
    hidden_dim=1_024,
    intermediate_dim=3_072,
    num_layers=28,
    num_heads=16,
    num_kv_heads=8,
    head_dim=128,
    vocab_size=151_669,
)
"""Qwen/Qwen3-Embedding-0.6B's config.json (2026-09-20)."""


class _ModelConfig(Protocol):
    """The one ``PretrainedConfig`` field :func:`hf_load` sets."""

    use_cache: bool


class _DeviceProperties(Protocol):
    """The one ``get_device_properties`` field the row plan reads (bytes)."""

    total_memory: int


class _HfTokenBatcher:
    """A :class:`bucket_embed.TokenBatcher` over a real HF tokenizer.

    ``count`` tokenizes one text unpadded (the routing key -- TRUE tokens, never
    a char estimate); ``pad_batch`` builds one bucket's forward inputs padded to
    the FULL ``rows`` with masked pad rows, left-padded per the Qwen recipe. Both
    read each text with ``prefix`` in front, the model's document prefix.
    """

    def __init__(
        self,
        tokenizer: PreTrainedTokenizerBase,
        *,
        device: str,
        prefix: str,
    ) -> None:
        self._tokenizer = tokenizer
        self._device = device
        self._prefix = prefix

    def count(self, text: str) -> int:
        """Return ``text``'s true token count, capped at the model window.

        Args:
          text: The text to tokenize (unpadded).

        Returns:
          tokens: Token count, truncated at the model's max window.

        """
        ids = cast(
            object,
            self._tokenizer(
                self._prefix + text,
                truncation=True,
                max_length=_MAX_TOKENS,
            )["input_ids"],
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
            [self._prefix + text for text in texts],
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
