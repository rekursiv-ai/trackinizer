"""Each owner's host-list version: read at once, waited on, bumped by a write."""

from __future__ import annotations

import asyncio

import pytest

from trackinizer.server.host_changes import HostChanges


pytestmark = pytest.mark.asyncio

_ADA = "ada@x.y"
_BOB = "bob@x.y"


async def test_a_version_the_caller_does_not_hold_is_answered_at_once() -> None:
    changes = HostChanges()
    now = changes.version(_ADA)

    async with asyncio.timeout(5):
        assert await changes.wait(_ADA, since=None, wait_sec=3600) == now
        assert await changes.wait(_ADA, since=now - 1, wait_sec=3600) == now


async def test_a_wait_ends_the_moment_its_owner_is_bumped() -> None:
    changes = HostChanges()
    now = changes.version(_ADA)
    waiting = asyncio.ensure_future(changes.wait(_ADA, since=now, wait_sec=3600))
    await asyncio.sleep(0)
    assert not waiting.done()

    changes.bump(_ADA)

    async with asyncio.timeout(5):
        assert await waiting == now + 1


async def test_another_owners_bump_wakes_no_one_and_the_wait_ends_unchanged() -> None:
    changes = HostChanges()
    now = changes.version(_ADA)
    waiting = asyncio.ensure_future(changes.wait(_ADA, since=now, wait_sec=0.05))
    await asyncio.sleep(0)

    changes.bump(_BOB)
    await asyncio.sleep(0)
    assert not waiting.done()

    async with asyncio.timeout(5):
        assert await waiting == now
    assert changes.version(_BOB) == now + 1


async def test_every_wait_of_an_owner_wakes_and_a_later_one_holds_again() -> None:
    changes = HostChanges()
    now = changes.version(_ADA)
    waits = [
        asyncio.ensure_future(changes.wait(_ADA, since=now, wait_sec=3600))
        for _ in range(3)
    ]
    await asyncio.sleep(0)

    changes.bump(_ADA)
    async with asyncio.timeout(5):
        assert await asyncio.gather(*waits) == [now + 1] * 3
        assert await changes.wait(_ADA, since=now + 1, wait_sec=0.01) == now + 1


async def test_a_restarted_server_never_holds_a_version_kept_from_before() -> None:
    before = HostChanges()
    kept = before.version(_ADA)

    after = HostChanges()

    assert after.version(_ADA) != kept
    async with asyncio.timeout(5):
        assert await after.wait(_ADA, since=kept, wait_sec=3600) == after.version(_ADA)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
