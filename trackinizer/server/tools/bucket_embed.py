"""Length-bucketed embed core for the GPU backfill runner.

Motivation: batching texts of similar length keeps the padded forward tensor
small, so throughput is dominated by real tokens rather than pad. Each text
routes to the smallest edge ``>= its true token count``; a bucket runs one
forward per ``rows[edge]`` rows at the ``(rows, edge)`` shape, and a ragged
tail row-pads to the full row count with masked pad rows that the caller slices
back off.

Inference is ONNX Runtime, never torch: the session's graph optimization owns
what ``torch.compile`` did before, so there is no compile step and no dynamo
recompile-limit knob to pin. The runner (``backfill_embedding.py``) owns the
read/write stages and the 3-stage overlap; this owns the embed transform,
whole-pool. The live CPU ingest path stays in
``server/embedders/_base.py`` and does NOT use this bucketing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import asyncio


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import numpy as np
else:
    from wrapt import lazy_import

    np = lazy_import("numpy")


__all__ = ["Session", "TokenBatcher", "embed_bucketed", "route"]


class Session(Protocol):
    """The one ONNX Runtime call the bucket core makes: ``run``.

    Structural so a test injects a fake session and the real
    ``onnxruntime.InferenceSession`` satisfies it without a nominal dependency.
    """

    def run(
        self,
        output_names: list[str],
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        """Return the model outputs for ``input_feed``."""
        ...


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

    A real implementation wraps a ``tokenizers`` tokenizer; tests inject a fake.
    ``count`` is the routing key (true tokens); ``pad_batch`` builds the
    ``(rows, seq)`` forward inputs (``input_ids`` + ``attention_mask``) for one
    bucket, padded to the FULL row count with masked tail rows, ready to feed the
    ONNX session.
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
    ) -> dict[str, np.ndarray]:
        """Return the ``(rows, seq)`` forward batch (input_ids + attention_mask)."""
        ...


async def embed_bucketed(
    session: Session,
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

    Args:
      session: The ONNX Runtime session running the model's graph.
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
                session,
                tokenizer,
                [texts[slot] for slot in chunk],
                rows=edge_rows,
                seq=edge,
                dim=dim,
            )
            for slot, vector in zip(chunk, chunk_vectors, strict=True):
                vectors[slot] = vector
    return vectors


# Runs inside ``asyncio.to_thread``: the forward is GPU/CPU-bound and must not
# touch the event loop.
# ``texts`` has at most ``rows`` entries; the mask pads to the full ``rows`` with all-
# zero (masked) tail rows so the forward shape is invariant, then the output keeps only
# the ``len(texts)`` real rows.
def _embed_one_bucket(
    session: Session,
    tokenizer: TokenBatcher,
    texts: list[str],
    *,
    rows: int,
    seq: int,
    dim: int,
) -> list[list[float]]:
    """Forward one bucket at the fixed ``(rows, seq)`` shape; return real rows."""
    batch = tokenizer.pad_batch(texts, rows=rows, seq=seq)
    hidden = session.run(["last_hidden_state"], batch)[0]
    pooled = _last_token_pool(hidden)
    truncated = pooled[: len(texts), :dim]
    normalized = _l2_normalize(truncated)
    # ``ndarray.tolist()`` is typed ``Any``; the 2-D float32 array yields exactly
    # ``list[list[float]]``.
    return cast(list[list[float]], normalized.astype(np.float32).tolist())


# Left-padded per the Qwen recipe: the last real token is at position -1 for
# every row, so the pooled vector is the hidden state at -1. Padded tail rows
# pool a meaningless vector but are sliced off by the ``len(texts)`` cut.
def _last_token_pool(last_hidden_states: np.ndarray) -> np.ndarray:
    """Return each row's last-position hidden state (left-padded)."""
    return last_hidden_states[:, -1]


# A zero row (never a real embedding -- only a fully-masked pad row) keeps a unit-1
# denominator so it stays all-zero rather than dividing by zero.
def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """Return ``vectors`` L2-normalized along the last axis (float32)."""
    as_float = vectors.astype(np.float32)
    norms = np.linalg.norm(as_float, axis=1, keepdims=True)
    return as_float / np.clip(norms, a_min=1e-12, a_max=None)
