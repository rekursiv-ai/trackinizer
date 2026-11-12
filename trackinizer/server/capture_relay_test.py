"""The relay between an agent's request for a picture and the page that takes it."""

from __future__ import annotations

from typing import Final

import asyncio
import uuid

import pytest

from trackinizer.server.capture_relay import CaptureRelay
from trackinizer.wire.wire_capture import CaptureAnswer


_CANVAS: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _answer(note: str) -> CaptureAnswer:
    return CaptureAnswer(error=note)


async def _ask(relay: CaptureRelay) -> tuple[asyncio.Task[CaptureAnswer], uuid.UUID]:
    """Start a request and return it with the id the pages were sent."""
    sent: list[uuid.UUID] = []
    task = asyncio.create_task(relay.ask(_CANVAS, publish=sent.append))
    await asyncio.sleep(0)
    return task, sent[0]


@pytest.mark.asyncio
async def test_an_answer_reaches_the_request_it_belongs_to() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)

    taken = relay.answer(_CANVAS, request=request, answer=_answer("from a page"))

    assert taken
    assert (await task).error == "from a page"


@pytest.mark.asyncio
async def test_the_first_answer_wins_and_a_second_is_refused() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)

    first = relay.answer(_CANVAS, request=request, answer=_answer("first"))
    second = relay.answer(_CANVAS, request=request, answer=_answer("second"))

    assert (first, second) == (True, False)
    assert (await task).error == "first"


@pytest.mark.asyncio
async def test_an_answer_for_another_canvas_or_an_unknown_request_is_refused() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)

    elsewhere = relay.answer(uuid.uuid4(), request=request, answer=_answer("x"))
    unknown = relay.answer(_CANVAS, request=uuid.uuid4(), answer=_answer("x"))

    assert (elsewhere, unknown) == (False, False)
    relay.answer(_CANVAS, request=request, answer=_answer("done"))
    await task


@pytest.mark.asyncio
async def test_a_request_nobody_answers_times_out_and_is_forgotten() -> None:
    relay = CaptureRelay(timeout_sec=0.01)
    task, request = await _ask(relay)

    with pytest.raises(TimeoutError):
        await task

    assert not relay.answer(_CANVAS, request=request, answer=_answer("late"))


@pytest.mark.asyncio
async def test_a_finished_request_leaves_nothing_waiting() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)
    relay.answer(_CANVAS, request=request, answer=_answer("done"))
    await task

    assert not relay.answer(_CANVAS, request=request, answer=_answer("again"))


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
