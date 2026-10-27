"""The upload relay: a page's ask waits for the assistant, which takes it and answers."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from trackinizer.server.upload_relay import UploadRelay, UploadsBusyError
from trackinizer.wire.wire_chat_uploads import (
    ChatUploadFile,
    ChatUploadSlot,
    UploadAnswer,
    UploadAsk,
)


_FILES = [ChatUploadFile(name="shot.png", size=4)]
_SLOTS = UploadAnswer(
    files=[
        ChatUploadSlot(
            name="shot.png",
            path="/home/user/uploads/shot.png",
            url="https://box.invalid/files?path=/home/user/uploads/shot.png",
        ),
    ],
)


_CHAT = uuid.UUID(int=2)
_WORKSPACE = uuid.UUID(int=3)


async def _ask(relay: UploadRelay) -> UploadAnswer:
    return await relay.ask(
        canvas_id=uuid.UUID(int=1),
        conversation_id=_CHAT,
        workspace_id=_WORKSPACE,
        asker="ada@example.com",
        asker_role="writer",
        files=_FILES,
    )


@pytest.mark.asyncio
async def test_the_assistant_takes_an_ask_and_its_answer_ends_the_pages_wait() -> None:
    relay = UploadRelay(timeout_sec=5)
    asked = asyncio.create_task(_ask(relay))

    taken = await relay.wait(wait_sec=5)

    assert isinstance(taken, UploadAsk)
    assert (taken.canvas_id, taken.conversation_id, taken.workspace_id) == (
        uuid.UUID(int=1),
        _CHAT,
        _WORKSPACE,
    )
    assert (taken.asker, taken.asker_role, taken.files) == (
        "ada@example.com",
        "writer",
        _FILES,
    )
    assert relay.answer(taken.request_id, _SLOTS)
    assert await asked == _SLOTS


@pytest.mark.asyncio
async def test_a_link_ask_carries_its_action_and_path_to_the_assistant() -> None:
    relay = UploadRelay(timeout_sec=5)
    linked = UploadAnswer(url="https://box.invalid/files?path=/home/user/a.png")
    asked = asyncio.create_task(
        relay.ask(
            canvas_id=uuid.UUID(int=1),
            conversation_id=None,
            workspace_id=_WORKSPACE,
            asker="ada@example.com",
            asker_role="writer",
            action="link",
            path="/home/user/uploads/0a1b2c3d/a.png",
        ),
    )

    taken = await relay.wait(wait_sec=5)

    assert taken is not None
    assert (taken.action, taken.path, taken.files) == (
        "link",
        "/home/user/uploads/0a1b2c3d/a.png",
        [],
    )
    assert relay.answer(taken.request_id, linked)
    assert await asked == linked


@pytest.mark.asyncio
async def test_an_ask_made_before_the_assistant_waits_is_handed_at_once() -> None:
    relay = UploadRelay(timeout_sec=5)
    asked = asyncio.create_task(_ask(relay))
    await asyncio.sleep(0)

    taken = await relay.wait(wait_sec=0.01)

    assert taken is not None
    assert relay.answer(taken.request_id, UploadAnswer(error="no box"))
    assert (await asked).error == "no box"


@pytest.mark.asyncio
async def test_an_ask_no_assistant_answers_times_out_and_is_not_handed_later() -> None:
    relay = UploadRelay(timeout_sec=0.02)

    with pytest.raises(TimeoutError):
        await _ask(relay)

    assert await relay.wait(wait_sec=0.01) is None


@pytest.mark.asyncio
async def test_a_wait_with_no_ask_ends_empty_after_its_time() -> None:
    assert await UploadRelay().wait(wait_sec=0.01) is None


@pytest.mark.asyncio
async def test_two_waits_share_two_asks_one_each() -> None:
    relay = UploadRelay(timeout_sec=5)
    waits = [asyncio.create_task(relay.wait(wait_sec=5)) for _ in range(2)]
    await asyncio.sleep(0)
    asks = [asyncio.create_task(_ask(relay)) for _ in range(2)]

    taken = [each for each in await asyncio.gather(*waits) if each is not None]

    assert len({each.request_id for each in taken}) == 2
    for each in taken:
        assert relay.answer(each.request_id, _SLOTS)
    assert await asyncio.gather(*asks) == [_SLOTS, _SLOTS]


@pytest.mark.asyncio
async def test_an_ask_past_the_most_waiting_is_refused_at_once() -> None:
    relay = UploadRelay(timeout_sec=5, max_waiting=2)
    waiting = [asyncio.create_task(_ask(relay)) for _ in range(2)]
    await asyncio.sleep(0)

    with pytest.raises(UploadsBusyError):
        await _ask(relay)

    for _ in waiting:
        taken = await relay.wait(wait_sec=1)
        assert taken is not None
        assert relay.answer(taken.request_id, _SLOTS)
    assert await asyncio.gather(*waiting) == [_SLOTS, _SLOTS]
    # Once they are answered there is room again.
    again = asyncio.create_task(_ask(relay))
    taken = await relay.wait(wait_sec=1)
    assert taken is not None
    assert relay.answer(taken.request_id, _SLOTS)
    assert await again == _SLOTS


@pytest.mark.asyncio
async def test_only_the_first_answer_to_a_known_ask_is_taken() -> None:
    relay = UploadRelay(timeout_sec=5)
    asked = asyncio.create_task(_ask(relay))
    taken = await relay.wait(wait_sec=5)
    assert taken is not None

    assert relay.answer(taken.request_id, _SLOTS)
    assert not relay.answer(taken.request_id, UploadAnswer(error="late"))
    assert not relay.answer(uuid.uuid4(), _SLOTS)
    assert await asked == _SLOTS


@pytest.mark.asyncio
async def test_a_page_that_leaves_takes_its_ask_with_it() -> None:
    relay = UploadRelay(timeout_sec=5)
    asked = asyncio.create_task(_ask(relay))
    await asyncio.sleep(0)

    asked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked

    assert await relay.wait(wait_sec=0.01) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
