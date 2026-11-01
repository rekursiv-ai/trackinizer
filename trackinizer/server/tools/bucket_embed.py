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
   declared ``(rows, edge)`` shapes. The plan's rows are a ceiling: a forward that
   runs the card out of memory halves its bucket's rows, in the caller's mapping,
   for the rest of the run (the arithmetic memory model under-counted short
   buckets ~1.4x on a 5090, measured 2026-10-31).
4. :func:`configure_recompile_limits` pins the per-code-object
   ``recompile_limit`` to ``len(edges)`` (the true invariant: a shape beyond the
   set must fail loudly) while leaving the GLOBAL
   ``accumulated_recompile_limit`` non-binding -- setting the accumulated limit
   to ``len(edges)`` kills legitimate warmup across a 36-layer model's hundreds
   of compiled frames (a dead run, live 2026-09-20).

5. :class:`BucketQueue` keeps each bucket's texts across calls, so a forward runs
   full rather than padded out to the card's rows for one pool's few texts.

The runner (``backfill_embedding.py``) owns the read/write stages and the 3-stage
overlap, and holds one queue across its pools; this owns the embed transform. The
live CPU ingest path stays in ``server/embedders/qwen_family.py`` and does NOT use
this bucketing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

import asyncio
import itertools
import logging

from treekle import from_plain


if TYPE_CHECKING:
    from collections.abc import MutableMapping, Sequence

    from torch.nn import functional

    import torch

    from trackinizer.types.embedder import ModelOutput
else:
    from wrapt import lazy_import

    torch = lazy_import("torch")
    functional = lazy_import("torch.nn.functional")


__all__ = [
    "BucketQueue",
    "ForwardModel",
    "Ticket",
    "TokenBatcher",
    "configure_recompile_limits",
    "embed_bucketed",
    "route",
]


_LOGGER: Final = logging.getLogger(__name__)


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
    texts: Sequence[str],
    *,
    edges: Sequence[int],
    rows: MutableMapping[int, int],
    dim: int,
) -> list[list[float]]:
    """Embed ``texts`` via fixed-shape length buckets; return input-order vectors.

    One call's worth of a :class:`BucketQueue`: every bucket runs, full or not.

    Args:
      model: The forward callable (a compiled model in production).
      tokenizer: Token counter + row-pad mask builder.
      texts: Document texts to embed.
      edges: Ascending bucket edges (the static shape set).
      rows: ``edge -> rows per forward`` for every edge in ``edges``; lowered in
        place when a bucket runs the device out of memory.
      dim: Output (truncated) dimension.

    Returns:
      vectors: One unit vector per text, in input order.

    Raises:
      torch.OutOfMemoryError: A bucket runs out of memory at one row.

    """
    queue = BucketQueue(model, tokenizer, edges=edges, rows=rows, dim=dim)
    ticket = queue.add(texts)
    await queue.run(hold=0)
    return ticket.vectors


@dataclass(slots=True, kw_only=True)
class Ticket:
    """The vectors of one :meth:`BucketQueue.add`, filled as their buckets run.

    Attributes:
      vectors: One vector per added text, in input order; empty until its bucket
        runs.
      left: Texts still waiting for a forward.

    """

    vectors: list[list[float]]
    left: int

    @property
    def done(self) -> bool:
        """Whether every text of the call has its vector."""
        return self.left == 0


