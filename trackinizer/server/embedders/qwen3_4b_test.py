"""Unit tests for QwenEmbedder that never download the model.

These pin the 4B model's constants and the registry's compile policy for it,
against a fake tiny model injected at the module's ``_load`` seam, so they run in
milliseconds and need no network. The shared recipe (lazy load, truncate-then-
normalize, prefixes, bucketing) is exercised in ``qwen_family_test``.
"""

from __future__ import annotations

import huggingface_hub
import pytest
import torch

from trackinizer.server.embedders import qwen3_4b, qwen_family, registry
from trackinizer.server.embedders.qwen3_4b import (
    QWEN_MODEL_ID,
    QWEN_TRUNCATED_DIM,
    QwenEmbedder,
)
from trackinizer.types.embedder import Embedder


class _FakeTokenizer:
    """Minimal left-padding tokenizer: maps texts to a fixed 2-token batch."""

    def __call__(
        self,
        texts: list[str],
        **_kwargs: object,
    ) -> _FakeBatch:
        rows = len(texts)
        return _FakeBatch(
            {
                "input_ids": torch.zeros((rows, 2), dtype=torch.long),
                "attention_mask": torch.ones((rows, 2), dtype=torch.long),
            },
        )


class _FakeBatch(dict[str, torch.Tensor]):
    """A ``dict``-like batch that supports ``.to(device)`` and ``**batch``."""

    def to(self, device: str) -> _FakeBatch:
        del device
        return self


class _FakeModel:
    """Returns a hidden state whose last-token vector has a heavy >1024 tail."""

    native_dim = QWEN_TRUNCATED_DIM + 512

    def __init__(self) -> None:
        self.loaded_on = "unset"
        self.rows_seen: list[int] = []

    def __call__(self, **batch: torch.Tensor) -> _FakeOutput:

        rows = batch["attention_mask"].shape[0]
        self.rows_seen.append(rows)
        prefix = torch.ones((rows, 2, QWEN_TRUNCATED_DIM))
        tail = torch.full((rows, 2, self.native_dim - QWEN_TRUNCATED_DIM), 8.0)
        return _FakeOutput(last_hidden_state=torch.cat([prefix, tail], dim=2))


class _FakeOutput:
    def __init__(self, *, last_hidden_state: torch.Tensor) -> None:
        self.last_hidden_state = last_hidden_state


def _install_fake(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    """Patch the module ``_load`` seam to return the fake tokenizer+model."""
    model = _FakeModel()

    def fake_load(device: str) -> tuple[_FakeTokenizer, _FakeModel]:
        model.loaded_on = device
        return _FakeTokenizer(), model

    monkeypatch.setattr(
        "trackinizer.server.embedders.qwen3_4b._load",
        fake_load,
    )
    return model


class _FakeInductorConfig:
    freezing = False


def _uncompiled(model: _FakeModel, *, dynamic: bool) -> _FakeModel:
    """Stand in for ``torch.compile``: the model itself, so its calls stay visible."""
    del dynamic
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("throughput", "device", "compiled", "frozen"),
    [
        (True, "cpu", True, True),
        (True, "cuda:0", True, False),
        (False, "cpu", False, False),
    ],
    ids=["follow on CPU", "GPU backfill", "server query path"],
)
async def test_a_throughput_embedder_compiles_and_freezes_only_on_cpu(
    monkeypatch: pytest.MonkeyPatch,
    *,
    throughput: bool,
    device: str,
    compiled: bool,
    frozen: bool,
) -> None:
    """The backfill builder compiles on every device; only a CPU compile freezes.

    Freezing folds the weights into the graph so Inductor prepacks them for the
    CPU's matmul; the server's query embedder stays eager, so it pays no compile.
    """
    _install_fake(monkeypatch)
    compiles: list[bool] = []
    config = _FakeInductorConfig()

    def fake_compile(model: _FakeModel, *, dynamic: bool) -> _FakeModel:
        compiles.append(dynamic)
        return model

    monkeypatch.setattr(torch, "compile", fake_compile)
    monkeypatch.setattr(qwen_family, "_inductor_config", lambda: config)
    embedder = (
        registry.build_backfill_embedder(
            "qwen3-embedding-4b@1024",
            device=device,
            batch_size=8,
        )
        if throughput
        else registry.build_session_embedder("qwen3-embedding-4b@1024")
    )
    assert embedder is not None

    _ = await embedder.embed("compile me")

    assert compiles == ([True] if compiled else [])
    assert config.freezing is frozen


@pytest.mark.asyncio
@pytest.mark.parametrize(("throughput", "rows"), [(True, [2, 3]), (False, [1, 3])])
async def test_a_compiled_forward_never_sees_a_batch_of_one(
    monkeypatch: pytest.MonkeyPatch,
    *,
    throughput: bool,
    rows: list[int],
) -> None:
    """One text runs as two rows when compiled; Inductor's CPU code fails on one.

    The vectors are still one per text, and an eager forward is left alone.
    """
    model = _install_fake(monkeypatch)
    monkeypatch.setattr(torch, "compile", _uncompiled)
    monkeypatch.setattr(qwen_family, "_inductor_config", _FakeInductorConfig)
    embedder = (
        registry.build_backfill_embedder(
            "qwen3-embedding-4b@1024",
            device="cpu",
            batch_size=8,
        )
        if throughput
        else registry.build_session_embedder("qwen3-embedding-4b@1024")
    )
    assert isinstance(embedder, qwen_family.QwenFamilyEmbedder)

    one = await embedder.embed("alone")
    three = await embedder.embed_batch(["a", "b", "c"])

    assert model.rows_seen == rows
    assert len(one) == QWEN_TRUNCATED_DIM
    assert len(three) == 3


def test_satisfies_the_embedder_protocol() -> None:
    """QwenEmbedder is an ``Embedder`` structurally: name, dim, embed."""
    assert isinstance(QwenEmbedder(), Embedder)


def test_name_and_dim_are_the_frozen_constants() -> None:
    embedder = QwenEmbedder()
    assert embedder.dim == QWEN_TRUNCATED_DIM == 1024
    # ``qwen3-embedding-4b``, not ``qwen3-4b`` (the chat LLM). This keys
    # session_embeddings rows, so the exact spelling is load-bearing.
    assert embedder.name == "qwen3-embedding-4b@1024"


@pytest.mark.asyncio
async def test_the_model_loads_through_this_modules_seam_on_its_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``QwenEmbedder`` loads via ``qwen3_4b._load``, on the device it was given."""
    model = _install_fake(monkeypatch)

    vector = await QwenEmbedder(device="cpu").embed("first")

    assert model.loaded_on == "cpu"
    assert len(vector) == QWEN_TRUNCATED_DIM


def test_weights_present_probes_its_own_models_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[tuple[str, str]] = []

    def probe(repo: str, filename: str) -> str | None:
        probes.append((repo, filename))
        return "/hf/config.json"

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", probe)

    assert qwen3_4b.weights_present() is True
    assert probes == [(QWEN_MODEL_ID, "config.json")]


def test_load_builds_its_own_model_on_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str]] = []
    built = (object(), object())

    def hf_load(model_id: str, device: str) -> tuple[object, object]:
        loads.append((model_id, device))
        return built

    monkeypatch.setattr(qwen3_4b, "hf_load", hf_load)

    assert qwen3_4b._load("cuda:0") is built
    assert loads == [(QWEN_MODEL_ID, "cuda:0")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
