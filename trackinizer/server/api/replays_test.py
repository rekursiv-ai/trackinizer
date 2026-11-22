"""Consuming requests retain their result until a transport retry can read it."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException

import pytest

from trackinizer.server.api.replays import Replay


class _Operation:
    def __init__(self, values: tuple[str, ...] = ("one", "two")) -> None:
        self.values: list[str] = list(values)
        self.calls = 0

    async def take(self) -> list[str]:
        self.calls += 1
        taken = self.values
        self.values = []
        return taken


@pytest.mark.asyncio
async def test_a_lost_response_can_be_replayed_without_consuming_again() -> None:
    replay = Replay[list[str]]()
    operation = _Operation()
    first = await replay.run("request", fingerprint="session", operation=operation.take)
    again = await replay.run("request", fingerprint="session", operation=operation.take)
    assert first == again == ["one", "two"]
    assert operation.calls == 1
    assert (
        await replay.run("next", fingerprint="session", operation=operation.take) == []
    )


@pytest.mark.asyncio
async def test_one_key_cannot_be_reused_for_a_different_request() -> None:
    replay = Replay[list[str]]()
    operation = _Operation()
    await replay.run("request", fingerprint="session-a", operation=operation.take)
    with pytest.raises(HTTPException) as caught:
        await replay.run("request", fingerprint="session-b", operation=operation.take)
    assert caught.value.status_code == 409
    assert operation.calls == 1


@pytest.mark.asyncio
async def test_concurrent_retries_share_the_pending_delivery() -> None:
    replay = Replay[list[str]]()
    operation = _Operation()
    result = await asyncio.gather(
        replay.run("request", fingerprint="session", operation=operation.take),
        replay.run("request", fingerprint="session", operation=operation.take),
    )
    assert result == [["one", "two"], ["one", "two"]]
    assert operation.calls == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_the_consuming_operation() -> None:
    replay = Replay[list[str]]()
    started, release = asyncio.Event(), asyncio.Event()
    operation = _Held(started=started, release=release)
    waiting = asyncio.create_task(
        replay.run("request", fingerprint="session", operation=operation.take),
    )
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    assert await replay.run(
        "request",
        fingerprint="session",
        operation=operation.take,
    ) == ["one"]
    assert operation.calls == 1


class _Held:
    def __init__(self, *, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release
        self.calls = 0

    async def take(self) -> list[str]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ["one"]


@pytest.mark.asyncio
async def test_capacity_is_refused_before_consumption_and_expiry_releases_it() -> None:
    now = [0.0]
    replay = Replay[list[str]](max_entries=1, retention_sec=10, clock=lambda: now[0])
    first, next_operation = _Operation(), _Operation(("next",))
    await replay.run("first", fingerprint="session", operation=first.take)
    with pytest.raises(HTTPException) as caught:
        await replay.run("next", fingerprint="session", operation=next_operation.take)
    assert caught.value.status_code == 429
    assert next_operation.values == ["next"]
    now[0] = 11.0
    assert await replay.run(
        "next",
        fingerprint="session",
        operation=next_operation.take,
    ) == ["next"]


@pytest.mark.asyncio
async def test_empty_polls_do_not_fill_receipt_capacity() -> None:
    replay = Replay[list[str]](max_entries=1)
    empty, full = _Operation(()), _Operation()
    assert await replay.run("empty", fingerprint="session", operation=empty.take) == []
    assert await replay.run("full", fingerprint="session", operation=full.take) == [
        "one",
        "two",
    ]


@pytest.mark.asyncio
async def test_an_unkeyed_request_remains_a_fresh_delivery() -> None:
    replay = Replay[list[str]]()
    operation = _Operation()
    assert await replay.run(None, fingerprint="session", operation=operation.take) == [
        "one",
        "two",
    ]
    assert await replay.run(None, fingerprint="session", operation=operation.take) == []


@pytest.mark.asyncio
async def test_default_capacity_bounds_retained_consuming_results() -> None:
    replay = Replay[list[str]]()
    for index in range(4_096):
        await replay.run(index, fingerprint="session", operation=_Operation().take)
    operation = _Operation(("kept",))
    with pytest.raises(HTTPException) as caught:
        await replay.run("full", fingerprint="session", operation=operation.take)
    assert caught.value.status_code == 429
    assert operation.values == ["kept"]


@pytest.mark.asyncio
async def test_default_retention_ends_at_fifteen_minutes() -> None:
    now = [0.0]
    replay = Replay[list[str]](clock=lambda: now[0])
    operation = _Operation()
    first = await replay.run("request", fingerprint="session", operation=operation.take)
    now[0] = 899.0
    assert (
        await replay.run("request", fingerprint="session", operation=operation.take)
        == first
    )
    now[0] = 900.0
    assert (
        await replay.run("request", fingerprint="session", operation=operation.take)
        == []
    )
    assert operation.calls == 2


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