# A forward at the plan's rows costs the same whether its rows are texts or padding,
# and the plan sizes rows to the card: thousands for a short bucket. A runner that
# embedded each 1024-unit pool on its own computed 4.2% real tokens for the 0.6B and
# 14.2% for the 4B (2026-10-31, a 5090). The queue keeps each bucket's texts across
# calls instead, so a forward runs full, and runs part-full only to bound what it
# holds: 71-74% real, 354.5 against 24.7 vec/s for the 0.6B and 75.1 against 14.9
# for the 4B.
class BucketQueue:
    """Texts routed to length buckets, waiting across calls for a full forward.

    Routes each text by true token count; :meth:`run` forwards a bucket at its
    declared ``(rows[edge], edge)`` shape (tail rows padded and sliced back), pools
    the last token, truncates to ``dim``, and L2-normalizes. A forward that runs
    out of device memory halves ``rows[edge]`` in the caller's mapping and retries.

    Args:
      model: The forward callable (a compiled model in production).
      tokenizer: Token counter + row-pad mask builder.
      edges: Ascending bucket edges (the static shape set).
      rows: ``edge -> rows per forward`` for every edge in ``edges``; the runner
        derives them from the model spec, the measured card, and the chosen dim
        via ``model_buckets.resolve_plan``.
      dim: Output (truncated) dimension.

    """

    def __init__(
        self,
        model: ForwardModel,
        tokenizer: TokenBatcher,
        *,
        edges: Sequence[int],
        rows: MutableMapping[int, int],
        dim: int,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._edges = edges
        self._rows = rows
        self._dim = dim
        # Per edge, oldest first: (arrival, ticket, slot, text).
        self._waiting: dict[int, deque[tuple[int, Ticket, int, str]]] = {
            edge: deque() for edge in edges
        }
        self._arrivals = itertools.count()
        self._held = 0

    def add(self, texts: Sequence[str]) -> Ticket:
        """Route ``texts`` to their buckets; return the ticket their vectors fill.

        Args:
          texts: Document texts to embed.

        Returns:
          ticket: Done once every text has run; no forward runs here.

        """
        ticket = Ticket(vectors=[[] for _ in texts], left=len(texts))
        for slot, text in enumerate(texts):
            edge = route(self._tokenizer.count(text), self._edges)
            self._waiting[edge].append((next(self._arrivals), ticket, slot, text))
        self._held += len(texts)
        return ticket

    async def run(self, *, hold: int) -> None:
        """Forward every full bucket, then oldest-first until ``hold`` texts wait.

        Args:
          hold: The most texts left waiting; ``0`` runs every bucket.

        Raises:
          torch.OutOfMemoryError: A bucket runs out of memory at one row.

        """
        for edge, waiting in self._waiting.items():
            while len(waiting) >= self._rows[edge]:
                await self._forward(edge)
        while self._held > hold:
            _arrival, oldest = min(
                (waiting[0][0], edge)
                for edge, waiting in self._waiting.items()
                if waiting
            )
            await self._forward(oldest)

    async def _forward(self, edge: int) -> None:
        """Forward up to ``rows[edge]`` of the bucket's oldest texts."""
        waiting = self._waiting[edge]
        while True:
            edge_rows = self._rows[edge]
            chunk = [waiting[at] for at in range(min(edge_rows, len(waiting)))]
            try:
                vectors = await asyncio.to_thread(
                    _embed_one_bucket,
                    self._model,
                    self._tokenizer,
                    [text for _arrival, _ticket, _slot, text in chunk],
                    rows=edge_rows,
                    seq=edge,
                    dim=self._dim,
                )
            except torch.OutOfMemoryError:
                if edge_rows == 1:
                    raise
                self._rows[edge] = edge_rows // 2
                _LOGGER.warning(
                    "bucket %d ran out of memory at %d rows; %d from now on",
                    edge,
                    edge_rows,
                    self._rows[edge],
                )
                torch.cuda.empty_cache()
                continue
            break
        for (_arrival, ticket, slot, _text), vector in zip(chunk, vectors, strict=True):
            ticket.vectors[slot] = vector
            ticket.left -= 1
            waiting.popleft()
        self._held -= len(chunk)


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
    # Needed: without this cast, the export (OSS copy) fails its type check.
    return from_plain(
        cast(object, normalized.to(torch.float32).cpu().tolist()),
        list[list[float]],
    )


# Left-padded per the Qwen recipe: the last real token is at position -1 for
# every row, so the pooled vector is the hidden state at -1. Padded tail rows
# pool a meaningless vector but are sliced off by the ``len(texts)`` cut.
def _last_token_pool(last_hidden_states: torch.Tensor) -> torch.Tensor:
    """Return each row's last-position hidden state (left-padded)."""
    return last_hidden_states[:, -1]


def _dynamo_config() -> _DynamoConfig:
    """Return torch._dynamo.config (indirected so a test can inject a fake)."""
    from torch import (  # noqa: PLC0415 -- deferred so importing this module pulls no torch.
        _dynamo,
    )

    # ``torch._dynamo.config`` is torch's own recompile-limit config surface; it
    # has no public alias, so the private access is the only path.
    return cast("_DynamoConfig", _dynamo.config)


class _DynamoConfig(Protocol):
    """The two recompile-limit knobs :func:`configure_recompile_limits` sets."""

    recompile_limit: int
    accumulated_recompile_limit: int
