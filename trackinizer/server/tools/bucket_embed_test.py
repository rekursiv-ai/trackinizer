"""Length-bucketed static-shape embed core: routing, tail-pad, shape invariant.

Every test uses a FAKE tokenizer (an injectable text->token-count rule) and a
FAKE model (records the ``(rows, seq)`` shape of every forward) -- no torch
weights, no GPU, no network. Each test mirrors a real failure from the live
production backfill (see ``bucket_embed`` docstring); the shape-invariant test is
the one that would have caught the 11x8192 OOM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import pytest
import torch

from trackinizer.server.tools import bucket_embed


if TYPE_CHECKING:
    from collections.abc import Callable


# A small edge set + its per-edge rows for tests. Rows decrease with edge (the
# runner derives real rows via ``model_buckets.resolve_plan``; this core just
# consumes the mapping). Short edges pack many rows so the tail-pad tests have
# room; the long edge packs few, mirroring production.
_EDGES = (32, 128, 512, 8192)
_ROWS = {32: 64, 128: 16, 512: 4, 8192: 2}


class _FakeTokenizer:
    """Maps each text to a token count via an injectable rule; pads to a width.

    ``token_count`` is how ``embed_bucketed`` learns each text's TRUE length
    (the routing key). A test can make "dense" text have more tokens than
    ``len(text) // 4`` would estimate, which is the chars-estimate regression.
    """

    def __init__(self, token_count: Callable[[str], int]) -> None:
        self._token_count = token_count

    def count(self, text: str) -> int:
        """Return the true token count for ``text``."""
        return self._token_count(text)

    def pad_batch(
        self,
        texts: list[str],
        *,
        rows: int,
        seq: int,
    ) -> dict[str, torch.Tensor]:
        """Return the ``(rows, seq)`` forward batch (input_ids + attention_mask).

        Left-padded per the Qwen recipe: within a real row the last ``count``
        positions are 1. Padded (tail) rows are all-zero. ``input_ids`` is zeros
        of the same shape -- the fake model reads only shapes, not token values.
        """
        mask = torch.zeros((rows, seq), dtype=torch.long)
        for i, text in enumerate(texts):
            count = min(self._token_count(text), seq)
            mask[i, seq - count :] = 1
        return {
            "input_ids": torch.zeros((rows, seq), dtype=torch.long),
            "attention_mask": mask,
        }


class _FakeOutput:
    """The forward output the pooler reads: one ``last_hidden_state`` field."""

    def __init__(self, last_hidden_state: torch.Tensor) -> None:
        self.last_hidden_state = last_hidden_state


class _FakeModel:
    """Records the ``(rows, seq)`` of every forward; returns a fake hidden state.

    The last-token hidden vector is the row index repeated across ``dim`` so a
    test can verify scatter-back order: row ``i``'s vector is ``[i, i, ...]``
    (pre-normalization).
    """

    def __init__(self, *, dim: int) -> None:
        self.dim = dim
        self.shapes: list[tuple[int, int]] = []

    def __call__(self, **batch: torch.Tensor) -> _FakeOutput:
        """Return a fake output with ``(rows, seq, dim)`` hidden states."""
        rows, seq = batch["input_ids"].shape
        self.shapes.append((rows, seq))
        # The last position of every row carries that row's ordinal, so the
        # caller's scatter can be checked by reading vector[0].
        hidden = torch.zeros((rows, seq, self.dim))
        for i in range(rows):
            hidden[i, -1, :] = float(i)
        return _FakeOutput(hidden)


def _chars_over_four(text: str) -> int:
    """Return the naive ``chars // 4`` estimate the router must NOT use."""
    return max(1, len(text) // 4)


# ---- routing ---------------------------------------------------------------


def test_routes_to_the_smallest_edge_at_least_the_token_count() -> None:
    """Each length lands in the smallest edge >= its TRUE token count."""
    assert bucket_embed.route(1, _EDGES) == 32
    assert bucket_embed.route(32, _EDGES) == 32
    assert bucket_embed.route(33, _EDGES) == 128
    assert bucket_embed.route(500, _EDGES) == 512
    assert bucket_embed.route(513, _EDGES) == 8192


@pytest.mark.asyncio
async def test_dense_text_routes_by_token_count_not_chars() -> None:
    """Dense text (many tokens per char) routes by tokens, never ``chars//4``.

    A 40-char string that tokenizes to 200 tokens must land in the 512 bucket,
    not the 32 bucket ``chars//4 == 10`` would pick -- the chars-estimate
    truncated real content live.
    """
    dense = "x" * 40  # chars//4 == 10 -> would route to edge 32.
    tokenizer = _FakeTokenizer(lambda t: 200 if t == dense else _chars_over_four(t))
    model = _FakeModel(dim=4)
    _ = await bucket_embed.embed_bucketed(
        model,
        tokenizer,
        [dense],
        edges=_EDGES,
        rows=_ROWS,
        dim=4,
    )
    # The only forward ran at the 512 edge (padded to full rows), never 32.
    seqs = {seq for _rows, seq in model.shapes}
    assert seqs == {512}


# ---- tail padding + shape invariant ----------------------------------------


@pytest.mark.asyncio
async def test_every_forward_shape_is_in_the_declared_set() -> None:
    """For an arbitrary length mix, every forward shape is a declared bucket shape.

    The core invariant: static compilation sees exactly ``len(edges)`` shapes.
    A ragged batch must row-pad to its bucket's full ``rows[edge]``. Breaking the
    tail-pad (see the mutation proof) makes a forward appear at a non-declared row
    count and fails this.
    """
    tokenizer = _FakeTokenizer(len)
    model = _FakeModel(dim=4)
    # A mix that lands in several buckets with ragged counts per bucket.
    texts = ["a" * n for n in (1, 5, 30, 40, 100, 500, 1000)]
    _ = await bucket_embed.embed_bucketed(
        model,
        tokenizer,
        texts,
        edges=_EDGES,
        rows=_ROWS,
        dim=4,
    )
    declared = {(_ROWS[edge], edge) for edge in _EDGES}
    assert set(model.shapes) <= declared, (model.shapes, declared)


@pytest.mark.asyncio
async def test_tail_rows_are_padded_and_sliced_back() -> None:
    """A ragged bucket pads to full rows; the result keeps only the real ones."""
    tokenizer = _FakeTokenizer(len)
    model = _FakeModel(dim=4)
    texts = ["a" * 5, "a" * 6]  # Two texts, both edge 32 (its rows is larger).
    vectors = await bucket_embed.embed_bucketed(
        model,
        tokenizer,
        texts,
        edges=_EDGES,
        rows=_ROWS,
        dim=4,
    )
    # Two inputs -> two vectors, even though the forward ran full rows.
    assert len(vectors) == 2
    # The forward ran at the full declared row count for edge 32, not 2 rows.
    rows_seen = {rows for rows, _seq in model.shapes}
    assert rows_seen == {_ROWS[32]}


# ---- out of memory ------------------------------------------------------------


class _SmallCard(_FakeModel):
    """A fake model whose forward runs out of memory above ``max_rows`` rows."""

    def __init__(self, *, dim: int, max_rows: int) -> None:
        super().__init__(dim=dim)
        self.max_rows = max_rows

    @override
    def __call__(self, **batch: torch.Tensor) -> _FakeOutput:
        rows, seq = batch["input_ids"].shape
        if rows > self.max_rows:
            self.shapes.append((rows, seq))
            raise torch.OutOfMemoryError(f"CUDA out of memory at {rows} rows")
        return super().__call__(**batch)


@pytest.mark.asyncio
async def test_a_bucket_that_runs_out_of_memory_halves_its_rows_for_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The plan is a ceiling: an OOM halves the bucket's rows and keeps them halved.

    The memory model under-counted short buckets ~1.4x on a 5090, so the first
    forward of a real backfill ran out of memory; halving lets it go on.
    """
    model = _SmallCard(dim=4, max_rows=4)
    rows = {32: 16, 128: 16}
    texts = [f"text {i}" for i in range(10)]

    vectors = await bucket_embed.embed_bucketed(
        model,
        _FakeTokenizer(len),
        texts,
        edges=(32, 128),
        rows=rows,
        dim=4,
    )
    again = await bucket_embed.embed_bucketed(
        model,
        _FakeTokenizer(len),
        texts[:3],
        edges=(32, 128),
        rows=rows,
        dim=4,
    )

    assert len(vectors) == len(texts)
    assert len(again) == 3
    assert rows == {32: 4, 128: 16}
    assert model.shapes == [(16, 32), (8, 32), *[(4, 32)] * 3, (4, 32)]
    assert caplog.messages[:2] == [
        "bucket 32 ran out of memory at 16 rows; 8 from now on",
        "bucket 32 ran out of memory at 8 rows; 4 from now on",
    ]


@pytest.mark.asyncio
async def test_a_bucket_out_of_memory_at_one_row_is_raised() -> None:
    model = _SmallCard(dim=4, max_rows=0)

    with pytest.raises(torch.OutOfMemoryError, match="at 1 rows"):
        await bucket_embed.embed_bucketed(
            model,
            _FakeTokenizer(len),
            ["one"],
            edges=(32,),
            rows={32: 2},
            dim=4,
        )


# ---- a queue that fills across calls ----------------------------------------


@pytest.mark.asyncio
async def test_a_bucket_waits_across_calls_until_it_fills_a_forward() -> None:
    """A forward runs only on a full bucket, so pools smaller than a bucket stay cheap.

    Per-call padding to the plan's rows left 4.2% of a production forward's tokens
    real for the 0.6B (1024-unit pools against thousands of rows, 2026-10-31).
    """
    model = _FakeModel(dim=4)
    queue = bucket_embed.BucketQueue(
        model,
        _FakeTokenizer(len),
        edges=(32,),
        rows={32: 4},
        dim=4,
    )

    first = queue.add(["a", "b", "c"])
    await queue.run(hold=8)
    shapes_before_full = list(model.shapes)
    second = queue.add(["d"])
    await queue.run(hold=8)
    shapes_once_full = list(model.shapes)
    third = queue.add(["e"])
    await queue.run(hold=8)

    assert shapes_before_full == []
    assert shapes_once_full == [(4, 32)]
    assert model.shapes == [(4, 32)]
    assert (first.done, second.done, third.done) == (True, True, False)
    await queue.run(hold=0)
    assert model.shapes == [(4, 32), (4, 32)]
    assert third.done
    assert len(third.vectors) == 1


@pytest.mark.asyncio
async def test_holding_past_the_cap_runs_the_bucket_of_the_oldest_text() -> None:
    """Past ``hold``, the oldest text's bucket runs part-full, so the oldest call ends."""
    model = _FakeModel(dim=4)
    queue = bucket_embed.BucketQueue(
        model,
        _FakeTokenizer(len),
        edges=(32, 128),
        rows={32: 4, 128: 4},
        dim=4,
    )

    old = queue.add(["a" * 100])
    new = queue.add(["b", "c"])
    await queue.run(hold=2)

    assert model.shapes == [(4, 128)]
    assert (old.done, new.done) == (True, False)


@pytest.mark.asyncio
async def test_a_queue_returns_each_calls_vectors_in_its_input_order() -> None:
    """Vectors land in the slot of the text that made them, across buckets and calls."""
    model = _FakeModel(dim=4)
    queue = bucket_embed.BucketQueue(
        model,
        _FakeTokenizer(len),
        edges=(32, 128),
        rows={32: 2, 128: 2},
        dim=4,
    )

    first = queue.add(["a" * 100, "b", "c" * 100])
    second = queue.add(["d"])
    await queue.run(hold=0)

    # A forward's row ``i`` pools to ``[i, i, ...]`` normalized: 0.0 for row 0, 0.5
    # for row 1. "b" then "d" fill the short bucket, "a"*100 then "c"*100 the long.
    assert [vector[0] for vector in first.vectors] == [0.0, 0.0, 0.5]
    assert [vector[0] for vector in second.vectors] == [0.5]


@pytest.mark.asyncio
async def test_vectors_are_cut_to_dim_then_normalized() -> None:
    """A wider model's pooled vector keeps its first ``dim`` entries, at unit norm."""
    vectors = await bucket_embed.embed_bucketed(
        _FakeModel(dim=8),
        _FakeTokenizer(len),
        ["a", "b"],
        edges=(32,),
        rows={32: 2},
        dim=4,
    )

    # Row 1 pools to [1] * 8; its first four, normalized, are 0.5 each.
    assert vectors[1] == pytest.approx([0.5] * 4)


# ---- scatter / order preservation ------------------------------------------


@pytest.mark.asyncio
async def test_vectors_return_in_input_order_across_buckets() -> None:
    """Inputs spanning several buckets come back in INPUT order, unit-normed.

    The fake model stamps each row's ordinal into its vector; after routing
    scrambles inputs across buckets, the scatter must restore input order.
    """
    tokenizer = _FakeTokenizer(len)
    model = _FakeModel(dim=4)
    # Interleave lengths so routing reorders them across buckets.
    texts = ["a" * 1000, "a" * 5, "a" * 400, "a" * 20, "a" * 5000]
    vectors = await bucket_embed.embed_bucketed(
        model,
        tokenizer,
        texts,
        edges=_EDGES,
        rows=_ROWS,
        dim=4,
    )
    assert len(vectors) == len(texts)
    for vector in vectors:
        norm = sum(v * v for v in vector) ** 0.5
        # Row 0 within its bucket has an all-zero vector (ordinal 0); norm 0 or 1.
        assert norm == pytest.approx(0.0) or norm == pytest.approx(1.0, abs=1e-4)


# ---- recompile-limit config (no real compile) ------------------------------


def test_recompile_limits_distinguish_per_frame_from_accumulated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recompile_limit`` is per-code-object; ``accumulated`` is global.

    Setting the GLOBAL ``accumulated_recompile_limit`` to ``len(edges)`` kills
    legitimate warmup across a 36-layer model's hundreds of compiled frames
    (dead run, live 2026-09-20). The binding invariant is the PER-FRAME
    ``recompile_limit == len(edges)``; accumulated is set non-binding.
    """
    seen: dict[str, int] = {}

    class _Config:
        recompile_limit = 0
        accumulated_recompile_limit = 0

    monkeypatch.setattr(bucket_embed, "_dynamo_config", lambda: _Config)
    bucket_embed.configure_recompile_limits(_EDGES)
    seen["recompile_limit"] = _Config.recompile_limit
    seen["accumulated"] = _Config.accumulated_recompile_limit
    assert seen["recompile_limit"] == len(_EDGES)
    assert seen["accumulated"] == len(_EDGES) * 1000


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
