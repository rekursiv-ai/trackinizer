"""Tests for the embedding fakes, so a broken fake cannot pass a model's test."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import math

import pytest
import torch

from trackinizer.addons.embedding import qwen3_0p6b
from trackinizer.addons.embedding.fakes import (
    CapturingTokenizer,
    FakeBatch,
    FakeModel,
    FakeTokenizer,
    HashEmbedder,
    _xorshift_floats,
    install_capturing,
    vector_store,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PostgresEngine


@pytest.mark.asyncio
async def test_a_text_embeds_to_the_same_unit_vector_every_time() -> None:
    model = HashEmbedder(dim=16)

    first, other = await model.embed_batch(["hello", "goodbye"])
    [again] = await HashEmbedder(dim=16).embed_batch(["hello"])

    assert (model.name, model.dim) == ("hash@16", 16)
    assert first == again
    assert first != other
    assert len(first) == 16
    assert abs(cast(float, sum(v * v for v in first) ** 0.5) - 1.0) < 1e-9
    assert model.calls == [["hello", "goodbye"]]


@pytest.mark.asyncio
async def test_a_query_embeds_as_its_document_does() -> None:
    model = HashEmbedder(dim=8)

    assert [await model.embed_query("q")] == await model.embed_batch(["q"])


@pytest.mark.asyncio
async def test_the_vector_is_the_hash_seeded_stream_normalized() -> None:
    # The first eight bytes of sha256(b"a"), ca978112ca1bbdca, read little-endian.
    rng = _xorshift_floats(0xCABD1BCA128197CA)
    stream = [next(rng), next(rng)]
    norm = math.hypot(*stream)

    assert await HashEmbedder(dim=2).embed_query("a") == [v / norm for v in stream]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_vector_store_holds_the_empty_vector_table(
    integ_engine: PostgresEngine,
) -> None:
    store = await vector_store(integ_engine)

    async with store.engine.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM embedding.vectors")

    assert (store.engine, count) == (integ_engine, 0)


def test_xorshift_is_deterministic_per_seed() -> None:
    a, b, c = _xorshift_floats(42), _xorshift_floats(42), _xorshift_floats(43)

    first = [next(a) for _ in range(5)]

    assert first == [next(b) for _ in range(5)]
    assert first != [next(c) for _ in range(5)]


@pytest.mark.parametrize("seed", [0, 1, 1 << 64])
def test_xorshift_stays_in_the_unit_interval_for_any_seed(seed: int) -> None:
    rng = _xorshift_floats(seed)

    values = [next(rng) for _ in range(50)]

    assert all(-1.0 <= v < 1.0 for v in values)
    assert len(set(values)) == 50


def test_a_batch_ignores_device_placement() -> None:
    batch = FakeBatch({"input_ids": torch.zeros(1, 2)})

    assert batch.to("cuda:0") is batch


def test_the_tokenizer_maps_every_text_to_two_unmasked_tokens() -> None:
    batch = FakeTokenizer()(["a", "bb", "ccc"], padding=True)

    assert batch["input_ids"].tolist() == [[0, 0]] * 3
    assert batch["attention_mask"].tolist() == [[1, 1]] * 3


def test_the_capturing_tokenizer_records_each_call_then_tokenizes() -> None:
    tokenizer = CapturingTokenizer()

    first = tokenizer(["a"])
    _ = tokenizer(["b", "c"], truncation=True)

    assert tokenizer.seen == [["a"], ["b", "c"]]
    assert first["attention_mask"].shape == (1, 2)


def test_the_model_puts_most_mass_past_the_truncated_dim() -> None:
    model = FakeModel(truncated_dim=3)

    hidden = model(**FakeTokenizer()(["a", "b"])).last_hidden_state

    assert (model.native_dim, model.loaded_on) == (3 + 512, "unset")
    assert hidden.shape == (2, 2, 3 + 512)
    assert hidden[..., :3].unique().tolist() == [1.0]
    assert hidden[..., 3:].unique().tolist() == [8.0]


def test_install_capturing_patches_the_module_load_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer = install_capturing(
        monkeypatch,
        qwen3_0p6b.__name__,
        truncated_dim=5,
    )

    loaded_tokenizer, model = qwen3_0p6b._load("cpu", threads=1)

    assert loaded_tokenizer is tokenizer
    assert isinstance(model, FakeModel)
    assert model.native_dim == 5 + 512


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
