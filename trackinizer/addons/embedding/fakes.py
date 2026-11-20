"""Test support for the embedding add-on: models without weights, and its database.

- :class:`HashEmbedder` is a model whose vectors are a hash of the text, so a
  test embeds and searches with no weights and the same text always lands on the
  same vector.
- The fake tokenizer and forward stand in for Hugging Face's under
  ``qwen3_0p6b``'s ``_load`` seam.
- :func:`vector_store` puts a test database back as the add-on's migrations
  leave it.

Not a ``_test.py``: it is imported BY tests, so it carries no test collection.
It is not shipped either (``.export/pyproject.delta.toml``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import hashlib
import math

import torch

from trackinizer.addons.embedding.addon import EmbeddingAddon
from trackinizer.conftest import CORE, Baselines, Build, bootstrapped
from trackinizer.server.store.core import Store


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    import pytest

    from trackinizer.lib.postgres import DatabaseEngine


class HashEmbedder:
    """A model whose vectors are a deterministic hash of the text.

    A query embeds as the same text's document does, so a search for a stored text
    finds it first. Records every :meth:`embed_batch` call's texts in ``calls``.

    Args:
      dim: Vector width.

    """

    def __init__(self, *, dim: int = 1_024) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        """Carries the width, so two widths never share a settings key."""
        return f"hash@{self.dim}"

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` in order, recording the call."""
        self.calls.append(list(texts))
        return [_hashed(text, dim=self.dim) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        """Embed a query exactly as its text's document embeds."""
        return _hashed(text, dim=self.dim)


async def vector_store(engine: DatabaseEngine) -> Store:
    """Put ``engine``'s database back as core's and the add-on's migrations leave it.

    Args:
      engine: A test engine, such as ``integ_engine``.

    Returns:
      store: A store over it, every table empty.

    """
    migrations = tuple(EmbeddingAddon.Config().make().manifest().migrations)
    await Baselines(engine, root=CORE, schemas=("public", "embedding")).ready(
        Build(name="embedding", migrations=migrations, fill=bootstrapped),
    )
    return Store(engine)


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

    def fake_load(device: str, *, threads: int) -> tuple[CapturingTokenizer, FakeModel]:
        del device, threads
        return tokenizer, FakeModel(truncated_dim=truncated_dim)

    monkeypatch.setattr(f"{module}._load", fake_load)
    return tokenizer


def _hashed(text: str, *, dim: int) -> list[float]:
    """Return ``text``'s unit vector: a stream seeded by its hash, normalized."""
    # The generator keeps the low 64 bits: the digest's first eight bytes.
    rng = _xorshift_floats(
        int.from_bytes(hashlib.sha256(text.encode()).digest(), "little"),
    )
    vector = [next(rng) for _ in range(dim)]
    norm = math.hypot(*vector)
    return [v / norm for v in vector]


# Stable per-text values without numpy or ``random``'s global state.
def _xorshift_floats(seed: int) -> Iterator[float]:
    """Deterministic ``uint64 -> float64 in [-1, 1)`` generator."""
    state = seed & ((1 << 64) - 1) or 1
    while True:
        state ^= (state << 13) & ((1 << 64) - 1)
        state ^= state >> 7
        state ^= (state << 17) & ((1 << 64) - 1)
        # Map the top 53 bits to a double in [0, 1), then scale to [-1, 1).
        yield (state >> 11) * (2.0 / (1 << 53)) - 1.0
