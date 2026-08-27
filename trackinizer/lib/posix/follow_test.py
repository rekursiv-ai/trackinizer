"""Tests for following a growing file."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import asyncio
import contextlib
import ctypes
import errno
import os
import platform

import pytest

from trackinizer.lib.posix import follow
from trackinizer.lib.posix.follow import follow_dir, follow_file


def test_yields_appended_lines(tmp_path: Path) -> None:
    """A line appended after the follow starts is delivered."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.sleep(0.1)
        with target.open("a") as handle:
            _ = handle.write("one\n")
        return await task

    assert asyncio.run(run()) == ["one"]


def test_skips_what_was_already_there(tmp_path: Path) -> None:
    """Lines present before the follow started are not delivered."""
    target = tmp_path / "log"
    _ = target.write_text("old\n")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.sleep(0.1)
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


def test_holds_a_partial_line(tmp_path: Path) -> None:
    """A line split across writes is delivered once, whole."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.sleep(0.1)
        with target.open("a") as handle:
            _ = handle.write("split")
            handle.flush()
            await asyncio.sleep(0.1)
            _ = handle.write("-line\n")
        return await task

    assert asyncio.run(run()) == ["split-line"]


def test_restarts_after_a_rewrite(tmp_path: Path) -> None:
    """A file that shrinks is re-read from its new start.

    A rewrite leaves the byte offset past the new end, so a follower that
    kept it would stall; one that kept its partial buffer would prepend dead
    bytes to the first new line.
    """
    target = tmp_path / "log"
    _ = target.write_text("aaa\nbbb\n")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.sleep(0.1)
        _ = target.write_text("fresh\n")
        return await task

    assert asyncio.run(run()) == ["fresh"]


def test_follows_a_file_created_later(tmp_path: Path) -> None:
    """The file need not exist when the follow starts."""
    target = tmp_path / "log"

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 5.0))
        await asyncio.sleep(0.1)
        _ = target.write_text("late\n")
        return await task

    assert asyncio.run(run()) == ["late"]


def test_ignores_a_sibling_file(tmp_path: Path) -> None:
    """A write to another file in the same directory is not delivered."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 1, 0.6))
        await asyncio.sleep(0.1)
        _ = (tmp_path / "other").write_text("elsewhere\n")
        return await task

    assert asyncio.run(run()) == []


def test_delivers_a_burst_in_order(tmp_path: Path) -> None:
    """Several lines written at once arrive in the order written."""
    target = tmp_path / "log"
    _ = target.write_text("")

    async def run() -> list[str]:
        task = asyncio.create_task(_take(target, 3, 5.0))
        await asyncio.sleep(0.1)
        with target.open("a") as handle:
            _ = handle.write("one\ntwo\nthree\n")
        return await task

    assert asyncio.run(run()) == ["one", "two", "three"]


