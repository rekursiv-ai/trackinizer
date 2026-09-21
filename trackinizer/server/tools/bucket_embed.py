"""Length-bucketed, statically compiled embed core for the GPU backfill runner.

Motivation (measured live in production 2026-09-20): ``torch.compile(dynamic=True)``
over an 11-shape mixed-length corpus stalls badly -- dynamic guards re-trace as
shapes drift -- while ``compile(dynamic=False, fullgraph=True)`` over a FIXED set
of padded shapes is markedly faster. The price of static compilation is that
every forward must be one of exactly ``len(edges)`` shapes; this module enforces
that.

The core:

1. Tokenize ONCE (true token count per text). Route each text to the smallest
   edge ``>= count`` (:func:`route`) -- by TRUE tokens, never a ``chars // 4``
   estimate, which truncates dense text.
2. Per edge, batch ``rows[edge]`` rows -- the caller-supplied per-edge row
   counts. The runner derives them from the model spec, the measured card, and
   the chosen dim (``bucket_boundaries.derive_rows`` via
   ``model_buckets.resolve_plan``); this core owns routing and the fixed-shape
   forward, not the memory model.
3. A ragged tail batch row-pads to the bucket's FULL row count with masked
   pad rows and slices the real rows back out -- so every forward is one of the
   declared ``(rows, edge)`` shapes.
4. :func:`configure_recompile_limits` pins the per-code-object
   ``recompile_limit`` to ``len(edges)`` (the true invariant: a shape beyond the
   set must fail loudly) while leaving the GLOBAL
   ``accumulated_recompile_limit`` non-binding -- setting the accumulated limit
   to ``len(edges)`` kills legitimate warmup across a 36-layer model's hundreds
   of compiled frames (a dead run, live 2026-09-20).

The runner (``backfill_embedding.py``) owns the read/write stages and the 3-stage
overlap; this owns the embed transform, whole-pool. The live CPU ingest path
stays in ``server/embedders/qwen_family.py`` and does NOT use this bucketing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import asyncio


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from torch.nn import functional

    import torch

    from trackinizer.types.embedder import ModelOutput
else:
    from wrapt import lazy_import

    torch = lazy_import("torch")
    functional = lazy_import("torch.nn.functional")


__all__ = [
    "ForwardModel",
    "TokenBatcher",
    "configure_recompile_limits",
    "embed_bucketed",
    "route",
]


def route(token_count: int, edges: Sequence[int]) -> int:
    """Return the smallest edge ``>= token_count``.

    Args:
      token_count: The text's TRUE token count (never a char estimate).
      edges: Ascending bucket edges; the last must cover the max length.

    Returns:
      edge: The bucket edge this text pads to.

    Raises:
      ValueError: ``token_count`` exceeds the last edge.

    """
    for edge in edges:
        if edge >= token_count:
            return edge
    raise ValueError(f"token_count {token_count} exceeds the last edge {edges[-1]}")


class TokenBatcher(Protocol):
    """The tokenizer contract the bucket core needs: count + row-padded batch.

    A real implementation wraps a HF tokenizer; tests inject a fake. ``count``
    is the routing key (true tokens); ``pad_batch`` builds the ``(rows, seq)``
    forward inputs (``input_ids`` + ``attention_mask``) for one bucket, padded to
    the FULL row count with masked tail rows, ready to splat as ``model(**batch)``.
    """

    def count(self, text: str) -> int:
        """Return the true token count for ``text``."""
        ...

    def pad_batch(
        self,
        texts: list[str],
        *,
        rows: int,
        seq: int,
    ) -> dict[str, torch.Tensor]:
        """Return the ``(rows, seq)`` forward batch (input_ids + attention_mask)."""
        ...


class ForwardModel(Protocol):
    """The model call the core makes: a batch dict in, a model output out."""

    def __call__(self, **batch: torch.Tensor) -> ModelOutput:
        """Return the forward output (``last_hidden_state``) for ``batch``."""
        ...


async def embed_bucketed(
    model: ForwardModel,
    tokenizer: TokenBatcher,
    texts: list[str],
    *,
    edges: Sequence[int],
    rows: Mapping[int, int],
    dim: int,
) -> list[list[float]]:
    """Embed ``texts`` via fixed-shape length buckets; return input-order vectors.

    Routes each text by true token count, runs one forward per non-empty bucket
    at that bucket's declared ``(rows[edge], edge)`` shape (tail rows padded and
    sliced back), pools the last token, truncates to ``dim``, and L2-normalizes.
    Order is preserved across the routing permutation.

    The per-edge row counts come from the caller (the runner derives them from the
    model spec, the measured card, and the chosen dim via
    ``model_buckets.resolve_plan``); this core owns only the routing and the
    fixed-shape forward, not the memory model.

    Args:
      model: The forward callable (a compiled model in production).
      tokenizer: Token counter + row-pad mask builder.
      texts: Document texts to embed.
      edges: Ascending bucket edges (the static shape set).
      rows: ``edge -> rows per forward`` for every edge in ``edges``.
      dim: Output (truncated) dimension.

    Returns:
      vectors: One unit vector per text, in input order.

    """
    if not texts:
        return []
    # Route once by TRUE token count, remembering each text's input slot.
    buckets: dict[int, list[int]] = {}
    for slot, text in enumerate(texts):
        edge = route(tokenizer.count(text), edges)
        buckets.setdefault(edge, []).append(slot)

    vectors: list[list[float]] = [[] for _ in texts]
    for edge, slots in buckets.items():
        edge_rows = rows[edge]
        # A bucket may hold more than one full batch; walk it in row-sized
        # chunks so every forward is exactly ``(edge_rows, edge)``.
        for start in range(0, len(slots), edge_rows):
            chunk = slots[start : start + edge_rows]
            chunk_vectors = await asyncio.to_thread(
                _embed_one_bucket,
                model,
                tokenizer,
                [texts[slot] for slot in chunk],
                rows=edge_rows,
                seq=edge,
                dim=dim,
            )
            for slot, vector in zip(chunk, chunk_vectors, strict=True):
                vectors[slot] = vector
    return vectors


def configure_recompile_limits(edges: Sequence[int]) -> None:
    """Pin torch dynamo's recompile limits for a static shape set.

    Sets the PER-CODE-OBJECT ``recompile_limit`` to ``len(edges)`` -- the true
    invariant, so a shape beyond the declared set fails loudly rather than
    silently compiling more -- and the GLOBAL ``accumulated_recompile_limit`` to
    a non-binding ``len(edges) * 1000``. Setting the accumulated limit to
    ``len(edges)`` kills legitimate warmup across a 36-layer model's hundreds of
    compiled frames (a dead run, live 2026-09-20).

    Args:
      edges: The static bucket edges; ``len`` is the per-frame recompile budget.

    """
    config = _dynamo_config()
    config.recompile_limit = len(edges)
    config.accumulated_recompile_limit = len(edges) * 1000


# Runs inside ``asyncio.to_thread``: the forward is GPU/CPU-bound and must not
# touch the event loop.
# ``texts`` has at most ``rows`` entries; the mask pads to the full ``rows`` with all-
# zero (masked) tail rows so the forward shape is invariant, then the output keeps only
# the ``len(texts)`` real rows.
def _embed_one_bucket(
    model: ForwardModel,
    tokenizer: TokenBatcher,
    texts: list[str],
    *,
    rows: int,
    seq: int,
    dim: int,
) -> list[list[float]]:
    """Forward one bucket at the fixed ``(rows, seq)`` shape; return real rows."""
    batch = tokenizer.pad_batch(texts, rows=rows, seq=seq)
    with torch.no_grad():
        output = model(**batch)
    pooled = _last_token_pool(output.last_hidden_state)
    truncated = pooled[: len(texts), :dim]
    normalized = functional.normalize(truncated, p=2, dim=1)
    listed = cast(object, normalized.to(torch.float32).cpu().tolist())
    return cast("list[list[float]]", listed)


# Left-padded per the Qwen recipe: the last real token is at position -1 for
# every row, so the pooled vector is the hidden state at -1. Padded tail rows
# pool a meaningless vector but are sliced off by the ``len(texts)`` cut.
def _last_token_pool(last_hidden_states: torch.Tensor) -> torch.Tensor:
    """Return each row's last-position hidden state (left-padded)."""
    return last_hidden_states[:, -1]


def _dynamo_config() -> _DynamoConfig:
    """Return torch._dynamo.config (indirected so a test can inject a fake)."""
    import torch._dynamo  # noqa: PLC0415 -- deferred so importing this module pulls no torch.

    # ``torch._dynamo.config`` is torch's own recompile-limit config surface; it
    # has no public alias, so the private access is the only path.
    return cast("_DynamoConfig", torch._dynamo.config)  # noqa: SLF001 -- torch's own config; no public alias exists.


class _DynamoConfig(Protocol):
    """The two recompile-limit knobs :func:`configure_recompile_limits` sets."""

    recompile_limit: int
    accumulated_recompile_limit: int
