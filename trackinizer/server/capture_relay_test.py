"""The relay between an agent's request for a picture and the page that takes it."""

from __future__ import annotations

from typing import Final

import asyncio
import base64
import uuid

from pydantic import ValidationError

import pytest

from trackinizer.server.capture_relay import (
    CaptureAnswer,
    CaptureImage,
    CaptureRelay,
)


_WORKSPACE: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PNG: Final = base64.b64encode(b"\x89PNG\r\n\x1a\n-").decode()
_JPEG: Final = base64.b64encode(b"\xff\xd8\xff-").decode()


def _image(*, data: str = _PNG, mime: str = "image/png") -> dict[str, object]:
    return {"width": 40, "height": 30, "mime": mime, "data": data}


def _answer(note: str) -> CaptureAnswer:
    return CaptureAnswer(error=note)


async def _ask(relay: CaptureRelay) -> tuple[asyncio.Task[CaptureAnswer], uuid.UUID]:
    """Start a request and return it with the id the pages were sent."""
    sent: list[uuid.UUID] = []
    task = asyncio.create_task(relay.ask(_WORKSPACE, publish=sent.append))
    await asyncio.sleep(0)
    return task, sent[0]


@pytest.mark.asyncio
async def test_an_answer_reaches_the_request_it_belongs_to() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)

    taken = relay.answer(_WORKSPACE, request=request, answer=_answer("from a page"))

    assert taken
    assert (await task).error == "from a page"


@pytest.mark.asyncio
async def test_the_first_answer_wins_and_a_second_is_refused() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)

    first = relay.answer(_WORKSPACE, request=request, answer=_answer("first"))
    second = relay.answer(_WORKSPACE, request=request, answer=_answer("second"))

    assert (first, second) == (True, False)
    assert (await task).error == "first"


@pytest.mark.asyncio
async def test_an_answer_for_another_canvas_or_an_unknown_request_is_refused() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)

    elsewhere = relay.answer(uuid.uuid4(), request=request, answer=_answer("x"))
    unknown = relay.answer(_WORKSPACE, request=uuid.uuid4(), answer=_answer("x"))

    assert (elsewhere, unknown) == (False, False)
    relay.answer(_WORKSPACE, request=request, answer=_answer("done"))
    await task


@pytest.mark.asyncio
async def test_a_request_nobody_answers_times_out_and_is_forgotten() -> None:
    relay = CaptureRelay(timeout_sec=0.01)
    task, request = await _ask(relay)

    with pytest.raises(TimeoutError):
        await task

    assert not relay.answer(_WORKSPACE, request=request, answer=_answer("late"))


@pytest.mark.asyncio
async def test_a_finished_request_leaves_nothing_waiting() -> None:
    relay = CaptureRelay()
    task, request = await _ask(relay)
    relay.answer(_WORKSPACE, request=request, answer=_answer("done"))
    await task

    assert not relay.answer(_WORKSPACE, request=request, answer=_answer("again"))


def test_an_answer_is_a_picture_or_a_reason_and_never_both_or_neither() -> None:
    image = CaptureImage.model_validate(_image())

    assert CaptureAnswer(image=image).error is None
    for both_or_neither in ({}, {"image": _image(), "error": "x"}):
        with pytest.raises(ValidationError):
            CaptureAnswer.model_validate(both_or_neither)


@pytest.mark.parametrize(
    "bad",
    [
        _image(data="not base64!"),
        _image(data=_JPEG),
        _image(data=_PNG, mime="image/jpeg"),
        _image(mime="image/gif"),
        {**_image(), "width": 0},
    ],
)
def test_bytes_that_are_not_the_picture_claimed_are_refused(
    bad: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CaptureImage.model_validate(bad)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
