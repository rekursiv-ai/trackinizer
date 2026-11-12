"""Tests for the Qwen-family fakes, so a broken fake cannot pass a model's test."""

from __future__ import annotations

from typing import TYPE_CHECKING

import torch

from trackinizer.server.embedders import qwen3_0p6b
from trackinizer.server.embedders.qwen_fakes import (
    CapturingTokenizer,
    FakeBatch,
    FakeModel,
    FakeTokenizer,
    install_capturing,
)


if TYPE_CHECKING:
    import pytest


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

    loaded_tokenizer, model = qwen3_0p6b._load("cpu")

    assert loaded_tokenizer is tokenizer
    assert isinstance(model, FakeModel)
    assert model.native_dim == 5 + 512


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
