"""Fake tokenizer + ONNX session for Jina embedder tests (no weights, no network).

Shared by ``jina_v5_text_nano_test`` and ``jina_v5_text_small_test``: each patches
its module ``_load`` seam with a capturing tokenizer and a fake session, so the
query/document prompt asymmetry and the mean-pool unit-norm output are asserted
offline. The tokenizer records the exact texts it was handed, exposing the
prefix each path applied.

Not a ``_test.py``: it is imported BY tests, so it carries no test collection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np


if TYPE_CHECKING:
    import pytest


class FakeEncoding:
    """The two fields the Jina encoder reads off a ``tokenizers`` encoding."""

    def __init__(self, *, ids: list[int], attention_mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = attention_mask


class CapturingTokenizer:
    """A fixed 2-token tokenizer that records the exact texts it was handed."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def enable_truncation(self, **_kwargs: object) -> None:
        """No-op: the fake never truncates."""

    def enable_padding(self, **_kwargs: object) -> None:
        """No-op: the fake already emits a fixed length."""

    def encode_batch(self, texts: list[str]) -> list[FakeEncoding]:
        """Record ``texts`` then return a fixed 2-token encoding per text."""
        self.seen.append(list(texts))
        return [FakeEncoding(ids=[0, 0], attention_mask=[1, 1]) for _ in texts]


class FakeSession:
    """Returns a constant non-unit hidden state so the embedder must normalize."""

    def __init__(self, *, dim: int) -> None:
        self._dim = dim
        self.loaded_on = "unset"

    def run(
        self,
        output_names: list[str],
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        """Return a fake ``last_hidden_state`` of constant 3.0 (non-unit).

        Args:
          output_names: Ignored; the fake always returns the one output.
          input_feed: The ONNX inputs; only ``attention_mask``'s shape is read.

        Returns:
          outputs: A single ``(rows, seq, dim)`` constant hidden-state array.

        """
        del output_names
        shape = input_feed["attention_mask"].shape
        rows, seq = int(shape[0]), int(shape[1])  # pyright: ignore[reportAny] -- NumPy shape indexing is dtype-erased.
        return [np.full((rows, seq, self._dim), 3.0, dtype=np.float32)]


def install(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    *,
    dim: int,
) -> CapturingTokenizer:
    """Patch ``<module>._load`` with a capturing tokenizer + fake session.

    Args:
      monkeypatch: The pytest fixture.
      module: Dotted module path whose ``_load`` seam to patch.
      dim: The model's native dim (drives the fake hidden-state width).

    Returns:
      tokenizer: The capturing tokenizer, for asserting the prefixed texts.

    """
    tokenizer = CapturingTokenizer()

    def fake_load(device: str) -> tuple[CapturingTokenizer, FakeSession]:
        del device
        return tokenizer, FakeSession(dim=dim)

    monkeypatch.setattr(f"{module}._load", fake_load)
    return tokenizer
