"""Tests for following a growing file."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TypeGuard
from unittest.mock import patch

import asyncio
import contextlib
import ctypes
import errno
import os
import platform
import select

import pytest

from trackinizer.lib.posix import follow
from trackinizer.lib.posix.follow import follow_dir, follow_file


@pytest.fixture
def file_watch_ready(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Signal real watch registration without changing delivered events."""
    ready = asyncio.Event()
    real_watch = follow._watch_lines

    @asynccontextmanager
    async def armed_watch(
        *directories: Path,
        match: Callable[[Path], bool],
        existing: set[Path],
    ) -> AsyncGenerator[AsyncIterator[set[Path]]]:
        async with real_watch(*directories, match=match, existing=existing) as changes:
            ready.set()
            yield changes

    monkeypatch.setattr(follow, "_watch_lines", armed_watch)
    return ready


def test_yields_appended_lines(tmp_path: Path, file_watch_ready: asyncio.Event) -> None:
    """A line appended after the follow starts is delivered."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        with target.open("a") as handle:
            _ = handle.write("one\n")
        return await task

    assert asyncio.run(run()) == ["one"]


def test_skips_what_was_already_there(
    tmp_path: Path,
    file_watch_ready: asyncio.Event,
) -> None:
    """Lines present before the follow started are not delivered."""
    target = tmp_path / "log"
    _ = target.write_text("old\n")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        with target.open("a") as handle:
            _ = handle.write("new\n")
        return await task

    assert asyncio.run(run()) == ["new"]


def test_replays_from_the_start_when_asked(tmp_path: Path) -> None:
    """``replay`` delivers what the file already holds."""
    target = tmp_path / "log"
    _ = target.write_text("old\n")

    async def run() -> list[str]:
        return await _take(target, 1, 5.0, replay=True)

    assert asyncio.run(run()) == ["old"]


def test_holds_a_partial_line(tmp_path: Path, file_watch_ready: asyncio.Event) -> None:
    """A line split across writes is delivered once, whole."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        with target.open("a") as handle:
            _ = handle.write("split")
            handle.flush()
            await asyncio.sleep(0.1)
            _ = handle.write("-line\n")
        return await task

    assert asyncio.run(run()) == ["split-line"]


def test_restarts_after_a_rewrite(
    tmp_path: Path,
    file_watch_ready: asyncio.Event,
) -> None:
    """A file that shrinks is re-read from its new start.

    A rewrite leaves the byte offset past the new end, so a follower that
    kept it would stall; one that kept its partial buffer would prepend dead
    bytes to the first new line.
    """
    target = tmp_path / "log"
    _ = target.write_text("aaa\nbbb\n")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        _ = target.write_text("fresh\n")
        return await task

    assert asyncio.run(run()) == ["fresh"]


def test_follows_a_file_created_later(
    tmp_path: Path,
    file_watch_ready: asyncio.Event,
) -> None:
    """The file need not exist when the follow starts."""
    target = tmp_path / "log"

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        _ = target.write_text("late\n")
        return await task

    assert asyncio.run(run()) == ["late"]


