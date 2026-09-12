"""Reader-thread ownership and terminal records for pushed normalizers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import TextIO, override

import queue
import threading

import pytest

from trackinizer.lib.agent.types.sessions import UserMessage
from trackinizer.trax.run.adapters import tail
from trackinizer.types.streams import TraxRecord


def test_failed_reader_cannot_end_the_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = tail.Tail(_records)
    produced = _PausingQueue(lambda item: isinstance(item, tail._Failed))
    reader._produced = produced
    try:
        assert reader.feed("first") == [UserMessage(content="first")]
        previous = reader._reader
        assert previous is not None
        progress: queue.SimpleQueue[str] = queue.SimpleQueue()
        join_previous = _observe_join(previous, monkeypatch, progress)
        with ThreadPoolExecutor(max_workers=1) as callers:
            failed = callers.submit(reader.feed, "boom")
            failed.add_done_callback(partial(_returned, progress))
            try:
                assert produced.published.wait(2.0)
                first_transition = progress.get(timeout=2.0)
            finally:
                produced.release.set()
            with pytest.raises(ValueError, match="poison"):
                failed.result(timeout=2.0)
            join_previous(2.0)
        assert reader.feed("recovered") == [UserMessage(content="recovered")], (
            "the failed reader's terminal signal contaminated its replacement"
        )
        assert first_transition == "join", "restarted before the old reader exited"
        assert not previous.is_alive()
        assert reader.close() == [UserMessage(content="EOF")]
        assert reader.close() == []
    finally:
        produced.release.set()
        _finish_reader(reader)


@pytest.mark.parametrize("finish_in_feed", [False, True])
def test_terminal_result_waits_for_reader_exit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    finish_in_feed: bool,
) -> None:
    reader = tail.Tail(_records)
    produced = _PausingQueue(lambda item: item is tail._ENDED)
    reader._produced = produced
    try:
        assert reader.feed("first") == [UserMessage(content="first")]
        worker = reader._reader
        assert worker is not None
        progress: queue.SimpleQueue[str] = queue.SimpleQueue()
        join_worker = _observe_join(worker, monkeypatch, progress)
        with ThreadPoolExecutor(max_workers=1) as callers:
            completed = (
                callers.submit(reader.feed, "finish")
                if finish_in_feed
                else callers.submit(reader.close)
            )
            completed.add_done_callback(partial(_returned, progress))
            try:
                assert produced.published.wait(2.0)
                assert progress.get(timeout=2.0) == "join", (
                    "returned a terminal result while the reader was still alive"
                )
                assert worker.is_alive()
                assert not completed.done()
            finally:
                produced.release.set()
                join_worker(2.0)
            assert completed.result(timeout=2.0) == [UserMessage(content="EOF")]
        assert not worker.is_alive()
        assert reader.close() == []
        assert reader.feed("ignored") == []
    finally:
        produced.release.set()
        _finish_reader(reader)


def test_unstarted_close_needs_no_reader() -> None:
    reader = tail.Tail(_records)
    assert reader.close() == []
    assert reader.close() == []
    assert reader._reader is None


def test_whole_file_reading_stays_threadless() -> None:
    reader = tail.Tail(_records, whole_file=True)
    assert reader.feed("first\n") == [
        UserMessage(content="first"),
        UserMessage(content="EOF"),
    ]
    assert reader.feed("second\n") == [
        UserMessage(content="second"),
        UserMessage(content="EOF"),
    ]
    assert reader.close() == []
    assert reader._reader is None


def _records(stream: TextIO) -> Iterator[TraxRecord]:
    for line in stream:
        text = line.rstrip("\n")
        if text == "boom":
            raise ValueError("poison")
        if text == "finish":
            break
        yield UserMessage(content=text)
    yield UserMessage(content="EOF")


class _PausingQueue(queue.SimpleQueue[TraxRecord | tail._Signal]):
    """Pause after publishing a selected signal, before the reader can exit."""

    def __init__(self, pause_when: Callable[[TraxRecord | tail._Signal], bool]) -> None:
        super().__init__()
        self._pause_when = pause_when
        self.published = threading.Event()
        self.release = threading.Event()

    @override
    def put(
        self,
        item: TraxRecord | tail._Signal,
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        super().put(item, block=block, timeout=timeout)
        if self._pause_when(item):
            self.published.set()
            self.release.wait()


def _observe_join(
    worker: threading.Thread,
    monkeypatch: pytest.MonkeyPatch,
    progress: queue.SimpleQueue[str],
) -> Callable[[float | None], None]:
    original_join = worker.join

    def joining(timeout: float | None = None) -> None:
        progress.put("join")
        original_join(timeout)

    monkeypatch.setattr(worker, "join", joining)
    return original_join


def _returned(
    progress: queue.SimpleQueue[str],
    completed: Future[list[TraxRecord]],
) -> None:
    del completed
    progress.put("returned")


def _finish_reader(reader: tail.Tail) -> None:
    worker = reader._reader
    if worker is not None:
        reader._lines.put(tail._NO_MORE_LINES)
        worker.join(timeout=2.0)
        assert not worker.is_alive()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
