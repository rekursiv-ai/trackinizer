"""What ``ToolLines`` writes is what sagent reads, and sagent's own lines pass."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import asyncio
import json
import math

import pytest

from trackinizer.sagent.host.wire import (
    Ack,
    Bye,
    Call,
    Cancel,
    Describe,
    Hello,
    Message,
    Ping,
    Result,
    encoded,
    received,
)
from trackinizer.sagent.session.types import ToolErrorResult
from trackinizer.server.config import Config
from trackinizer.server.tool_lines import ToolLines, authorize, refused_line


if TYPE_CHECKING:
    from treekle import PlainTree


_TOKEN = "t"  # noqa: S105 -- A fixture.
_HELLO = Hello(
    version=1,
    token=_TOKEN,
    session_id="s_1-A",
    client="c",
    tools=("Bash", "Read"),
)


async def _received(line: bytes) -> Message | None:
    reader = asyncio.StreamReader()
    reader.feed_data(line)
    reader.feed_eof()
    return await received(reader)


def _kind(message: Message) -> str:
    return type(message).__name__


def _lines() -> ToolLines:
    return ToolLines(
        asker="me@x",
        machine="m1",
        authorize=authorize,
        line_bytes=Config().tool_line_bytes,
    )


def _call(**arguments: PlainTree) -> Call:
    return Call(
        id="c1",
        agent="a",
        window="w",
        tool="Write",
        arguments=arguments,
        address_book=("a", "b"),
    )


@pytest.mark.asyncio
async def test_sagent_reads_a_refusal_as_a_tool_error() -> None:
    message = await _received(refused_line("c1", "no Bash"))
    assert isinstance(message, Result)
    assert message.id == "c1"
    assert isinstance(message.record, ToolErrorResult)
    assert message.record.content == "no Bash"
    assert message.record.is_error is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        _HELLO,
        Hello(version=1, token=_TOKEN, session_id="s", client="c"),
        _call(path="/x"),
        _call(),
        Call(id="c2", agent="a", window="w", tool="Bash", arguments={"n": 1}),
        _call(nested={"a": [1, {"b": (2, 3)}], "set": [1.5, True, None]}),
        _call(not_a_number=math.inf, surrogate="\ud800", big=2**64, text="é\u0000"),
        Cancel(id="c1"),
        Ack(id="c1"),
        Describe(id="d1", tool="Bash"),
        Ping(),
        Bye(),
    ],
    ids=_kind,
)
async def test_what_an_endpoint_sends_is_forwarded_and_read_back(
    message: Message,
) -> None:
    lines = _lines()
    if not isinstance(message, Hello):
        lines.feed(encoded(_HELLO))
    routed, calls = lines.feed(encoded(message))
    assert routed.to_agent == b""
    assert await _received(routed.to_host) == message
    assert len(calls) == isinstance(message, Call)


@pytest.mark.asyncio
async def test_nan_as_an_endpoint_writes_it_is_read_back() -> None:
    lines = _lines()
    lines.feed(encoded(_HELLO))
    raw = encoded(_call(x=math.nan))
    assert b'"py/float": "nan"' in raw
    routed, calls = lines.feed(raw)
    message = await _received(routed.to_host)
    assert isinstance(message, Call)
    number = message.arguments["x"]
    assert isinstance(number, float)
    assert math.isnan(number)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_forwarded_call_reads_back_with_a_duplicate_escaped_tool_key() -> None:
    lines = _lines()
    lines.feed(encoded(_HELLO))
    raw = (
        b'{"py/object": "Call", "id": "c1", "agent": "a", "window": "w",'
        b' "tool": "Read", "\\u0074ool": "Bash", "tool": "Write",'
        b' "arguments": {"py/dict": {"path": "/x"}}, "address_book": ["a"], "granted": ["Bash"]}\n'
    )
    routed, calls = lines.feed(raw)
    message = await _received(routed.to_host)
    # The host sets ``granted`` over whatever the agent sent (``tool_host_test``).
    assert message == Call(
        id="c1",
        agent="a",
        window="w",
        tool="Write",
        arguments={"path": "/x"},
        address_book=("a",),
        granted=("Bash",),
    )
    assert [call.tool for call in calls] == ["Write"]


@pytest.mark.asyncio
async def test_the_size_is_over_the_arguments_as_forwarded() -> None:
    lines = _lines()
    lines.feed(encoded(_HELLO))
    routed, calls = lines.feed(
        encoded(_call(n=math.nan, d={"k": [1, 2.5]}, **{"py/object": "kept"})),
    )
    forwarded = cast(dict[str, object], json.loads(routed.to_host)["arguments"])
    shown = json.dumps(forwarded, sort_keys=True).encode()
    assert "py/dict" in forwarded
    assert calls[0].argument_bytes == len(shown)


@pytest.mark.asyncio
async def test_a_key_that_reads_like_a_tag_as_an_endpoint_writes_it_is_forwarded() -> (
    None
):
    lines = _lines()
    lines.feed(encoded(_HELLO))
    message = _call(**{"py/object": 1, "json://x": 2})
    routed, calls = lines.feed(encoded(message))
    assert len(calls) == 1
    assert await _received(routed.to_host) == message


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