def test_ignores_a_sibling_file(
    tmp_path: Path,
    file_watch_ready: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling is examined but only the requested file supplies a line."""
    target = tmp_path / "log"
    sibling = tmp_path / "other"
    _ = target.write_text("")
    examined = asyncio.Event()
    real_watch = follow._watch_lines

    @asynccontextmanager
    async def observed_watch(
        *directories: Path,
        match: Callable[[Path], bool],
        existing: set[Path],
    ) -> AsyncGenerator[AsyncIterator[set[Path]]]:
        async with real_watch(
            *directories,
            match=partial(
                _matching_observed,
                match=match,
                sibling=sibling,
                examined=examined,
            ),
            existing=existing,
        ) as changes:
            yield _changes_observed(changes, sibling=sibling, examined=examined)

    monkeypatch.setattr(follow, "_watch_lines", observed_watch)

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        _ = sibling.write_text("elsewhere\n")
        await asyncio.wait_for(examined.wait(), 5.0)
        _ = target.write_text("right\n")
        return await task

    assert asyncio.run(run()) == ["right"]


def test_delivers_a_burst_in_order(
    tmp_path: Path,
    file_watch_ready: asyncio.Event,
) -> None:
    """Several lines written at once arrive in the order written."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 3, 5.0))
        await asyncio.wait_for(file_watch_ready.wait(), 5.0)
        with target.open("a") as handle:
            _ = handle.write("one\ntwo\nthree\n")
        return await task

    assert asyncio.run(run()) == ["one", "two", "three"]


def _matching_observed(
    path: Path,
    *,
    match: Callable[[Path], bool],
    sibling: Path,
    examined: asyncio.Event,
) -> bool:
    matched = match(path)
    if path == sibling:
        examined.set()
    return matched


async def _changes_observed(
    changes: AsyncIterator[set[Path]],
    *,
    sibling: Path,
    examined: asyncio.Event,
) -> AsyncIterator[set[Path]]:
    async for paths in changes:
        if sibling in paths:
            examined.set()
        yield paths


async def _take(
    path: Path,
    count: int,
    timeout_sec: float,
    *,
    replay: bool = False,
) -> list[str]:
    """Read ``count`` lines, or return what arrived before the deadline."""

    async def gather() -> list[str]:
        seen: list[str] = []
        async for line in follow_file(path, replay=replay):
            seen.append(line)
            if len(seen) >= count:
                return seen
        return seen

    try:
        return await asyncio.wait_for(gather(), timeout_sec)
    except TimeoutError:
        return []


def test_wakes_on_file_creation(tmp_path: Path) -> None:
    """A file appearing in the watched directory wakes the caller."""

    async def run() -> set[Path]:
        async with follow_dir(tmp_path) as changed:
            _ = (tmp_path / "session.jsonl").write_text("{}\n")
            return await _await_wake(changed, 5.0)

    assert asyncio.run(run()) == {tmp_path / "session.jsonl"}


def test_wakes_on_append(tmp_path: Path) -> None:
    """Appending to an existing file wakes the caller."""
    target = tmp_path / "session.jsonl"
    _ = target.write_text("{}\n")

    async def run() -> set[Path]:
        async with follow_dir(tmp_path) as changed:
            with target.open("a") as handle:
                _ = handle.write("{}\n")
            return await _await_wake(changed, 5.0)

    assert asyncio.run(run()) == {target}


def test_wakes_on_rewrite(tmp_path: Path) -> None:
    """A whole-file rewrite wakes the caller.

    Compaction replaces the transcript rather than appending, so a watcher
    that only saw growth would miss it.
    """
    target = tmp_path / "session.jsonl"
    _ = target.write_text("aaaa\n")

    async def run() -> set[Path]:
        async with follow_dir(tmp_path) as changed:
            _ = target.write_text("b\n")
            return await _await_wake(changed, 5.0)

    assert asyncio.run(run()) == {target}


def test_ignores_other_directories(tmp_path: Path) -> None:
    """A write outside the watched directory does not wake the caller."""
    watched = tmp_path / "watched"
    other = tmp_path / "other"
    watched.mkdir()
    other.mkdir()

    async def run() -> set[Path]:
        async with follow_dir(watched) as changed:
            _ = (other / "session.jsonl").write_text("{}\n")
            _ = (watched / "session.jsonl").write_text("{}\n")
            return await _await_wake(changed, 5.0)

    assert asyncio.run(run()) == {watched / "session.jsonl"}


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    """Watching a directory that does not exist fails loudly."""

    async def run() -> None:
        async with follow_dir(tmp_path / "absent") as woken:
            _ = await anext(woken)

    with pytest.raises(FileNotFoundError):
        asyncio.run(run())


def test_watch_is_armed_before_entering_the_context(tmp_path: Path) -> None:
    """A change made in the context body cannot predate the kernel watch."""

    async def run() -> set[Path]:
        target = tmp_path / "session.jsonl"
        async with follow_dir(tmp_path) as woken:
            _ = target.write_text("{}\n")
            return await asyncio.wait_for(anext(woken), 1.0)

    assert asyncio.run(run()) == {tmp_path / "session.jsonl"}


class TestSeveralDirectories:
    """One watch serving several directories at once.

    Every adapter names a LIST of directories, so a watch that takes one is
    a watch the caller has to multiply -- an fd and a task per directory,
    against a 128-instance kernel ceiling. The kernel puts many watches on
    one descriptor; the API has to expose that.
    """

    def test_wakes_for_a_write_in_either_directory(self, tmp_path: Path) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        async def run() -> list[set[Path]]:
            seen: list[set[Path]] = []
            async with follow_dir(first, second) as changed:
                _ = (first / "a.jsonl").write_text("{}\n")
                _ = (second / "b.jsonl").write_text("{}\n")
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        seen.append(await asyncio.wait_for(anext(changed), 1.0))
                    except TimeoutError:
                        break
                    if len({p for batch in seen for p in batch}) >= 2:
                        break
            return seen

        woken = {path for batch in asyncio.run(run()) for path in batch}
        assert woken == {first / "a.jsonl", second / "b.jsonl"}

    def test_paths_resolve_against_their_own_directory(self, tmp_path: Path) -> None:
        """A same-named file in two watched dirs must not collapse into one.

        Each event carries the watch descriptor it fired on, not a path; a
        reader that rebuilt paths against a single remembered directory would
        report both writes as the same file.
        """
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        async def run() -> set[Path]:
            woken: set[Path] = set()
            async with follow_dir(first, second) as changed:
                _ = (first / "session.jsonl").write_text("{}\n")
                _ = (second / "session.jsonl").write_text("{}\n")
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        woken |= await asyncio.wait_for(anext(changed), 1.0)
                    except TimeoutError:
                        break
                    if len(woken) >= 2:
                        break
            return woken

        assert asyncio.run(run()) == {
            first / "session.jsonl",
            second / "session.jsonl",
        }

    def test_a_single_directory_still_works(self, tmp_path: Path) -> None:
        """The one-directory call is the same call, not a special case."""

        async def run() -> set[Path]:
            async with follow_dir(tmp_path) as changed:
                _ = (tmp_path / "only.jsonl").write_text("{}\n")
                return await asyncio.wait_for(anext(changed), 5.0)

        assert asyncio.run(run()) == {tmp_path / "only.jsonl"}

    def test_no_directories_is_an_error(self) -> None:
        """Watching nothing is a caller bug, not an iterator that never wakes."""

        async def run() -> None:
            async with follow_dir() as changed:
                _ = await anext(changed)

        with pytest.raises(ValueError, match="directory"):
            asyncio.run(run())

    def test_one_missing_directory_fails_the_whole_watch(self, tmp_path: Path) -> None:
        """A watch that silently covers fewer dirs than asked captures nothing.

        Partial success is the dangerous shape: the caller believes every
        directory is covered and never learns which one is not.
        """
        present = tmp_path / "present"
        present.mkdir()

        async def run() -> None:
            async with follow_dir(present, tmp_path / "absent") as changed:
                _ = await anext(changed)

        with pytest.raises(FileNotFoundError):
            asyncio.run(run())


class TestSubdirectories:
    """Watches are not recursive; a new subdirectory needs its own watch.

    Codex files rollouts under ``sessions/<Y>/<M>/<D>/`` and the day
    directory does not exist before the first run of the day, so a watch on
    the root alone sees nothing a session ever writes.
    """

    def test_wakes_for_a_write_in_an_existing_subdirectory(
        self,
        tmp_path: Path,
    ) -> None:
        leaf = tmp_path / "2026" / "08" / "25"
        leaf.mkdir(parents=True)

        async def run() -> set[Path]:
            async with follow_dir(tmp_path) as changed:
                _ = (leaf / "rollout.jsonl").write_text("{}\n")
                return await asyncio.wait_for(anext(changed), 5.0)

        assert asyncio.run(run()) == {leaf / "rollout.jsonl"}

    def test_wakes_for_a_subdirectory_created_after_the_watch(
        self,
        tmp_path: Path,
    ) -> None:
        """A directory born mid-run must get its own watch as it appears."""

        async def run() -> set[Path]:
            woken: set[Path] = set()
            async with follow_dir(tmp_path) as changed:
                leaf = tmp_path / "born-later"
                leaf.mkdir()
                await asyncio.sleep(0.2)
                _ = (leaf / "rollout.jsonl").write_text("{}\n")
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        woken |= await asyncio.wait_for(anext(changed), 1.0)
                    except TimeoutError:
                        break
                    if tmp_path / "born-later" / "rollout.jsonl" in woken:
                        break
            return woken

        assert tmp_path / "born-later" / "rollout.jsonl" in asyncio.run(run())

    def test_a_file_written_before_its_watch_is_still_reported(
        self,
        tmp_path: Path,
    ) -> None:
        r"""The documented inotify race: fill a new directory instantly.

        ``inotify(7)``: "by the time you create a watch for the new
        subdirectory, new files may already have been created in the
        subdirectory. Therefore, you may want to scan the contents of the
        subdirectory immediately after adding the watch."

        Without that scan the file is invisible forever -- no later event ever
        names it, because the write already happened.
        """

        async def run() -> set[Path]:
            woken: set[Path] = set()
            async with follow_dir(tmp_path) as changed:
                leaf = tmp_path / "day"
                leaf.mkdir()
                # No pause: the write races the walker's watch registration.
                _ = (leaf / "rollout-early.jsonl").write_text("{}\n")
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        woken |= await asyncio.wait_for(anext(changed), 1.0)
                    except TimeoutError:
                        break
                    if tmp_path / "day" / "rollout-early.jsonl" in woken:
                        break
            return woken

        assert tmp_path / "day" / "rollout-early.jsonl" in asyncio.run(run())

    def test_a_nested_subdirectory_chain_is_walked(self, tmp_path: Path) -> None:
        """``mkdir -p`` of a whole chain must leave every level watched."""

        async def run() -> set[Path]:
            woken: set[Path] = set()
            async with follow_dir(tmp_path) as changed:
                leaf = tmp_path / "2026" / "08" / "25"
                leaf.mkdir(parents=True)
                _ = (leaf / "rollout.jsonl").write_text("{}\n")
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        woken |= await asyncio.wait_for(anext(changed), 1.0)
                    except TimeoutError:
                        break
                    if leaf / "rollout.jsonl" in woken:
                        break
            return woken

        expected = tmp_path / "2026" / "08" / "25" / "rollout.jsonl"
        assert expected in asyncio.run(run())


class TestCursor:
    """Byte-offset bookkeeping, independent of which kernel wakes it."""

    def test_a_same_size_replacement_is_re_read_whole(self, tmp_path: Path) -> None:
        """A rewrite is not always a SHRINK; identity is content, not length.

        ``test_restarts_after_a_rewrite`` only covers a file getting smaller,
        so a cursor that resets on ``size < offset`` passes it while seeking
        straight into the middle of a same-or-larger replacement -- yielding
        the tail of a line nobody wrote.
        """
        target = tmp_path / "log"
        _ = target.write_text("old\n")
        cursor = follow._Cursor(target, offset=4)

        _ = target.write_text("fresh\n")

        assert cursor.drain() == ["fresh"]

    def test_a_growing_file_is_not_re_read(self, tmp_path: Path) -> None:
        """Growth past the cursor is new content, never a replacement.

        The counterpart to the test above: a guard that re-read whenever the
        size changed would replay the whole file on every append.
        """
        target = tmp_path / "log"
        _ = target.write_text("one\n")
        cursor = follow._Cursor(target, offset=0)
        assert cursor.drain() == ["one"]

        with target.open("a") as handle:
            _ = handle.write("two\n")

        assert cursor.drain() == ["two"]

    def test_every_line_is_delivered_exactly_once(self, tmp_path: Path) -> None:
        """A line read is a line recorded, however the file grew meanwhile.

        The cursor must advance by what it READ, never by a length measured
        before reading: a write landing between those two syscalls is read now
        and would be delivered again on the next drain.
        """
        target = tmp_path / "log"
        _ = target.write_bytes(b"a\n")
        cursor = follow._Cursor(target, offset=0)
        seen = cursor.drain()

        with target.open("ab") as handle:
            _ = handle.write(b"b\n")
        seen += cursor.drain()
        seen += cursor.drain()

        assert seen == ["a", "b"]

    def test_a_character_split_across_writes_survives(self, tmp_path: Path) -> None:
        """A multibyte character split across appends is not two mojibake.

        ``drain`` decodes each chunk on its own, so a UTF-8 sequence straddling
        two reads is decoded as two invalid halves and ``errors="replace"``
        turns each into U+FFFD -- silently corrupting the line rather than
        holding the incomplete bytes for the next read.
        """
        target = tmp_path / "log"
        # ``café`` cut mid-sequence: the leading byte of its final character
        # is not decodable alone, and the byte completing it arrives next.
        whole = "café".encode()
        _ = target.write_bytes(whole[:-1])
        cursor = follow._Cursor(target, offset=0)
        assert cursor.drain() == []

        with target.open("ab") as handle:
            _ = handle.write(whole[-1:] + b"\n")

        assert cursor.drain() == ["café"]

    def test_unreadable_file_yields_nothing(self, tmp_path: Path) -> None:
        """A file that cannot be opened is skipped, not raised through.

        The follow loop runs for the life of a session; a transient
        permission or race error must cost one drain, not the whole follow.
        """
        target = tmp_path / "locked"
        _ = target.write_text("visible\n")
        cursor = follow._Cursor(target, offset=0)
        with patch.object(
            Path,
            "open",
            side_effect=PermissionError(errno.EACCES, "permission denied"),
        ) as open_file:
            assert cursor.drain() == []
        open_file.assert_called_once_with("rb")
        assert cursor.drain() == ["visible"]
        assert cursor.drain() == []

    @pytest.mark.parametrize("code", [errno.EMFILE, errno.ENFILE])
    def test_read_descriptor_exhaustion_is_reported(
        self,
        tmp_path: Path,
        code: int,
    ) -> None:
        target = tmp_path / "log"
        target.write_text("history\n")
        cursor = follow._Cursor(target, offset=0)
        with (
            patch.object(Path, "open", side_effect=OSError(code, "descriptor limit")),
            pytest.raises(OSError, match="descriptor limit") as caught,
        ):
            cursor.drain()
        assert caught.value.errno == code

    def test_missing_file_measures_zero(self, tmp_path: Path) -> None:
        assert follow._size(tmp_path / "absent") == 0


def _recording_warning(into: list[str]) -> Callable[..., None]:
    """Return a ``logger.warning`` stand-in that captures the formatted message."""

    def warning(msg: str, *args: object, **kwargs: object) -> None:
        del kwargs  # ``exc_info`` and friends; only the text is asserted on.
        into.append(msg % args)

    return warning


class TestWatchFailures:
    """What the watch does when the kernel refuses, not when it obliges.

    Each of these silently disables capture: the caller keeps awaiting an
    iterator that will never name the file it is waiting for.
    """

    def test_a_descendant_vanishing_mid_walk_does_not_kill_the_watch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A temp directory deleted between ``rglob`` and its watch is routine.

        The walk lists the tree and then registers each entry, so anything
        short-lived under it is gone by the time its turn comes. Letting that
        ENOENT propagate closes the descriptor and takes the ROOT watch with
        it -- the run then captures nothing at all, over a directory it never
        needed.
        """
        doomed = tmp_path / "doomed"
        doomed.mkdir()
        real_add = follow._add_watch

        def vanishing(
            libc: ctypes.CDLL,
            fd: int,
            directory: Path,
            watches: dict[int, Path],
        ) -> None:
            if directory == doomed:
                raise FileNotFoundError(errno.ENOENT, "no such directory", str(doomed))
            real_add(libc, fd, directory, watches)

        monkeypatch.setattr(follow, "_add_watch", vanishing)

        async def run() -> set[Path]:
            async with follow.follow_dir(tmp_path) as changed:
                _ = (tmp_path / "session.jsonl").write_text("{}\n")
                return await asyncio.wait_for(anext(changed), 5.0)

        assert asyncio.run(run()) == {tmp_path / "session.jsonl"}

    def test_a_missing_root_is_still_an_error(self, tmp_path: Path) -> None:
        """Tolerating a vanished DESCENDANT must not tolerate a missing ROOT.

        The root is what the caller asked for; silently covering nothing is
        the failure mode the whole watch exists to rule out.
        """

        async def run() -> None:
            async with follow.follow_dir(tmp_path / "absent") as changed:
                _ = await anext(changed)

        with pytest.raises(FileNotFoundError):
            asyncio.run(run())

    def test_a_refused_adoption_is_reported_not_swallowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hitting the watch limit is not "the directory vanished".

        ``_adopt`` treated every OSError as a race, so exhausting
        ``max_user_watches`` left the new subtree permanently unwatched with
        nothing said -- the one failure an operator can actually act on.
        """
        warned: list[str] = []
        directory = tmp_path / "born-later"
        directory.mkdir()
        libc = ctypes.CDLL(None)
        watches: dict[int, Path] = {}
        monkeypatch.setattr(follow._logger, "warning", _recording_warning(warned))

        with patch.object(
            follow,
            "_watch_tree",
            side_effect=OSError(errno.ENOSPC, "watch limit reached", str(directory)),
        ) as watch_tree:
            assert follow._adopt(libc, -1, directory, watches) == set()

        watch_tree.assert_called_once_with(libc, -1, directory, watches)
        assert warned == [f"inotify refused a watch on {directory}"]

    @pytest.mark.skipif(
        platform.system() != "Linux",
        reason="IN_Q_OVERFLOW is an inotify event",
    )
    def test_a_queue_overflow_rescans_rather_than_losing_the_writes(
        self,
        tmp_path: Path,
    ) -> None:
        """``IN_Q_OVERFLOW`` means events were DROPPED, not that none happened.

        The kernel queues a bounded number of events; past that it discards
        them and reports one overflow instead. It arrives with ``wd = -1``,
        which names no watch, so the reader skipped it like any unknown
        descriptor -- and every write it stood for was lost for good. The only
        recovery is to re-list the watched tree, which is what a caller cannot
        do for itself: it never learns anything was dropped.
        """
        existing = tmp_path / "already-there.jsonl"
        _ = existing.write_text("{}\n")
        watches = {1: tmp_path}
        overflow = follow._EVENT_HEADER.pack(-1, follow._IN_Q_OVERFLOW, 0, 0)

        changed = follow._read_events(overflow, follow._libc(), -1, watches)

        assert existing in changed, "an overflow reported nothing was dropped"

    def test_a_vanished_adoption_is_not_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The counterpart: a directory that really did vanish is routine.

        Warning on it would make the log useless for the case above.
        """
        warned: list[str] = []
        directory = tmp_path / "born-later"
        libc = ctypes.CDLL(None)
        watches: dict[int, Path] = {}
        monkeypatch.setattr(follow._logger, "warning", _recording_warning(warned))

        with patch.object(
            follow,
            "_watch_tree",
            side_effect=FileNotFoundError(errno.ENOENT, "gone", str(directory)),
        ) as watch_tree:
            assert follow._adopt(libc, -1, directory, watches) == set()

        watch_tree.assert_called_once_with(libc, -1, directory, watches)
        assert warned == []


class TestFollowTree:
    """Following every matching file under a set of directories.

    A caller draining a CLI's session logs does not know which file it wants
    -- the CLI mints the name at startup, sometimes in a directory that does
    not exist yet. It knows only the roots and a predicate.
    """

    def test_yields_lines_from_a_file_created_later(self, tmp_path: Path) -> None:
        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.wait_for(armed.wait(), 5.0)
            _ = (tmp_path / "session.jsonl").write_text('{"n":1}\n')
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "session.jsonl", '{"n":1}')]

    def test_ignores_a_file_the_predicate_rejects(self, tmp_path: Path) -> None:
        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.wait_for(armed.wait(), 5.0)
            _ = (tmp_path / "notes.txt").write_text("ignored\n")
            _ = (tmp_path / "session.jsonl").write_text('{"n":1}\n')
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "session.jsonl", '{"n":1}')]

    def test_follows_several_files_at_once(self, tmp_path: Path) -> None:
        """Two live sessions in one tree keep separate byte cursors."""

        async def run() -> set[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect(lines, seen, 4))
            await asyncio.wait_for(armed.wait(), 5.0)
            first = tmp_path / "a.jsonl"
            second = tmp_path / "b.jsonl"
            _ = first.write_text("a1\n")
            _ = second.write_text("b1\n")
            await asyncio.sleep(0.2)
            with first.open("a") as handle:
                _ = handle.write("a2\n")
            with second.open("a") as handle:
                _ = handle.write("b2\n")
            await asyncio.wait_for(task, 5.0)
            return set(seen)

        assert asyncio.run(run()) == {
            (tmp_path / "a.jsonl", "a1"),
            (tmp_path / "a.jsonl", "a2"),
            (tmp_path / "b.jsonl", "b1"),
            (tmp_path / "b.jsonl", "b2"),
        }

    def test_follows_a_file_in_a_subdirectory_created_later(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex's shape: the day directory is born during the run."""

        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.wait_for(armed.wait(), 5.0)
            leaf = tmp_path / "2026" / "08" / "25"
            leaf.mkdir(parents=True)
            _ = (leaf / "rollout.jsonl").write_text('{"n":1}\n')
            await asyncio.wait_for(task, 5.0)
            return seen

        expected = tmp_path / "2026" / "08" / "25" / "rollout.jsonl"
        assert asyncio.run(run()) == [(expected, '{"n":1}')]

    def test_a_rewrite_is_announced_as_a_restart(self, tmp_path: Path) -> None:
        """A replaced file says so, so the reader can re-derive it.

        ``drain`` already detects the replacement -- it must, or it would read
        from a stale offset -- but reporting only ``(path, line)`` made that
        knowledge die inside the cursor. A consumer that stores records keyed
        by their position CANNOT recover it afterwards: the re-read lines look
        exactly like appended ones.

        This is claude's compaction: the transcript is rewritten smaller,
        keeping the turns it did not summarize away.
        """
        target = tmp_path / "s.jsonl"

        async def run() -> list[follow.Line]:
            seen: list[follow.Line] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect_lines(lines, seen, 2))
            await asyncio.wait_for(armed.wait(), 5.0)
            _ = target.write_text("first\n")
            await asyncio.sleep(0.2)
            _ = target.write_text("rewritten\n")
            await asyncio.wait_for(task, 5.0)
            return seen

        got = asyncio.run(run())
        assert [line.text for line in got] == ["first", "rewritten"]
        assert [line.restart for line in got] == [False, True]

    def test_a_restart_survives_a_drain_that_finds_no_lines(
        self,
        tmp_path: Path,
    ) -> None:
        """A rewrite seen mid-truncate must still reach the line it replaced.

        Replacing a file is not atomic -- ``write_text`` truncates, then
        writes -- so a drain can land in that window. It sees the file no
        longer holds the bytes it read (a restart) but has NO lines to carry
        the flag on, and the next drain returns the replacement's lines. A
        flag cleared per-drain is therefore lost exactly when the file was
        rewritten, and the re-read records append as duplicates rather than
        overwriting the rows they already occupy.

        Driven through ``_Cursor`` rather than the watcher: the window is
        microseconds wide under a real writer, and reproducing it by timing
        would be the flake this fixes rather than a test of it.
        """
        target = tmp_path / "s.jsonl"
        _ = target.write_text("first\n")
        cursor = follow._Cursor(target, offset=0)
        assert cursor.drain() == ["first"]
        assert not cursor.restarted

        _ = target.write_text("")  # The truncate half of a replacement.
        assert cursor.drain() == []

        _ = target.write_text("rewritten\n")  # The write half.
        assert cursor.drain() == ["rewritten"]
        assert cursor.restarted, "the restart was lost before any line carried it"

    def test_a_restart_clears_once_its_lines_are_delivered(
        self,
        tmp_path: Path,
    ) -> None:
        """Sticky until delivered, not sticky forever.

        A flag that never cleared would mark every later append as a rewrite,
        making the consumer re-derive the whole part on each batch.
        """
        target = tmp_path / "s.jsonl"
        _ = target.write_text("first\n")
        cursor = follow._Cursor(target, offset=0)
        _ = cursor.drain()
        _ = target.write_text("rewritten\n")
        assert cursor.drain() == ["rewritten"]
        assert cursor.restarted

        with target.open("a") as handle:
            _ = handle.write("appended\n")
        assert cursor.drain() == ["appended"]
        assert not cursor.restarted

    def test_an_append_is_not_a_restart(self, tmp_path: Path) -> None:
        """Ordinary growth carries no restart, or every batch would rewrite."""
        target = tmp_path / "s.jsonl"

        async def run() -> list[follow.Line]:
            seen: list[follow.Line] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect_lines(lines, seen, 2))
            await asyncio.wait_for(armed.wait(), 5.0)
            _ = target.write_text("one\n")
            await asyncio.sleep(0.2)
            with target.open("a") as handle:
                _ = handle.write("two\n")
            await asyncio.wait_for(task, 5.0)
            return seen

        assert [line.restart for line in asyncio.run(run())] == [False, False]

    def test_skips_files_present_before_the_follow(self, tmp_path: Path) -> None:
        """A prior session's transcript is not this run's to capture."""
        _ = (tmp_path / "old.jsonl").write_text("stale\n")

        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.wait_for(armed.wait(), 5.0)
            _ = (tmp_path / "new.jsonl").write_text("fresh\n")
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "new.jsonl", "fresh")]

    def test_holds_a_partial_line_per_file(self, tmp_path: Path) -> None:
        """A line split across writes is delivered once, whole."""

        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            armed = asyncio.Event()
            lines = follow.follow_tree(
                tmp_path,
                match=lambda p: p.suffix == ".jsonl",
                on_armed=armed.set,
            )
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.wait_for(armed.wait(), 5.0)
            target = tmp_path / "s.jsonl"
            with target.open("a") as handle:
                _ = handle.write("split")
                handle.flush()
                await asyncio.sleep(0.2)
                _ = handle.write("-line\n")
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "s.jsonl", "split-line")]


