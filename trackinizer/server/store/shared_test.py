from __future__ import annotations

from typing import cast

import pytest

from trackinizer.conftest import FakeEngine, RecordingPolicy
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.access_policy import DefaultPolicy
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.shared import _StoreShared


class _Fixed:
    """An embedder that answers one fixed vector under a chosen name."""

    def __init__(self, name: str, *, dim: int = 384, value: float = 0.0) -> None:
        self._name = name
        self.dim = dim
        self.value = value

    @property
    def name(self) -> str:
        return self._name

    async def embed(self, text: str) -> list[float]:
        del text
        return [self.value] * self.dim


def _engine() -> DatabaseEngine:
    return cast(DatabaseEngine, FakeEngine())


def test_single_embedder_is_wrapped_and_defaults_apply() -> None:
    engine = _engine()
    embedder = StubEmbedder()
    shared = _StoreShared(engine, embedder)
    assert shared.embedders == (embedder,)
    assert shared.engine is engine
    assert isinstance(shared.access_policy, DefaultPolicy)
    assert shared._last_used_bumped_at == {}
    assert shared._verified_bearers == {}


def test_sequence_keeps_order_and_policy() -> None:
    first, second = _Fixed("a"), _Fixed("b")
    policy = RecordingPolicy()
    shared = _StoreShared(_engine(), [first, second], access_policy=policy)
    assert shared.embedders == (first, second)
    assert shared.access_policy is policy


def test_empty_embedders_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one embedder"):
        _StoreShared(_engine(), [])


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ValueError, match=r"duplicates: \['a'\]"):
        _StoreShared(_engine(), [_Fixed("a"), _Fixed("b"), _Fixed("a")])


def test_mismatched_dim_is_refused() -> None:
    with pytest.raises(
        ValueError,
        match=r"dim=8; got mismatched \(name, dim\) pairs: \[\('a', 384\)\]",
    ):
        _StoreShared(_engine(), [_Fixed("a"), _Fixed("b", dim=8)], embedding_dim=8)


@pytest.mark.asyncio
async def test_embed_all_pairs_each_name_with_its_vector() -> None:
    shared = _StoreShared(
        _engine(),
        [_Fixed("a", dim=2, value=1.0), _Fixed("b", dim=2, value=2.0)],
        embedding_dim=2,
    )
    assert await shared._embed_all("text") == [
        ("a", [1.0, 1.0]),
        ("b", [2.0, 2.0]),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
