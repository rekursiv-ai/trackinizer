"""The device login's in-process limits, on a clock the test moves."""

from __future__ import annotations

import uuid

import pytest

from trackinizer.server.api.device_limits import (
    CODES_PER_ADDRESS,
    CodeRequests,
    WrongEntries,
)
from trackinizer.wire.wire_device import DEVICE_CODE_TTL_SEC, WRONG_ENTRIES_LOCK


_PERSON = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_the_sixth_entry_inside_a_window_is_refused_and_not_counted() -> None:
    entries = WrongEntries(clock=_Clock())

    allowed = [entries.reserve(_PERSON) for _ in range(WRONG_ENTRIES_LOCK + 2)]

    assert allowed == [True] * WRONG_ENTRIES_LOCK + [False, False]
    assert len(entries.failures[_PERSON]) == WRONG_ENTRIES_LOCK


def test_a_forgiven_entry_frees_its_place_and_forgiving_nothing_changes_nothing() -> (
    None
):
    entries = WrongEntries(clock=_Clock())
    entries.forgive(_PERSON)
    for _ in range(WRONG_ENTRIES_LOCK):
        assert entries.reserve(_PERSON)

    entries.forgive(_PERSON)

    assert entries.reserve(_PERSON)
    assert not entries.reserve(_PERSON)
    assert entries.reserve(_OTHER)


def test_entries_older_than_a_codes_life_no_longer_count() -> None:
    clock = _Clock()
    entries = WrongEntries(clock=clock)
    for _ in range(WRONG_ENTRIES_LOCK):
        entries.reserve(_PERSON)

    clock.now += DEVICE_CODE_TTL_SEC + 1

    assert entries.reserve(_PERSON)


def test_each_address_has_its_own_ceiling_that_the_window_lifts() -> None:
    clock = _Clock()
    asks = CodeRequests(clock=clock)

    first = [asks.allow("203.0.113.9") for _ in range(CODES_PER_ADDRESS + 1)]

    assert first == [True] * CODES_PER_ADDRESS + [False]
    assert asks.allow("203.0.113.10")
    clock.now += DEVICE_CODE_TTL_SEC + 1
    assert asks.allow("203.0.113.9")


@pytest.mark.parametrize(("tracked", "kept"), [(1_024, 1_025), (1_025, 1)])
def test_addresses_that_went_quiet_are_dropped_only_once_over_a_thousand_are_tracked(
    tracked: int,
    kept: int,
) -> None:
    clock = _Clock()
    asks = CodeRequests(clock=clock)
    for number in range(tracked):
        asks.allow(f"198.51.{number // 256}.{number % 256}")

    clock.now += DEVICE_CODE_TTL_SEC + 1
    asks.allow("203.0.113.9")

    assert len(asks.asks) == kept


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