async def _collect(
    lines: AsyncIterator[follow.Line],
    into: list[tuple[Path, str]],
    count: int,
) -> None:
    """Accumulate ``count`` ``(path, text)`` pairs from ``lines``."""
    async for item in lines:
        into.append((item.path, item.text))
        if len(into) >= count:
            return


async def _collect_lines(
    lines: AsyncIterator[follow.Line],
    into: list[follow.Line],
    count: int,
) -> None:
    """Accumulate ``count`` whole lines, restart flag included."""
    async for item in lines:
        into.append(item)
        if len(into) >= count:
            return


class TestPlatformDispatch:
    """Each platform reaches its own backend.

    The HOST's own backend is never faked -- every other test in this file
    drives it for real. What is forced here is the OTHER platform's dispatch,
    so a Linux developer still exercises the macOS branch and a macOS
    developer still exercises the Linux one. Faking the native branch would
    replace a real backend with a stub and assert nothing.
    """

    @pytest.mark.skipif(
        platform.system() == "Darwin",
        reason="native here; the real backend runs",
    )
    def test_darwin_uses_the_fsevents_backend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Off macOS, the Darwin branch still reaches FSEvents."""
        started: list[tuple[Path, ...]] = []
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(follow, "_watch_fsevents", _recording_watch(started))

        asyncio.run(_open_and_close(tmp_path))
        assert started == [(tmp_path,)]

    @pytest.mark.skipif(
        platform.system() == "Linux",
        reason="native here; the real backend runs",
    )
    def test_linux_uses_the_inotify_backend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Off Linux, the Linux branch still reaches inotify."""
        opened: list[tuple[Path, ...]] = []

        def fake_fd(*directories: Path) -> tuple[int, dict[int, Path]]:
            opened.append(directories)
            return (-1, {})

        def closed(fd: int) -> None:
            del fd

        def events(fd: int, watches: dict[int, Path]) -> AsyncIterator[set[Path]]:
            del fd, watches
            return _no_events()

        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(follow, "_inotify_fd", fake_fd)
        monkeypatch.setattr(os, "close", closed)
        monkeypatch.setattr(follow, "_inotify_events", events)

        asyncio.run(_open_and_close(tmp_path))
        assert opened == [(tmp_path,)]

    def test_the_host_reaches_a_real_backend(self, tmp_path: Path) -> None:
        """Unmocked, on whatever this is: a watch opens and reports a write.

        The native counterpart to the two forced tests above -- it is what
        makes them meaningful, since a dispatch that reached a working backend
        nowhere would still pass those.
        """

        async def run() -> set[Path]:
            async with follow.follow_dir(tmp_path) as changed:
                _ = (tmp_path / "native.jsonl").write_text("{}\n")
                return await asyncio.wait_for(anext(changed), 5.0)

        assert asyncio.run(run()) == {tmp_path / "native.jsonl"}

    def test_a_platform_with_neither_backend_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Somewhere with no supported mechanism says so, not degrades."""
        monkeypatch.setattr(platform, "system", lambda: "SunOS")

        async def run() -> None:
            async with follow.follow_dir(tmp_path) as changed:
                _ = await anext(changed)

        with pytest.raises(NotImplementedError, match="SunOS"):
            asyncio.run(run())


class TestFsEventsAdapter:
    """Translating watchdog's events into this module's changed-path sets.

    Driven by a stub observer on every host: watchdog's FSEvents backend needs
    a macOS kernel extension, and what can be wrong here is the translation,
    not Apple's stream. A macOS run exercises the real stream through every
    other test in this file.
    """

    def test_reports_the_changed_path(self) -> None:
        emitted = _run_fsevents(lambda o: o.fire(Path("/watched/session.jsonl")))
        assert emitted == [{Path("/watched/session.jsonl")}]

    def test_reports_both_ends_of_a_rename(self) -> None:
        """A compaction lands as a rename INTO the directory.

        A watcher that only reported the source would miss the transcript that
        replaced it.
        """
        emitted = _run_fsevents(
            lambda o: o.fire(Path("/watched/tmp"), dest=Path("/watched/session.jsonl")),
        )
        assert emitted == [{Path("/watched/tmp"), Path("/watched/session.jsonl")}]

    def test_ignores_a_directory_event(self) -> None:
        """A directory is not a file to follow; only its contents are."""
        emitted = _run_fsevents(
            lambda o: (
                o.fire(Path("/watched/subdir"), is_directory=True),
                o.fire(Path("/watched/session.jsonl")),
            ),
        )
        assert emitted == [{Path("/watched/session.jsonl")}]

    def test_schedules_every_directory_recursively(self) -> None:
        """One stream per tree is the whole reason for choosing FSEvents."""
        observer = _StubObserver()

        async def run() -> None:
            async with follow._fsevents_events(
                observer,
                (Path("/first"), Path("/second")),
            ):
                pass

        asyncio.run(run())
        assert observer.scheduled == [Path("/first"), Path("/second")]
        assert observer.started
        assert observer.stopped, "the observer thread outlived the watch"


def _recording_watch(
    into: list[tuple[Path, ...]],
) -> Callable[..., AbstractAsyncContextManager[AsyncIterator[set[Path]]]]:
    """Return a ``follow_dir`` stub that records its directories, yielding nothing."""

    @asynccontextmanager
    async def watch(*directories: Path) -> AsyncGenerator[AsyncIterator[set[Path]]]:
        into.append(directories)
        yield _no_events()

    return watch


async def _no_events() -> AsyncIterator[set[Path]]:
    """Return an iterator that ends immediately, standing in for a quiet watch."""
    for never in ():
        yield never


async def _open_and_close(directory: Path) -> None:
    """Enter and leave a watch, tolerating one that reports nothing."""
    async with follow.follow_dir(directory) as changed:
        with contextlib.suppress(StopAsyncIteration):
            _ = await anext(changed)


class _StubEvent:
    """The three attributes the adapter reads off a watchdog event."""

    def __init__(
        self,
        *,
        src_path: str,
        dest_path: str = "",
        is_directory: bool = False,
    ) -> None:
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


class _StubObserver:
    """Stands in for a watchdog observer: records the handler, replays events."""

    def __init__(self) -> None:
        self._handlers: list[object] = []
        self.scheduled: list[Path] = []
        self.started = False
        self.stopped = False

    def schedule(self, handler: object, path: str, *, recursive: bool) -> None:
        assert recursive, "a non-recursive schedule defeats the point of FSEvents"
        self._handlers.append(handler)
        self.scheduled.append(Path(path))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        del timeout

    def fire(
        self,
        path: Path,
        *,
        dest: Path | None = None,
        is_directory: bool = False,
    ) -> None:
        """Deliver one event, as watchdog would."""
        event = _StubEvent(
            src_path=str(path),
            dest_path=str(dest) if dest is not None else "",
            is_directory=is_directory,
        )
        for handler in self._handlers:
            assert isinstance(handler, follow._FsEventsHandler)
            handler.on_any_event(event)


def _run_fsevents(fire: Callable[[_StubObserver], object]) -> list[set[Path]]:
    """Drive the FSEvents adapter with ``fire``; return what it emitted."""
    emitted: list[set[Path]] = []

    async def run() -> None:
        observer = _StubObserver()
        async with follow._fsevents_events(observer, (Path("/watched"),)) as changed:
            collector = asyncio.create_task(_gather(changed, emitted, 1))
            _ = fire(observer)
            await asyncio.wait_for(collector, 5.0)

    asyncio.run(run())
    return emitted


async def _gather(
    changed: AsyncIterator[set[Path]],
    into: list[set[Path]],
    count: int,
) -> None:
    """Collect ``count`` wakes from ``changed``."""
    async for paths in changed:
        into.append(paths)
        if len(into) >= count:
            return


async def _await_wake(woken: AsyncIterator[set[Path]], timeout_sec: float) -> set[Path]:
    """Collect one wake from an armed watch, or an empty set on timeout."""
    try:
        return await asyncio.wait_for(anext(woken), timeout_sec)
    except TimeoutError:
        return set()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_tree_delivers_before_writer_closes(
    tmp_path: Path,
    *,
    existing: bool,
    nested: bool,
) -> None:
    """Successive appends arrive while the same writer remains open."""
    target = tmp_path / "nested" / "log" if nested else tmp_path / "log"
    if existing:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("history\n")
    asyncio.run(_live_tree(tmp_path, target))


def _is_async_generator[T](
    value: AsyncIterator[T],
) -> TypeGuard[AsyncGenerator[T, None]]:
    return isinstance(value, AsyncGenerator)


async def _live_tree(root: Path, target: Path) -> None:
    armed = asyncio.Event()
    lines = follow.follow_tree(root, match=lambda p: p == target, on_armed=armed.set)
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        first = asyncio.create_task(anext(lines))
        try:
            await asyncio.wait_for(armed.wait(), 5)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a") as writer:
                for text in ("one", "two", "three"):
                    writer.write(text + "\n")
                    writer.flush()
                    line = await asyncio.wait_for(
                        first if text == "one" else anext(lines),
                        2,
                    )
                    assert line.text == text
                    assert not line.restart
                pending = asyncio.create_task(anext(lines))
                try:
                    writer.write("partial")
                    writer.flush()
                    await asyncio.sleep(0.02)
                    assert not pending.done()
                    writer.write("-line\n")
                    writer.flush()
                    assert (await asyncio.wait_for(pending, 2)).text == "partial-line"
                finally:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending
        finally:
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first


def test_tree_follows_open_replacement(tmp_path: Path) -> None:
    """A replacement inode remains watched while its writer stays open."""
    asyncio.run(_live_replacement(tmp_path))


async def _live_replacement(root: Path) -> None:
    target = root / "log"
    lines = follow.follow_tree(root, match=lambda p: p == target, replay=True)
    assert _is_async_generator(lines)
    target.write_text("old\n")
    async with contextlib.aclosing(lines):
        assert (await asyncio.wait_for(anext(lines), 2)).text == "old"
        replacement = root / "replacement"
        with replacement.open("a") as writer:
            writer.write("new\n")
            writer.flush()
            replacement.replace(target)
            line = await asyncio.wait_for(anext(lines), 2)
            assert line.text == "new"
            assert line.restart
            writer.write("later\n")
            writer.flush()
            line = await asyncio.wait_for(anext(lines), 2)
            assert line.text == "later"
            assert not line.restart


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS vnode descriptors")
def test_tree_cancellation_closes_descriptors(tmp_path: Path) -> None:
    """Repeated cancellation leaves no vnode or observer descriptors open."""
    before = len(list(Path("/dev/fd").iterdir()))
    asyncio.run(_cancel_live_tree(tmp_path))
    assert len(list(Path("/dev/fd").iterdir())) == before


async def _cancel_live_tree(root: Path) -> None:
    target = root / "log"
    target.write_text("seed\n")
    for _ in range(3):
        lines = follow.follow_tree(root, match=lambda p: p == target, replay=True)
        assert _is_async_generator(lines)
        async with contextlib.aclosing(lines):
            assert (await asyncio.wait_for(anext(lines), 2)).text == "seed"
            pending = asyncio.create_task(anext(lines))
            await asyncio.sleep(0)
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS vnode descriptors")
@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENFILE])
def test_tree_exhaustion_closes_partial_setup(tmp_path: Path, code: int) -> None:
    """Failed registration propagates and releases already-owned descriptors."""
    paths = [tmp_path / str(index) for index in range(3)]
    for path in paths:
        path.write_text("history\n")
    before = len(list(Path("/dev/fd").iterdir()))
    descriptors = [os.open(path, os.O_RDONLY) for path in paths[:2]]
    try:
        with (
            patch("os.open", side_effect=[*descriptors, OSError(code, "watch limit")]),
            pytest.raises(OSError, match="watch limit") as caught,
        ):
            asyncio.run(_one_tree_line_matching_all(tmp_path))
        assert caught.value.errno == code
        assert len(list(Path("/dev/fd").iterdir())) == before
        for fd in descriptors:
            with pytest.raises(OSError, match="Bad file descriptor") as closed:
                os.fstat(fd)
            assert closed.value.errno == errno.EBADF
    finally:
        for fd in descriptors:
            with contextlib.suppress(OSError):
                os.close(fd)


async def _one_tree_line_matching_all(root: Path) -> follow.Line:
    lines = follow.follow_tree(root, match=lambda path: path.name.isdecimal())
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        return await anext(lines)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS vnode descriptors")
@pytest.mark.parametrize("resume_excluded", [False, True])
def test_tree_opens_only_selected_files(
    tmp_path: Path,
    *,
    resume_excluded: bool,
) -> None:
    """Rejected history consumes no vnode descriptors, even when in resume."""
    selected = tmp_path / "selected"
    excluded = tmp_path / "excluded"
    selected.write_text("selected\n")
    excluded.write_text("excluded\n")
    opened: list[Path] = []
    real_open = os.open

    def recording_open(path: Path, flags: int) -> int:
        opened.append(path)
        return real_open(path, flags)

    with patch("os.open", side_effect=recording_open):
        asyncio.run(_selected_history(tmp_path, selected, excluded, resume_excluded))
    assert opened == [selected]


async def _selected_history(
    root: Path,
    selected: Path,
    excluded: Path,
    resume_excluded: bool,
) -> None:
    lines = follow.follow_tree(
        root,
        match=lambda path: path == selected,
        replay=True,
        resume=frozenset({excluded}) if resume_excluded else frozenset(),
    )
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        assert (await asyncio.wait_for(anext(lines), 2)).text == "selected"


@pytest.mark.parametrize("lines", [False, True])
def test_case_insensitive_root_preserves_spelling(
    tmp_path: Path,
    *,
    lines: bool,
) -> None:
    physical = tmp_path / "MixedCase"
    physical.mkdir()
    alias = tmp_path / "mixedcase"
    if not alias.is_dir():
        pytest.skip("requires a case-insensitive filesystem")
    if lines:
        asyncio.run(_live_tree(alias, alias / "log"))
    else:
        assert asyncio.run(_write_under_watch(alias)) == {alias / "log"}


def test_directory_events_preserve_symlinked_root(tmp_path: Path) -> None:
    """A changed path retains the watched root's spelling."""
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    assert asyncio.run(_write_under_watch(alias)) == {alias / "log"}


