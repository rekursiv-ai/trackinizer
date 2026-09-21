"""Fake tokenizer + ONNX session for Qwen-family embedder tests (no weights).

Shared by the per-model test files (``qwen3_0p6b_test``, ``qwen3_8b_test``,
``octen_8b_test``) so each can patch its module ``_load`` seam with a tiny
session and assert the model-specific contract (name, dim, Octen's ``"- "``
document prefix) without duplicating the fakes. The full recipe (truncate-
before-normalize, lazy load, batch order) is exercised once in ``qwen3_4b_test``.

Not a ``_test.py``: it is imported BY tests, so it carries no test collection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import numpy as np


if TYPE_CHECKING:
    import pytest


class FakeEncoding:
    """The two fields the base encoder reads off a ``tokenizers`` encoding."""

    def __init__(self, *, ids: list[int], attention_mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = attention_mask


class FakeTokenizer:
    """Minimal left-padding tokenizer: maps each text to a fixed 2-token row."""

    def enable_truncation(self, **_kwargs: object) -> None:
        """No-op: the fake never truncates."""

    def enable_padding(self, **_kwargs: object) -> None:
        """No-op: the fake already emits a fixed length."""

    def no_padding(self) -> None:
        """No-op: the fake already emits a fixed length."""

    def encode(self, text: str) -> FakeEncoding:
        """Return a fixed 2-token encoding for ``text``."""
        del text
        return FakeEncoding(ids=[0, 0], attention_mask=[1, 1])

    def encode_batch(self, texts: list[str]) -> list[FakeEncoding]:
        """Return a fixed 2-token encoding per text."""
        return [self.encode(text) for text in texts]


class CapturingTokenizer(FakeTokenizer):
    """A tokenizer that records the exact texts it was handed."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    @override
    def encode_batch(self, texts: list[str]) -> list[FakeEncoding]:
        """Record ``texts`` then delegate to the fixed-batch parent."""
        self.seen.append(list(texts))
        return super().encode_batch(texts)


class FakeSession:
    """Returns a hidden state whose last-token vector has a heavy >dim tail.

    Native width is ``truncated_dim + 512``: the prefix is ``1`` and the tail
    ``8``, so normalizing the full vector then slicing leaves the prefix far
    short of unit norm -- only slice-then-normalize yields a unit vector.
    """

    def __init__(self, *, truncated_dim: int) -> None:
        self.native_dim = truncated_dim + 512
        self._truncated_dim = truncated_dim
        self.loaded_on = "unset"

    def run(
        self,
        output_names: list[str],
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        """Return a fake ``last_hidden_state`` with the heavy-tail vector.

        Args:
          output_names: Ignored; the fake always returns the one output.
          input_feed: The ONNX inputs; only ``attention_mask``'s shape is read.

        Returns:
          outputs: A single ``(rows, seq, native_dim)`` hidden-state array.

        """
        del output_names
        shape = input_feed["attention_mask"].shape
        rows, seq = int(shape[0]), int(shape[1])  # pyright: ignore[reportAny] -- NumPy shape indexing is dtype-erased.
        prefix = np.ones((rows, seq, self._truncated_dim), dtype=np.float32)
        tail = np.full(
            (rows, seq, self.native_dim - self._truncated_dim),
            8.0,
            dtype=np.float32,
        )
        return [np.concatenate([prefix, tail], axis=2)]


def install_capturing(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    *,
    truncated_dim: int,
) -> CapturingTokenizer:
    """Patch ``<module>._load`` with a capturing tokenizer + fake session.

    Args:
      monkeypatch: The pytest fixture.
      module: Dotted module path whose ``_load`` seam to patch.
      truncated_dim: The model's output dim (drives the fake's native width).

    Returns:
      tokenizer: The capturing tokenizer, for asserting the texts it saw.

    """
    tokenizer = CapturingTokenizer()

    def fake_load(device: str) -> tuple[CapturingTokenizer, FakeSession]:
        del device
        return tokenizer, FakeSession(truncated_dim=truncated_dim)

    monkeypatch.setattr(f"{module}._load", fake_load)
    return tokenizer
