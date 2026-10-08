"""Tests for the connection identity shared clients are keyed on."""

from __future__ import annotations

from typing import Final

from trackinizer.trax.client_cache import Target


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