@pytest.mark.parametrize("existing", [False, True])
def test_tree_follows_symlinked_root(tmp_path: Path, *, existing: bool) -> None:
    """Both discovered and existing files keep the caller's path spelling."""
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    target = alias / "log"
    if existing:
        target.write_text("history\n")
    asyncio.run(_live_tree(alias, target))


async def _write_under_watch(root: Path) -> set[Path]:
    async with follow_dir(root) as changes:
        (root / "log").write_text("line\n")
        return await asyncio.wait_for(anext(changes), 2)


@pytest.mark.parametrize("outside_exists", [False, True])
def test_fsevents_keeps_in_root_rename_endpoint(
    tmp_path: Path,
    *,
    outside_exists: bool,
) -> None:
    root = tmp_path / "watched"
    root.mkdir()
    outside = tmp_path / "outside"
    if outside_exists:
        outside.mkdir()
    assert asyncio.run(_moved_into_root(root, outside=outside)) == {root / "new"}


async def _moved_into_root(root: Path, *, outside: Path) -> set[Path]:
    observer = _StubObserver()
    async with follow._fsevents_events(observer, (root,)) as changes:
        observer.fire(outside / "old", dest=root / "new")
        return await asyncio.wait_for(anext(changes), 2)


def test_fsevents_maps_overlapping_roots(tmp_path: Path) -> None:
    """Each scheduled root preserves its spelling, including rename endpoints."""
    physical = tmp_path / "physical"
    physical.mkdir()
    nested = physical / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)
    assert asyncio.run(_renamed_under_roots(physical, alias)) == {
        physical / "nested" / "old",
        physical / "nested" / "new",
        alias / "old",
        alias / "new",
    }


