"""Fake tokenizer + model for Qwen-family embedder tests (no weights, no network).

Shared by the per-model test files (``qwen3_0p6b_test``, ``qwen3_8b_test``,
``octen_8b_test``) so each can patch its module ``_load`` seam with a tiny model
and assert the model-specific contract (name, dim, Octen's ``"- "`` document
prefix) without duplicating the fakes. The full recipe (truncate-before-
normalize, lazy load, batch order) is exercised once in ``qwen3_4b_test``.

Not a ``_test.py``: it is imported BY tests, so it carries no test collection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import torch


if TYPE_CHECKING:
    import pytest


class FakeBatch(dict[str, "torch.Tensor"]):
    """A ``dict``-like batch supporting ``.to(device)`` and ``**batch``."""

    def to(self, device: str) -> FakeBatch:
        """Return self; the fake ignores device placement."""
        del device
        return self


class FakeTokenizer:
    """Minimal left-padding tokenizer: maps texts to a fixed 2-token batch."""

    def __call__(self, texts: list[str], **_kwargs: object) -> FakeBatch:
        """Return a fixed 2-token batch for ``texts``."""
        rows = len(texts)
        return FakeBatch(
            {
                "input_ids": torch.zeros((rows, 2), dtype=torch.long),
                "attention_mask": torch.ones((rows, 2), dtype=torch.long),
            },
        )


class CapturingTokenizer(FakeTokenizer):
    """A tokenizer that records the exact texts it was handed."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    @override
    def __call__(self, texts: list[str], **kwargs: object) -> FakeBatch:
        """Record ``texts`` then delegate to the fixed-batch parent."""
        self.seen.append(list(texts))
        return super().__call__(texts, **kwargs)


class FakeModel:
    """Returns a hidden state whose last-token vector has a heavy >dim tail.

    Native width is ``truncated_dim + 512``: the prefix is ``1`` and the tail
    ``8``, so normalizing the full vector then slicing leaves the prefix far
    short of unit norm -- only slice-then-normalize yields a unit vector.
    """

    def __init__(self, *, truncated_dim: int) -> None:
        self.native_dim = truncated_dim + 512
        self._truncated_dim = truncated_dim
        self.loaded_on = "unset"

    def __call__(self, **batch: torch.Tensor) -> FakeOutput:
        """Return a fake forward output with the heavy-tail hidden state."""
        rows = batch["attention_mask"].shape[0]
        prefix = torch.ones((rows, 2, self._truncated_dim))
        tail = torch.full((rows, 2, self.native_dim - self._truncated_dim), 8.0)
        return FakeOutput(last_hidden_state=torch.cat([prefix, tail], dim=2))


class FakeOutput:
    """The one field the pooler reads off a forward output."""

    def __init__(self, *, last_hidden_state: torch.Tensor) -> None:
        self.last_hidden_state = last_hidden_state


def install_capturing(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    *,
    truncated_dim: int,
) -> CapturingTokenizer:
    """Patch ``<module>._load`` with a capturing tokenizer + fake model.

    Args:
      monkeypatch: The pytest fixture.
      module: Dotted module path whose ``_load`` seam to patch.
      truncated_dim: The model's output dim (drives the fake's native width).

    Returns:
      tokenizer: The capturing tokenizer, for asserting the texts it saw.

    """
    tokenizer = CapturingTokenizer()

    def fake_load(device: str) -> tuple[CapturingTokenizer, FakeModel]:
        del device
        return tokenizer, FakeModel(truncated_dim=truncated_dim)

    monkeypatch.setattr(f"{module}._load", fake_load)
    return tokenizer
