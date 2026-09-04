"""Streaming tests: an adapter holds neither the stream it reads nor writes.

Axiom 11 is a memory claim, and a byte test cannot fail on it -- a reader that
buffers the whole file produces exactly the same session as one that does not.
So each property is measured directly: the reader is handed lines it can weakly
watch, and the writer is run at two sizes to see whether its transient cost
tracks the output it produced.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TextIO, override
from weakref import ReferenceType, ref

import gc
import json
import tracemalloc

import pytest

from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.sessions.convert import _Adapter
from trackinizer.lib.agent.types.sessions import SessionRecord


class _TrackedLine(str):
    """A line a test can hold weakly, so an adapter's own grip is visible."""

    __slots__ = ("__weakref__",)


class _WatchedInput(TextIO):
    """Hand out one line at a time, watching how many stay alive at once.

    The count is taken WHILE the adapter reads, not after: a reader that
    materializes the file drops that list when it returns, so a check made
    afterwards passes for both designs.
    """

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)
        self._at = 0
        self._handed: list[ReferenceType[str]] = []
        self.peak_alive = 0

    @override
    def __iter__(self) -> Iterator[str]:
        return self

    @override
    def __next__(self) -> str:
        if self._at >= len(self._lines):
            raise StopIteration
        line = _TrackedLine(self._lines[self._at])
        self._at += 1
        self._handed.append(ref(line))
        self.peak_alive = max(
            self.peak_alive, sum(found() is not None for found in self._handed)
        )
        return line


class _NullSink(TextIO):
    """Discard every write, so only the writer's own retention is measured."""

    @override
    def write(self, text: str, /) -> int:
        """Accept and drop one chunk."""
        return len(text)


def _claude_lines(count: int) -> list[str]:
    """Return ``count`` distinct claude user lines, none of them degenerate."""
    return [
        json.dumps(
            {
                "parentUuid": None,
                "isSidechain": False,
                "type": "user",
                "message": {"role": "user", "content": f"line {index} " + "x" * 200},
                "uuid": f"u{index}",
                "timestamp": "2026-09-02T00:00:00.000Z",
                "userType": "external",
                "cwd": "/w",
                "sessionId": "01a03544-88de-71e2-981c-c8433de27ddc",
                "version": "2.1.241",
            },
            separators=(",", ":"),
        )
        + "\n"
        for index in range(count)
    ]


def _codex_lines(count: int) -> list[str]:
    """Return a codex launch line followed by ``count`` response items."""
    head = json.dumps(
        {
            "timestamp": "2026-08-24T19:34:39.215Z",
            "type": "session_meta",
            "payload": {
                "session_id": "01a03544-88de-71e2-981c-c8433de27ddc",
                "id": "01a03544-88de-71e2-981c-c8433de27ddc",
            },
        },
        separators=(",", ":"),
    )
    items = [
        json.dumps(
            {
                "timestamp": "2026-08-24T19:34:41.197Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"u{index}",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"line {index} " + "x" * 200}
                    ],
                },
            },
            separators=(",", ":"),
        )
        for index in range(count)
    ]
    return [f"{line}\n" for line in (head, *items)]


def _lines_for(adapter: _Adapter, count: int) -> list[str]:
    """Return ``count`` native lines in the format ``adapter`` reads."""
    return _claude_lines(count) if adapter is claude else _codex_lines(count)


def _write_cost(
    adapter: _Adapter, records: Sequence[SessionRecord], *, repeats: int = 3
) -> int:
    """Return the peak bytes ``denormalize`` allocates beyond its input.

    The MINIMUM over a few runs. ``get_traced_memory`` reports a process-wide
    high-water mark, so anything the interpreter does during the window -- a
    prior test's garbage finalizing, an import's one-time table -- adds to the
    reading and nothing subtracts from it. A single sample therefore fails at
    random under xdist (measured 34344 against a bound of 22950, where a clean
    run reports ~1100), while the floor is stable. Noise only ever inflates the
    cost, so a writer that really buffers still fails: its minimum is
    proportional to the session too.
    """
    costs: list[int] = []
    for _ in range(repeats):
        gc.collect()
        tracemalloc.start()
        try:
            base, _ = tracemalloc.get_traced_memory()
            adapter.denormalize(records, _NullSink())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        costs.append(peak - base)
    return min(costs)


@pytest.mark.parametrize(
    "adapter",
    [pytest.param(claude, id="claude"), pytest.param(codex, id="codex")],
)
def test_normalize_does_not_hold_the_stream_it_reads(adapter: _Adapter) -> None:
    # Two lines: the one being handed out, and the one the reader's own loop
    # variable still names. A reader that lists the stream holds all 64.
    stream = _WatchedInput(_lines_for(adapter, 64))

    # Drained, not merely called: ``normalize`` is a generator, so an
    # un-consumed one reads nothing at all and every count stays at zero.
    _ = list(adapter.normalize(stream))

    assert stream.peak_alive <= 2


@pytest.mark.parametrize(
    "adapter",
    [pytest.param(claude, id="claude"), pytest.param(codex, id="codex")],
)
def test_denormalize_cost_does_not_track_the_output_it_writes(
    adapter: _Adapter,
) -> None:
    # Two sizes rather than one bound: a writer that buffers pays for every
    # line it produced, so doubling the session doubles the cost, while one
    # that streams pays for a bounded window whatever the session's size.
    small = _lines_for(adapter, 200)
    large = _lines_for(adapter, 400)
    grew = sum(len(line.encode("utf-8")) for line in large) - sum(
        len(line.encode("utf-8")) for line in small
    )
    cost = _write_cost(
        adapter, list(adapter.normalize(_WatchedInput(large)))
    ) - _write_cost(adapter, list(adapter.normalize(_WatchedInput(small))))

    assert cost < grew // 4


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