async def _renamed_under_roots(root: Path, alias: Path) -> set[Path]:
    observer = _StubObserver()
    async with follow._fsevents_events(observer, (root, alias)) as changes:
        observer.fire(root / "nested" / "old", dest=root / "nested" / "new")
        return await asyncio.wait_for(anext(changes), 2)


def test_tree_drains_an_append_during_arming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An append before registration needs no later event to become visible."""
    (tmp_path / "log").write_text("history\n")
    monkeypatch.setattr(follow, "_watch_lines", _append_while_arming)
    assert asyncio.run(_one_tree_line(tmp_path)).text == "late"


@pytest.mark.parametrize("replay", [False, True])
def test_tree_resume_preserves_history_selection(
    tmp_path: Path,
    *,
    replay: bool,
) -> None:
    """Resume replays one file; replay includes its neighbors exactly once."""
    selected = tmp_path / "a"
    selected.write_text("selected\n")
    (tmp_path / "b").write_text("neighbor\n")
    expected = ["selected", "neighbor"] if replay else ["selected"]
    assert asyncio.run(_resume_lines(tmp_path, selected, replay=replay)) == expected


async def _resume_lines(root: Path, selected: Path, *, replay: bool) -> list[str]:
    lines = follow.follow_tree(
        root,
        match=lambda path: path.name in {"a", "b"},
        resume=frozenset({selected}),
        replay=replay,
    )
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        result = [(await asyncio.wait_for(anext(lines), 2)).text]
        if replay:
            result.append((await asyncio.wait_for(anext(lines), 2)).text)
        with selected.open("a") as writer:
            writer.write("appended\n")
            writer.flush()
            assert (await asyncio.wait_for(anext(lines), 2)).text == "appended"
        return result


@asynccontextmanager
async def _append_while_arming(
    *directories: Path,
    match: Callable[[Path], bool],
    existing: set[Path],
) -> AsyncGenerator[AsyncIterator[set[Path]]]:
    del match, existing
    with (directories[0] / "log").open("a") as writer:
        writer.write("late\n")
    yield _no_events()


async def _one_tree_line(root: Path) -> follow.Line:
    lines = follow.follow_tree(root, match=lambda path: path.name == "log")
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        return await anext(lines)


def test_tree_requires_a_root() -> None:
    """The line watcher rejects an empty root list on every platform."""
    with pytest.raises(ValueError, match="directory"):
        asyncio.run(_empty_tree())


async def _empty_tree() -> None:
    lines = follow.follow_tree(match=lambda path: path.name == "log")
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        await asyncio.wait_for(anext(lines), 0.2)


def test_vnode_selection_arms_before_yielding(tmp_path: Path) -> None:
    """Ignored paths are not watched; selected paths are armed before a read."""
    asyncio.run(_selected_vnodes(tmp_path))


async def _selected_vnodes(root: Path) -> None:
    selected = root / "log"
    watched: list[Path] = []
    queue: asyncio.Queue[Path] = asyncio.Queue()
    queue.put_nowait(root / "ignored")
    queue.put_nowait(selected)
    async with contextlib.aclosing(
        follow._vnode_changes(
            follow._drain_queue(queue),
            watched.append,
            lambda path: path == selected,
        ),
    ) as changes:
        assert await anext(changes) == {selected}
        assert watched == [selected]
        queue.put_nowait(selected)
        assert await anext(changes) == {selected}
        assert watched == [selected, selected]


@pytest.mark.parametrize("offset", [0, 5])
@pytest.mark.parametrize("drain_first", [False, True])
@pytest.mark.parametrize("replacement", [b"same\n", b"same\nnew\n"])
def test_cursor_new_inode_replays_shared_prefix(
    tmp_path: Path,
    offset: int,
    replacement: bytes,
    *,
    drain_first: bool,
) -> None:
    target = tmp_path / "log"
    target.write_bytes(b"same\n")
    cursor = follow._Cursor(target, offset=offset)
    if drain_first:
        assert cursor.drain() == ([] if offset else ["same"])
    staged = tmp_path / "replacement"
    staged.write_bytes(replacement)
    staged.replace(target)
    assert cursor.drain() == replacement.decode().splitlines()
    assert cursor.restarted
    assert cursor.drain() == []
    with target.open("ab") as writer:
        writer.write(b"later\n")
    assert cursor.drain() == ["later"]
    assert not cursor.restarted


@pytest.mark.parametrize("initial", [b"", "café".encode()[:-1]])
@pytest.mark.parametrize("replacement", [b"", "café".encode()[:-1]])
def test_cursor_new_inode_defers_restart_until_complete_line(
    tmp_path: Path,
    initial: bytes,
    replacement: bytes,
) -> None:
    target = tmp_path / "log"
    target.write_bytes(initial)
    cursor = follow._Cursor(target, offset=0)
    assert cursor.drain() == []
    staged = tmp_path / "replacement"
    staged.write_bytes(replacement)
    staged.replace(target)
    assert cursor.drain() == []
    assert cursor.drain() == []
    with target.open("ab") as writer:
        writer.write(b"\xa9\n" if replacement else "café\n".encode())
    assert cursor.drain() == ["café"]
    assert cursor.restarted


def test_cursor_first_creation_is_not_a_restart(tmp_path: Path) -> None:
    target = tmp_path / "log"
    cursor = follow._Cursor(target, offset=0)
    assert cursor.drain() == []
    target.write_text("first\n")
    assert cursor.drain() == ["first"]
    assert not cursor.restarted


@pytest.mark.parametrize("replay", [False, True])
def test_tree_new_inode_replays_shared_prefix(tmp_path: Path, *, replay: bool) -> None:
    asyncio.run(_shared_prefix_replacement(tmp_path, replay=replay))


async def _shared_prefix_replacement(root: Path, *, replay: bool) -> None:
    target = root / "log"
    target.write_text("same\n")
    armed = asyncio.Event()
    lines = follow.follow_tree(
        root,
        match=lambda p: p == target,
        replay=replay,
        on_armed=armed.set,
    )
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        pending = asyncio.create_task(anext(lines))
        try:
            await asyncio.wait_for(armed.wait(), 2)
            if replay:
                assert (await asyncio.wait_for(pending, 2)).text == "same"
                pending = asyncio.create_task(anext(lines))
            staged = root / "replacement"
            with staged.open("w") as writer:
                writer.write("same\nnew\n")
                writer.flush()
                staged.replace(target)
                assert await asyncio.wait_for(pending, 2) == follow.Line(
                    path=target,
                    text="same",
                    restart=True,
                )
                assert await asyncio.wait_for(anext(lines), 2) == follow.Line(
                    path=target,
                    text="new",
                )
                writer.write("later\n")
                writer.flush()
                assert await asyncio.wait_for(anext(lines), 2) == follow.Line(
                    path=target,
                    text="later",
                )
        finally:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS vnode notifications")
@pytest.mark.parametrize("writer_predates_follow", [False, True])
def test_settled_writer_needs_no_close(
    tmp_path: Path,
    *,
    writer_predates_follow: bool,
) -> None:
    asyncio.run(
        _settled_writer(tmp_path, writer_predates_follow=writer_predates_follow),
    )


async def _settled_writer(root: Path, *, writer_predates_follow: bool) -> None:
    target = root / "log"
    target.write_text("history\n")
    # Recent creation events can mask missing eager vnode watches.
    await asyncio.sleep(3)
    armed = asyncio.Event()
    lines = follow.follow_tree(root, match=lambda p: p == target, on_armed=armed.set)
    assert _is_async_generator(lines)
    with contextlib.ExitStack() as stack:
        writer = (
            stack.enter_context(target.open("a")) if writer_predates_follow else None
        )
        async with contextlib.aclosing(lines):
            pending = asyncio.create_task(anext(lines))
            try:
                await asyncio.wait_for(armed.wait(), 5)
                await asyncio.sleep(3)
                assert not pending.done()
                if writer is None:
                    writer = stack.enter_context(target.open("a"))
                for index in range(3):
                    writer.write(f"append-{index}\n")
                    writer.flush()
                    line = await asyncio.wait_for(
                        pending if index == 0 else anext(lines),
                        2,
                    )
                    assert line == follow.Line(path=target, text=f"append-{index}")
            finally:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending


def test_cursor_recovers_when_initial_open_fails(tmp_path: Path) -> None:
    target = tmp_path / "log"
    target.write_text("same\n")
    with patch.object(Path, "open", side_effect=PermissionError):
        cursor = follow._Cursor(target, offset=5)
    assert cursor.drain() == ["same"]
    assert cursor.restarted


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS vnode registration")
def test_vnode_registration_error_releases_open_file(tmp_path: Path) -> None:
    before = len(list(Path("/dev/fd").iterdir()))
    with pytest.raises(OSError, match="registration refused"):
        asyncio.run(_fail_discovered_vnode(tmp_path))
    assert len(list(Path("/dev/fd").iterdir())) == before


async def _fail_discovered_vnode(root: Path) -> None:
    target = root / "log"
    async with follow._watch_lines(
        root,
        match=lambda path: path == target,
        existing=set(),
    ) as changed:
        target.write_text("new\n")
        with patch.object(
            select,
            "kevent",
            side_effect=OSError(errno.ENOSPC, "registration refused"),
        ):
            await asyncio.wait_for(anext(changed), 2)


@pytest.mark.skipif(platform.system() != "Linux", reason="inotify errors")
def test_inotify_init_failure_is_reported() -> None:
    libc = follow._libc()
    with (
        patch.object(libc, "inotify_init1", return_value=-1),
        patch.object(follow, "_libc", return_value=libc),
        pytest.raises(OSError, match="inotify_init1 failed"),
    ):
        follow._inotify_fd()


@pytest.mark.skipif(platform.system() != "Linux", reason="inotify errors")
def test_inotify_refused_descendant_keeps_root_watch(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    libc = follow._libc()
    watches: dict[int, Path] = {}
    with patch.object(libc, "inotify_add_watch", side_effect=[7, -1]):
        ctypes.set_errno(errno.ENOSPC)
        follow._watch_tree(libc, -1, tmp_path, watches)
    assert watches == {7: tmp_path}


@pytest.mark.skipif(platform.system() != "Linux", reason="inotify event decoding")
def test_inotify_ignored_and_unknown_descriptors(tmp_path: Path) -> None:
    watches = {7: tmp_path}
    ignored = follow._EVENT_HEADER.pack(7, follow._IN_IGNORED, 0, 0)
    unknown = follow._EVENT_HEADER.pack(8, follow._IN_MODIFY, 0, 4) + b"log\0"
    assert follow._read_events(ignored + unknown, follow._libc(), -1, watches) == set()
    assert watches == {}


@pytest.mark.skipif(platform.system() != "Linux", reason="inotify nonblocking read")
def test_inotify_empty_read_is_not_a_failure(tmp_path: Path) -> None:
    fd, watches = follow._inotify_fd(tmp_path)
    try:
        assert follow._read_inotify(follow._libc(), fd, watches) == set()
    finally:
        os.close(fd)


def test_overflow_rescan_tolerates_vanished_directory(tmp_path: Path) -> None:
    target = tmp_path / "log"
    target.write_text("line\n")
    assert follow._rescan({1: tmp_path, 2: tmp_path / "absent"}) == {target}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
