"""Tests for the connection identity shared clients are keyed on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import pytest

from trackinizer.trax import client_cache
from trackinizer.trax.client_cache import Target


if TYPE_CHECKING:
    from collections.abc import Iterator


_KEY: Final = "tok-SENTINEL-5e2a10"
"""A recognisable key, so a leak is found by substring."""


def test_target_repr_and_str_hold_no_key_text() -> None:
    """A traceback or log line that shows a ``Target`` must not print its key."""
    target = Target(url="http://prod:9000", author="alice", api_key=_KEY)

    assert _KEY not in repr(target)
    assert _KEY not in str(target)


def test_target_still_keys_on_the_api_key() -> None:
    """Hiding the key from ``repr`` must not merge two keys into one client."""
    first = Target(url="http://prod:9000", author="", api_key="key-a")
    second = Target(url="http://prod:9000", author="", api_key="key-b")

    assert first != second
    assert len({first, second}) == 2


class _FakeClient:
    """Stands in for ``Client``: records its identity and whether it was closed."""

    def __init__(self, url: str, *, author: str, api_key: str) -> None:
        self.identity = (url, author, api_key)
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Build ``_FakeClient`` in place of a real pool, and empty the cache after."""
    monkeypatch.setattr(client_cache, "Client", _FakeClient)
    yield
    client_cache.close_clients()


@pytest.mark.usefixtures("fake_client")
class TestSharedClient:
    """One client per connection identity, until ``close_clients`` drops them."""

    def test_same_target_returns_the_same_client(self) -> None:
        target = Target(url="http://prod:9000", author="alice", api_key="k")

        first = client_cache.shared_client(target)
        again = client_cache.shared_client(
            Target(url="http://prod:9000", author="alice", api_key="k"),
        )

        assert first is again
        assert cast(_FakeClient, first).identity == ("http://prod:9000", "alice", "k")

    def test_distinct_targets_get_distinct_clients(self) -> None:
        first = client_cache.shared_client(
            Target(url="http://a", author="", api_key=""),
        )
        second = client_cache.shared_client(
            Target(url="http://b", author="", api_key=""),
        )

        assert first is not second

    def test_close_clients_closes_each_and_forgets_it(self) -> None:
        target = Target(url="http://prod:9000", author="", api_key="")
        before = cast(_FakeClient, client_cache.shared_client(target))

        client_cache.close_clients()
        after = client_cache.shared_client(target)

        assert before.closed
        assert after is not before


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