async def _take(
    path: Path, count: int, timeout_sec: float, *, replay: bool = False
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
        task = asyncio.create_task(_await_wake(tmp_path, 5.0))
        # Let the watch register before the write; otherwise the event
        # predates the watch and nothing is delivered.
        await asyncio.sleep(0.1)
        _ = (tmp_path / "session.jsonl").write_text("{}\n")
        return await task

    assert asyncio.run(run()) == {tmp_path / "session.jsonl"}


def test_wakes_on_append(tmp_path: Path) -> None:
    """Appending to an existing file wakes the caller."""
    target = tmp_path / "session.jsonl"
    _ = target.write_text("{}\n")

    async def run() -> set[Path]:
        task = asyncio.create_task(_await_wake(tmp_path, 5.0))
        await asyncio.sleep(0.1)
        with target.open("a") as handle:
            _ = handle.write("{}\n")
        return await task

    assert asyncio.run(run()) == {target}


def test_wakes_on_rewrite(tmp_path: Path) -> None:
    """A whole-file rewrite wakes the caller.

    Compaction replaces the transcript rather than appending, so a watcher
    that only saw growth would miss it.
    """
    target = tmp_path / "session.jsonl"
    _ = target.write_text("aaaa\n")

    async def run() -> set[Path]:
        task = asyncio.create_task(_await_wake(tmp_path, 5.0))
        await asyncio.sleep(0.1)
        _ = target.write_text("b\n")
        return await task

    assert asyncio.run(run()) == {target}


def test_ignores_other_directories(tmp_path: Path) -> None:
    """A write outside the watched directory does not wake the caller."""
    watched = tmp_path / "watched"
    other = tmp_path / "other"
    watched.mkdir()
    other.mkdir()

    async def run() -> set[Path]:
        task = asyncio.create_task(_await_wake(watched, 0.5))
        await asyncio.sleep(0.1)
        _ = (other / "session.jsonl").write_text("{}\n")
        return await task

    assert asyncio.run(run()) == set()


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    """Watching a directory that does not exist fails loudly."""

    async def run() -> None:
        async with follow_dir(tmp_path / "absent") as woken:
            _ = await anext(woken)

    with pytest.raises(FileNotFoundError):
        asyncio.run(run())


def test_watch_is_armed_before_entering_the_context(tmp_path: Path) -> None:
    """A change made in the context body cannot predate the kernel watch.

    Every other test here pauses before writing, so a watch armed lazily --
    on the first ``anext`` rather than on entry -- would pass them all while
    silently losing whatever a caller wrote in the window.
    """

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
                await asyncio.sleep(0.1)
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
                await asyncio.sleep(0.1)
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
                await asyncio.sleep(0.1)
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
        self, tmp_path: Path
    ) -> None:
        leaf = tmp_path / "2026" / "08" / "25"
        leaf.mkdir(parents=True)

        async def run() -> set[Path]:
            async with follow_dir(tmp_path) as changed:
                await asyncio.sleep(0.1)
                _ = (leaf / "rollout.jsonl").write_text("{}\n")
                return await asyncio.wait_for(anext(changed), 5.0)

        assert asyncio.run(run()) == {leaf / "rollout.jsonl"}

    def test_wakes_for_a_subdirectory_created_after_the_watch(
        self, tmp_path: Path
    ) -> None:
        """A directory born mid-run must get its own watch as it appears."""

        async def run() -> set[Path]:
            woken: set[Path] = set()
            async with follow_dir(tmp_path) as changed:
                await asyncio.sleep(0.1)
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
        self, tmp_path: Path
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
                await asyncio.sleep(0.1)
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
                await asyncio.sleep(0.1)
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
        target.chmod(0o000)
        try:
            assert cursor.drain() == []
        finally:
            target.chmod(0o644)

    def test_missing_file_measures_zero(self, tmp_path: Path) -> None:
        assert follow._size(tmp_path / "absent") == 0


