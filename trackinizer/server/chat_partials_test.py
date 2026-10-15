"""The answers chats' assistants are writing: held, replaced, expired, waited for."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncio

from trackinizer.server.chat_partials import ChatPartials
from trackinizer.wire.wire_science_chat import ChatPartial, ChatPartialState


_CHAT = UUID(int=1)
_OTHER = UUID(int=2)


@dataclass(slots=True, kw_only=True)
class _Clock:
    """A monotonic clock a test moves by hand."""

    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def _partial(text: str, *, turn: str = "t1") -> ChatPartial:
    return ChatPartial(turn=turn, text=text)


def test_a_chats_seq_rises_by_one_per_put_whatever_other_chats_put() -> None:
    partials = ChatPartials()

    first = partials.put(_CHAT, _partial("One"))
    for text in ("a", "b", "c"):
        _ = partials.put(_OTHER, _partial(text))
    second = partials.put(_CHAT, _partial("One two"))

    # A browser holds an integer exactly only below 2**53.
    assert 0 < first <= 2**52
    assert second == first + 1
    assert partials.read(_CHAT) == ChatPartialState(
        seq=second,
        partial=_partial("One two"),
    )
    assert partials.read(UUID(int=3)) == ChatPartialState(seq=0, partial=None)


def test_a_partial_put_after_one_went_takes_a_seq_no_reader_saw() -> None:
    partials = ChatPartials()
    seen = {partials.put(_CHAT, _partial("One")), partials.put(_CHAT, _partial("Two"))}
    partials.clear(_CHAT, turn="t1")

    again = partials.put(_CHAT, _partial("Three"))

    assert again not in seen


def test_a_partial_expires_ttl_sec_after_its_last_put() -> None:
    clock = _Clock()
    partials = ChatPartials(ttl_sec=60.0, clock=clock)
    _ = partials.put(_CHAT, _partial("One"))
    clock.now += 59.0
    seq = partials.put(_CHAT, _partial("One two"))

    clock.now += 59.5
    held = partials.read(_CHAT).seq
    clock.now += 0.5

    assert held == seq
    assert partials.read(_CHAT) == ChatPartialState(seq=0, partial=None)


def test_a_clear_drops_only_the_partial_of_its_own_turn() -> None:
    partials = ChatPartials()
    _ = partials.put(_CHAT, _partial("Next", turn="t2"))

    partials.clear(_CHAT, turn="t1")
    kept = partials.read(_CHAT).partial
    partials.clear(_CHAT, turn="t2")

    assert kept == _partial("Next", turn="t2")
    assert partials.read(_CHAT).partial is None


def test_a_put_past_the_most_drops_the_partial_put_longest_ago() -> None:
    partials = ChatPartials(most=256)
    chats = [UUID(int=number) for number in range(1, 258)]
    for chat in chats[:256]:
        _ = partials.put(chat, _partial("x"))
    _ = partials.put(chats[0], _partial("again"))

    _ = partials.put(chats[256], _partial("x"))

    assert partials.read(chats[0]).partial == _partial("again")
    assert partials.read(chats[1]).partial is None
    assert partials.read(chats[256]).partial == _partial("x")


def test_a_held_read_ends_on_the_next_put() -> None:
    async def scenario() -> ChatPartialState:
        partials = ChatPartials(first_seq=lambda: 1)
        seq = partials.put(_CHAT, _partial("One"))
        held = asyncio.create_task(partials.wait(_CHAT, after=seq, wait_sec=25.0))
        await asyncio.sleep(0)
        _ = partials.put(_CHAT, _partial("One two"))
        return await asyncio.wait_for(held, 1)

    assert asyncio.run(scenario()) == ChatPartialState(
        seq=2,
        partial=_partial("One two"),
    )


def test_a_held_read_ends_when_its_turn_is_cleared() -> None:
    async def scenario() -> ChatPartialState:
        partials = ChatPartials()
        seq = partials.put(_CHAT, _partial("One"))
        held = asyncio.create_task(partials.wait(_CHAT, after=seq, wait_sec=25.0))
        await asyncio.sleep(0)
        partials.clear(_CHAT, turn="t1")
        return await asyncio.wait_for(held, 1)

    assert asyncio.run(scenario()) == ChatPartialState(seq=0, partial=None)


def test_a_read_after_another_seq_answers_at_once_and_one_at_it_times_out() -> None:
    async def scenario() -> tuple[ChatPartialState, ChatPartialState]:
        partials = ChatPartials(first_seq=lambda: 1)
        seq = partials.put(_CHAT, _partial("One"))
        behind = await asyncio.wait_for(
            partials.wait(_CHAT, after=0, wait_sec=25.0),
            1,
        )
        current = await partials.wait(_CHAT, after=seq, wait_sec=0.001)
        return behind, current

    behind, current = asyncio.run(scenario())

    assert behind == ChatPartialState(seq=1, partial=_partial("One"))
    assert current == behind


def test_a_held_read_ends_when_its_partial_expires() -> None:
    clock = _Clock()

    async def scenario() -> ChatPartialState:
        partials = ChatPartials(ttl_sec=60.0, clock=clock)
        seq = partials.put(_CHAT, _partial("One"))
        clock.now += 59.999
        held = asyncio.create_task(partials.wait(_CHAT, after=seq, wait_sec=25.0))
        await asyncio.sleep(0)
        # The hold was cut to the 1 ms the partial had left, which has now run out.
        clock.now += 1.0
        return await asyncio.wait_for(held, 1)

    assert asyncio.run(scenario()) == ChatPartialState(seq=0, partial=None)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
