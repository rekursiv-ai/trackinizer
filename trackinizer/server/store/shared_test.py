from __future__ import annotations

from typing import cast

from trackinizer.conftest import FakeEngine, RecordingPolicy
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.access_policy import DefaultPolicy
from trackinizer.server.store.shared import _StoreShared


def _engine() -> DatabaseEngine:
    return cast(DatabaseEngine, FakeEngine())


def test_an_engine_alone_builds_it_with_the_defaults() -> None:
    engine = _engine()
    shared = _StoreShared(engine)
    assert shared.engine is engine
    assert isinstance(shared.access_policy, DefaultPolicy)
    assert shared._last_used_bumped_at == {}
    assert shared._verified_bearers == {}


def test_the_access_policy_is_kept() -> None:
    policy = RecordingPolicy()
    shared = _StoreShared(_engine(), access_policy=policy)
    assert shared.access_policy is policy


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