def _recording_warning(into: list[str]) -> Callable[..., None]:
    """A ``logger.warning`` stand-in that captures the formatted message."""

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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            libc: ctypes.CDLL, fd: int, directory: Path, watches: dict[int, Path]
        ) -> None:
            if directory == doomed:
                raise FileNotFoundError(errno.ENOENT, "no such directory", str(doomed))
            real_add(libc, fd, directory, watches)

        monkeypatch.setattr(follow, "_add_watch", vanishing)

        async def run() -> set[Path]:
            async with follow.follow_dir(tmp_path) as changed:
                await asyncio.sleep(0.1)
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hitting the watch limit is not "the directory vanished".

        ``_adopt`` treated every OSError as a race, so exhausting
        ``max_user_watches`` left the new subtree permanently unwatched with
        nothing said -- the one failure an operator can actually act on.
        """
        warned: list[str] = []
        real_tree = follow._watch_tree

        def refusing(
            libc: ctypes.CDLL, fd: int, directory: Path, watches: dict[int, Path]
        ) -> None:
            if directory.name == "born-later":
                raise OSError(errno.ENOSPC, "watch limit reached", str(directory))
            real_tree(libc, fd, directory, watches)

        monkeypatch.setattr(follow, "_watch_tree", refusing)
        monkeypatch.setattr(follow._logger, "warning", _recording_warning(warned))

        async def run() -> None:
            async with follow.follow_dir(tmp_path) as changed:
                await asyncio.sleep(0.1)
                (tmp_path / "born-later").mkdir()
                with contextlib.suppress(TimeoutError):
                    _ = await asyncio.wait_for(anext(changed), 1.0)

        asyncio.run(run())
        assert any("watch" in w for w in warned), (
            "a refused watch left the subtree uncovered with nothing logged"
        )

    def test_a_queue_overflow_rescans_rather_than_losing_the_writes(
        self, tmp_path: Path
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counterpart: a directory that really did vanish is routine.

        Warning on it would make the log useless for the case above.
        """
        warned: list[str] = []
        real_tree = follow._watch_tree

        def vanished(
            libc: ctypes.CDLL, fd: int, directory: Path, watches: dict[int, Path]
        ) -> None:
            if directory.name == "born-later":
                raise FileNotFoundError(errno.ENOENT, "gone", str(directory))
            real_tree(libc, fd, directory, watches)

        monkeypatch.setattr(follow, "_watch_tree", vanished)
        monkeypatch.setattr(follow._logger, "warning", _recording_warning(warned))

        async def run() -> None:
            async with follow.follow_dir(tmp_path) as changed:
                await asyncio.sleep(0.1)
                (tmp_path / "born-later").mkdir()
                with contextlib.suppress(TimeoutError):
                    _ = await asyncio.wait_for(anext(changed), 1.0)

        asyncio.run(run())
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
            lines = follow.follow_tree(tmp_path, match=lambda p: p.suffix == ".jsonl")
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.sleep(0.1)
            _ = (tmp_path / "session.jsonl").write_text('{"n":1}\n')
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "session.jsonl", '{"n":1}')]

    def test_ignores_a_file_the_predicate_rejects(self, tmp_path: Path) -> None:
        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            lines = follow.follow_tree(tmp_path, match=lambda p: p.suffix == ".jsonl")
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.sleep(0.1)
            _ = (tmp_path / "notes.txt").write_text("ignored\n")
            _ = (tmp_path / "session.jsonl").write_text('{"n":1}\n')
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "session.jsonl", '{"n":1}')]

    def test_follows_several_files_at_once(self, tmp_path: Path) -> None:
        """Two live sessions in one tree keep separate byte cursors."""

        async def run() -> set[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            lines = follow.follow_tree(tmp_path, match=lambda p: p.suffix == ".jsonl")
            task = asyncio.create_task(_collect(lines, seen, 4))
            await asyncio.sleep(0.1)
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
        self, tmp_path: Path
    ) -> None:
        """Codex's shape: the day directory is born during the run."""

        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            lines = follow.follow_tree(tmp_path, match=lambda p: p.suffix == ".jsonl")
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.sleep(0.1)
            leaf = tmp_path / "2026" / "08" / "25"
            leaf.mkdir(parents=True)
            _ = (leaf / "rollout.jsonl").write_text('{"n":1}\n')
            await asyncio.wait_for(task, 5.0)
            return seen

        expected = tmp_path / "2026" / "08" / "25" / "rollout.jsonl"
        assert asyncio.run(run()) == [(expected, '{"n":1}')]

    def test_skips_files_present_before_the_follow(self, tmp_path: Path) -> None:
        """A prior session's transcript is not this run's to capture."""
        _ = (tmp_path / "old.jsonl").write_text("stale\n")

        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            lines = follow.follow_tree(tmp_path, match=lambda p: p.suffix == ".jsonl")
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.sleep(0.1)
            _ = (tmp_path / "new.jsonl").write_text("fresh\n")
            await asyncio.wait_for(task, 5.0)
            return seen

        assert asyncio.run(run()) == [(tmp_path / "new.jsonl", "fresh")]

    def test_holds_a_partial_line_per_file(self, tmp_path: Path) -> None:
        """A line split across writes is delivered once, whole."""

        async def run() -> list[tuple[Path, str]]:
            seen: list[tuple[Path, str]] = []
            lines = follow.follow_tree(tmp_path, match=lambda p: p.suffix == ".jsonl")
            task = asyncio.create_task(_collect(lines, seen, 1))
            await asyncio.sleep(0.1)
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
    lines: AsyncIterator[tuple[Path, str]],
    into: list[tuple[Path, str]],
    count: int,
) -> None:
    """Accumulate ``count`` items from ``lines`` into ``into``."""
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
        platform.system() == "Darwin", reason="native here; the real backend runs"
    )
    def test_darwin_uses_the_fsevents_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off macOS, the Darwin branch still reaches FSEvents."""
        started: list[tuple[Path, ...]] = []
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(follow, "_watch_fsevents", _recording_watch(started))

        asyncio.run(_open_and_close(tmp_path))
        assert started == [(tmp_path,)]

    @pytest.mark.skipif(
        platform.system() == "Linux", reason="native here; the real backend runs"
    )
    def test_linux_uses_the_inotify_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                await asyncio.sleep(0.1)
                _ = (tmp_path / "native.jsonl").write_text("{}\n")
                return await asyncio.wait_for(anext(changed), 5.0)

        assert asyncio.run(run()) == {tmp_path / "native.jsonl"}

    def test_a_platform_with_neither_backend_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            lambda o: o.fire(Path("/watched/tmp"), dest=Path("/watched/session.jsonl"))
        )
        assert emitted == [{Path("/watched/tmp"), Path("/watched/session.jsonl")}]

    def test_ignores_a_directory_event(self) -> None:
        """A directory is not a file to follow; only its contents are."""
        emitted = _run_fsevents(
            lambda o: (
                o.fire(Path("/watched/subdir"), is_directory=True),
                o.fire(Path("/watched/session.jsonl")),
            )
        )
        assert emitted == [{Path("/watched/session.jsonl")}]

    def test_schedules_every_directory_recursively(self) -> None:
        """One stream per tree is the whole reason for choosing FSEvents."""
        observer = _StubObserver()

        async def run() -> None:
            async with follow._fsevents_events(
                observer, (Path("/first"), Path("/second"))
            ):
                pass

        asyncio.run(run())
        assert observer.scheduled == [Path("/first"), Path("/second")]
        assert observer.started
        assert observer.stopped, "the observer thread outlived the watch"


def _recording_watch(
    into: list[tuple[Path, ...]],
) -> Callable[..., AbstractAsyncContextManager[AsyncIterator[set[Path]]]]:
    """A ``follow_dir`` backend that records its directories and yields nothing."""

    @asynccontextmanager
    async def watch(*directories: Path) -> AsyncGenerator[AsyncIterator[set[Path]]]:
        into.append(directories)
        yield _no_events()

    return watch


async def _no_events() -> AsyncIterator[set[Path]]:
    """An iterator that ends immediately, standing in for a quiet watch."""
    for never in ():
        yield never


async def _open_and_close(directory: Path) -> None:
    """Enter and leave a watch, tolerating one that reports nothing."""
    async with follow.follow_dir(directory) as changed:
        with contextlib.suppress(StopAsyncIteration):
            _ = await anext(changed)


def _run_fsevents(fire: Callable[[_StubObserver], object]) -> list[set[Path]]:
    """Drive the FSEvents adapter with ``fire``; return what it emitted."""
    emitted: list[set[Path]] = []

    async def run() -> None:
        observer = _StubObserver()
        async with follow._fsevents_events(observer, (Path("/watched"),)) as changed:
            collector = asyncio.create_task(_gather(changed, emitted, 1))
            await asyncio.sleep(0.05)
            _ = fire(observer)
            await asyncio.wait_for(collector, 5.0)

    asyncio.run(run())
    return emitted


async def _gather(
    changed: AsyncIterator[set[Path]], into: list[set[Path]], count: int
) -> None:
    """Collect ``count`` wakes from ``changed``."""
    async for paths in changed:
        into.append(paths)
        if len(into) >= count:
            return


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
            cast(Any, handler).on_any_event(event)


class _StubEvent:
    """The three attributes the adapter reads off a watchdog event."""

    def __init__(
        self, *, src_path: str, dest_path: str = "", is_directory: bool = False
    ) -> None:
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


async def _await_wake(directory: Path, timeout_sec: float) -> set[Path]:
    """Collect one wake from ``directory``, or an empty set on timeout."""
    async with follow_dir(directory) as woken:
        try:
            return await asyncio.wait_for(anext(woken), timeout_sec)
        except TimeoutError:
            return set()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
