"""Follow a growing file, and the directory watch that wakes it.

``tail -F`` as an async generator, plus the kernel notification underneath.
A regular file cannot be waited on -- POSIX reports it always ready, so
``select`` returns instantly at EOF and a naive loop spins -- so
:func:`follow_file` waits on a change notification for the containing
DIRECTORY and reads only when there is something to read. That is what a
reader of an append-only log needs instead of a poll interval.

:func:`follow_dir` watches SEVERAL directories and their subtrees. Several,
because every caller names a list of them and one descriptor holds many
watches -- a watch-per-directory API would multiply fds against a 128-instance
kernel ceiling. Subtrees, because neither kernel watches recursively: a new
subdirectory gets its own watch as it appears, which is the emulation every
recursive watcher performs.

Linux inotify is reached through ``ctypes``, since CPython ships no inotify
module and the alternative is a dependency for one syscall. Elsewhere the
watching is delegated (see :func:`follow_dir`); a platform with neither raises
rather than falling back to a timer nobody asked for.

Four hazards :func:`follow_file` handles, each of which silently loses a line:

* The file need not exist when the follow starts.
* A read can land mid-line, because a writer appends without atomicity.
* A rewrite leaves the byte offset past the new end, so the follower stalls;
  a held partial buffer would prepend dead bytes to the first new line.
* Another file in the same directory changing is not this file changing.

And one the WATCH handles, because no later event ever repeats it: a file
written into a brand-new subdirectory before its watch is armed is invisible
forever. ``inotify(7)`` prescribes the fix -- "scan the contents of the
subdirectory immediately after adding the watch" -- so every newly watched
directory is listed once, and its entries reported as though the kernel had
named them.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Final, Protocol, cast

import asyncio
import ctypes
import ctypes.util
import errno
import logging
import os
import platform
import struct

from wrapt import lazy_import


__all__ = ["follow_dir", "follow_file", "follow_tree"]


_logger = logging.getLogger(__name__)

# How long to wait for watchdog's observer thread to exit on teardown. Bounded
# because it is a plain thread, not a daemon: a wedged join would hang the
# caller's whole process on exit.
_OBSERVER_JOIN_SEC: Final = 5.0


async def follow_file(path: Path, *, replay: bool = False) -> AsyncIterator[str]:
    """Yield each line ``path`` gains, indefinitely.

    Args:
      path: File to follow; it need not exist yet.
      replay: Whether to yield the lines already in the file. Off by default,
        matching ``tail -F``: a follower that replayed would re-deliver a
        history the caller has usually already seen.

    Yields:
      line: One line, without its terminator.

    Raises:
      FileNotFoundError: ``path.parent`` does not exist. The FILE may be
        absent -- that is the point -- but its directory is what gets watched.
      NotImplementedError: The platform has no supported watch mechanism.

    """
    state = _Cursor(path, offset=0 if replay else _size(path))
    # The watch is registered BEFORE the first read. Reading first would lose
    # a write landing in between: the watch reports only what follows it, so
    # that line would wait for an unrelated later change.
    async with follow_dir(path.parent) as changed:
        for line in state.drain():
            yield line
        async for paths in changed:
            if path not in paths:
                continue
            for line in state.drain():
                yield line


async def follow_tree(
    *directories: Path,
    match: Callable[[Path], bool],
    replay: bool = False,
    on_armed: Callable[[], None] | None = None,
) -> AsyncIterator[tuple[Path, str]]:
    """Yield each line gained by any matching file under ``directories``.

    :func:`follow_file` needs the path in advance. A caller draining a CLI's
    session logs does not have one: the CLI mints the filename at startup,
    sometimes in a directory that does not exist yet. It knows only the roots
    and how to recognise a log, which is what this takes.

    Args:
      *directories: Roots to watch, with their subtrees.
      match: Whether a path is a file this caller wants followed.
      replay: Whether to yield what matching files already hold. Off by
        default: a file present before the follow began belongs to an earlier
        session, and replaying it would re-capture that session's history.
      on_armed: Called once the watch is registered, before anything is
        yielded. A caller that STARTS the writer -- spawning the process whose
        output it is following -- has no other way to know when doing so is
        safe: arming happens on the first ``anext``, which it cannot await
        before the writer exists.

    Yields:
      line: The file it came from, and one line without its terminator.

    Raises:
      ValueError: No directory was given.
      FileNotFoundError: One of ``directories`` does not exist.

    """
    cursors: dict[Path, _Cursor] = {}
    # Snapshot BEFORE arming, never after. What a later event names is either
    # in this set -- pre-existing, so resume at its end rather than re-emit its
    # history -- or is not, so read it whole. Taken AFTER arming instead, a
    # file created in the arming window lands in the set, its cursor starts at
    # its current end, and every line it already holds is lost for good; the
    # ordering here means such a file is merely read whole, which is the
    # survivable direction to be wrong in.
    existing = {
        path
        for directory in directories
        for path in directory.rglob("*")
        if path.is_file() and match(path)
    }
    async with follow_dir(*directories) as changed:
        if on_armed is not None:
            on_armed()
        if replay:
            for path in sorted(existing):
                cursors[path] = _Cursor(path, offset=0)
            for path in sorted(cursors):
                for line in cursors[path].drain():
                    yield (path, line)
        async for paths in changed:
            for path in sorted(paths):
                if not match(path):
                    continue
                cursor = cursors.get(path)
                if cursor is None:
                    cursor = _Cursor(
                        path, offset=_size(path) if path in existing else 0
                    )
                    cursors[path] = cursor
                for line in cursor.drain():
                    yield (path, line)


class _Cursor:
    """Tracks how far into a file has been read, and any partial line.

    Reads in BINARY and advances by the bytes actually read. Both are
    load-bearing, for hazards a size-and-text-handle cursor loses data to:

    * A multibyte character can be split across two appends. A text handle
      decodes each read independently, so with ``errors="replace"`` each half
      becomes U+FFFD and the line is silently corrupted. Holding the
      undecodable tail as bytes carries the character across the split.
    * A rewrite is not always a SHRINK. Deciding "was this replaced?" by
      length leaves a same-or-larger replacement read from the stale offset,
      yielding the tail of a line nobody wrote. The bytes immediately before
      the offset are re-read and compared instead.
    * A write landing between measuring and reading is read now; recording
      the measurement rather than the read would deliver it again next time.
    """

    def __init__(self, path: Path, *, offset: int) -> None:
        self._path = path
        self._offset = offset
        # Bytes read but not yet forming a complete line: an unterminated
        # line, and possibly a partial UTF-8 sequence at its end.
        self._partial = b""
        # The bytes immediately before ``offset``, which a later drain
        # re-reads to tell an append from a replacement. Seeded here because a
        # cursor may START mid-file (``offset=_size(path)``, the skip-history
        # case) without ever having read them itself.
        self._seen = _tail(path, offset)

    def drain(self) -> list[str]:
        """Return every complete line appended since the last call."""
        try:
            with self._path.open("rb") as handle:
                if self._offset and not self._continues(handle):
                    # The bytes this cursor already read are gone or changed:
                    # the file was replaced. Its offset and held fragment both
                    # describe bytes that no longer exist.
                    self._offset = 0
                    self._partial = b""
                    self._seen = b""
                _ = handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return []
        if not chunk:
            return []
        self._offset += len(chunk)
        self._seen = (self._seen + chunk)[-_REPLACEMENT_WINDOW:]
        raw = self._partial + chunk
        # Split on BYTES; decode only complete lines, so a character
        # straddling two reads is held rather than replaced.
        complete, newline, self._partial = raw.rpartition(b"\n")
        if not newline:
            return []
        return complete.decode("utf-8", errors="replace").split("\n")

    def _continues(self, handle: IO[bytes]) -> bool:
        """Whether the file still holds the bytes this cursor last read."""
        if not self._seen:
            return False
        _ = handle.seek(self._offset - len(self._seen))
        return handle.read(len(self._seen)) == self._seen


_REPLACEMENT_WINDOW: Final = 4_096
"""How many consumed bytes a cursor keeps to recognise a replacement.

Bounded because a session file reaches megabytes. A window is sufficient
because a rewrite would have to reproduce this many preceding bytes exactly
and then diverge to pass as an append, which a session log does not do.
"""


def _tail(path: Path, offset: int) -> bytes:
    """The bytes just before ``offset``, or empty when unreadable."""
    if offset <= 0:
        return b""
    window = min(offset, _REPLACEMENT_WINDOW)
    try:
        with path.open("rb") as handle:
            _ = handle.seek(offset - window)
            return handle.read(window)
    except OSError:
        return b""


def _size(path: Path) -> int:
    """Byte length of ``path``, or zero when it does not exist."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


_IN_MODIFY: Final = 0x0000_0002
_IN_CREATE: Final = 0x0000_0100
_IN_MOVED_TO: Final = 0x0000_0080
"""A rename INTO the directory. Compaction and atomic writes land this way
rather than as a create, so a watcher without it misses the new transcript."""

_IN_IGNORED: Final = 0x0000_8000
"""The kernel dropped a watch. Its descriptor is then free to be REUSED for a
later directory, so an entry left in the map would rebuild paths under a
directory that no longer owns that number."""

_IN_ISDIR: Final = 0x4000_0000
"""Set on an event naming a directory rather than a file; the only way to
know a new entry needs a watch of its own."""

_IN_Q_OVERFLOW: Final = 0x0000_4000
"""The kernel's event queue filled and it DISCARDED events.

Unsolicited -- it is not in the requested mask -- and it arrives with
``wd = -1``, naming no watch. Whatever it stands for is gone: no later event
repeats a dropped one, and the caller cannot detect the loss itself. The only
recovery is to re-list every watched directory, which is what the reader does
on seeing it."""

_WATCH_MASK: Final = _IN_MODIFY | _IN_CREATE | _IN_MOVED_TO

_EVENT_HEADER: Final = struct.Struct("iIII")
"""``struct inotify_event``: wd, mask, cookie, name length. The name follows,
NUL-padded to that length."""


@asynccontextmanager
async def follow_dir(*directories: Path) -> AsyncGenerator[AsyncIterator[set[Path]]]:
    """Yield an iterator of changed paths, waking when the kernel says so.

    Each iteration returns every path that changed since the last one, so a
    burst of writes wakes the caller once rather than per event. Each
    directory is watched with its whole subtree, including subdirectories
    created later.

    Args:
      *directories: Directories to watch. Every one must exist: a watch that
        silently covered fewer than asked would leave the caller believing a
        directory is covered with no way to learn otherwise.

    Yields:
      changed: Iterator whose every item is a set of changed paths.

    Raises:
      ValueError: No directory was given.
      FileNotFoundError: One of ``directories`` does not exist.
      NotImplementedError: The platform has no supported watch mechanism.

    """
    if not directories:
        raise ValueError("follow_dir requires at least one directory")
    system = platform.system()
    if system == "Darwin":
        async with _watch_fsevents(*directories) as changed:
            yield changed
        return
    if system != "Linux":
        raise NotImplementedError(
            f"{system} has no supported watch mechanism; "
            "a recursive backend is needed here"
        )
    fd, watches = _inotify_fd(*directories)
    try:
        yield _inotify_events(fd, watches)
    finally:
        os.close(fd)


@asynccontextmanager
async def _watch_fsevents(
    *directories: Path,
) -> AsyncGenerator[AsyncIterator[set[Path]]]:
    """The macOS backend: one FSEvents stream per tree, natively recursive.

    kqueue is the wrong tool on this platform. It holds an open descriptor per
    watched directory, so emulating recursion over a few thousand of them
    exhausts the default file-handle limit -- Apple's guidance is to use
    file-system events for a large hierarchy, which is what watchdog's
    ``fsevents`` observer wraps.

    Raises:
      FileNotFoundError: A directory does not exist.
      NotImplementedError: watchdog's FSEvents backend is unavailable.

    """
    for directory in directories:
        if not directory.is_dir():
            raise FileNotFoundError(errno.ENOENT, "no such directory", str(directory))
    observer = _fsevents_observer()
    async with _fsevents_events(observer, directories) as changed:
        yield changed


_fsevents = lazy_import("watchdog.observers.fsevents")
"""Watchdog's FSEvents backend, resolved on first use.

Deferred because the module is macOS-only: importing it eagerly would make
this module unimportable on Linux, where its ``_watchdog_fsevents`` extension
does not exist. The proxy defers that failure to the one call that needs it.
"""


class _Observer(Protocol):
    """The four methods this module calls on a watchdog observer.

    Named rather than cast to ``Any``: watchdog ships no types reachable here
    (its FSEvents module does not import off macOS), and a Protocol keeps the
    call sites checked -- a renamed ``schedule`` is a type error instead of an
    AttributeError on a Mac nobody runs the tests on.
    """

    def schedule(self, handler: object, path: str, *, recursive: bool) -> None:
        """Watch one directory tree, dispatching events to ``handler``."""
        ...

    def start(self) -> None:
        """Begin dispatching on the observer's own thread."""
        ...

    def stop(self) -> None:
        """Ask the observer thread to finish."""
        ...

    def join(self, timeout: float | None = None) -> None:
        """Wait for the observer thread to exit."""
        ...


def _fsevents_observer() -> _Observer:
    """Build watchdog's FSEvents observer.

    Raises:
      NotImplementedError: The FSEvents backend is unavailable, which on a
        non-macOS host is every time -- the extension ships only there.

    """
    try:
        return cast("_Observer", _fsevents.FSEventsObserver())
    except ImportError as err:
        raise NotImplementedError(
            "watchdog's FSEvents backend is unavailable; "
            "it ships only on macOS, where watchdog provides the extension"
        ) from err


@asynccontextmanager
async def _fsevents_events(
    observer: _Observer, directories: tuple[Path, ...]
) -> AsyncGenerator[AsyncIterator[set[Path]]]:
    """Adapt a watchdog observer to the changed-path iterator this module yields.

    Watchdog dispatches on its own thread, so every event is handed to the
    loop through ``call_soon_threadsafe``; the queue is what the caller awaits.
    A rename reports both ends, and both matter -- a compaction lands as a
    rename INTO the directory, so a watcher that only saw the source would
    miss the new transcript.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    def deliver(event: object) -> None:
        if getattr(event, "is_directory", False):
            return
        for attribute in ("src_path", "dest_path"):
            raw = getattr(event, attribute, "")
            if raw:
                loop.call_soon_threadsafe(queue.put_nowait, Path(os.fsdecode(raw)))

    handler = _FsEventsHandler(deliver)
    for directory in directories:
        observer.schedule(handler, str(directory), recursive=True)
    observer.start()
    try:
        yield _drain_queue(queue)
    finally:
        observer.stop()
        await asyncio.to_thread(observer.join, _OBSERVER_JOIN_SEC)


class _FsEventsHandler:
    """Watchdog's handler protocol, narrowed to the one method it calls."""

    def __init__(self, deliver: Callable[[object], None]) -> None:
        self._deliver = deliver

    def dispatch(self, event: object) -> None:
        """Watchdog's entry point; forwards to :meth:`on_any_event`."""
        self.on_any_event(event)

    def on_any_event(self, event: object) -> None:
        """Hand one filesystem event to the loop."""
        self._deliver(event)


async def _drain_queue(queue: asyncio.Queue[Path]) -> AsyncIterator[set[Path]]:
    """Yield every path queued since the last wake, batching a burst into one."""
    while True:
        first = await queue.get()
        changed = {first}
        while True:
            try:
                changed.add(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        yield changed


def _inotify_fd(*directories: Path) -> tuple[int, dict[int, Path]]:
    """Open one inotify fd watching every directory's subtree.

    Returns:
      fd: The inotify descriptor.
      watches: Watch descriptor to the directory it names, which the reader
        needs to rebuild a path -- an event carries a number, never a path.

    Raises:
      FileNotFoundError: A directory does not exist.
      OSError: The kernel refused a watch (descriptor or watch limit).

    """
    libc = _libc()
    fd = int(libc.inotify_init1(os.O_NONBLOCK))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    watches: dict[int, Path] = {}
    try:
        # No existence pre-check: the kernel reports ENOENT and ``_add_watch``
        # turns it into the same FileNotFoundError, so a check here would be a
        # second guard for one fault, free to drift from the first.
        for directory in directories:
            _watch_tree(libc, fd, directory, watches)
    except BaseException:
        # Every watch dies with the descriptor, so no per-watch unwind is
        # needed -- but a partial failure must not leak the fd itself.
        os.close(fd)
        raise
    return fd, watches


def _watch_tree(
    libc: ctypes.CDLL, fd: int, directory: Path, watches: dict[int, Path]
) -> None:
    """Watch ``directory`` and every subdirectory below it.

    The ROOT's failure propagates -- it is what the caller asked for, and a
    watch silently covering nothing is the fault this module exists to rule
    out. A DESCENDANT's does not: ``rglob`` lists the tree and the watches are
    added after, so anything short-lived under it is already gone by its turn.
    Failing the whole call there would close the descriptor and lose the root
    watch too, disabling capture over a directory nobody needed.
    """
    _add_watch(libc, fd, directory, watches)
    for path in directory.rglob("*"):
        if not path.is_dir():
            continue
        try:
            _add_watch(libc, fd, path, watches)
        except FileNotFoundError:
            continue
        except OSError:
            # Not a race: the kernel refused (typically ``max_user_watches``).
            # The subtree is uncovered for the rest of the run and only an
            # operator can fix it, so say so rather than continue silently.
            _logger.warning("inotify refused a watch on %s", path, exc_info=True)


def _add_watch(
    libc: ctypes.CDLL, fd: int, directory: Path, watches: dict[int, Path]
) -> None:
    """Register one directory; record which watch descriptor names it.

    Raises:
      FileNotFoundError: The directory vanished before it could be watched.
      OSError: The kernel refused the watch.

    """
    descriptor = int(libc.inotify_add_watch(fd, str(directory).encode(), _WATCH_MASK))
    if descriptor < 0:
        code = ctypes.get_errno()
        if code == errno.ENOENT:
            raise FileNotFoundError(code, "no such directory", str(directory))
        raise OSError(code, "inotify_add_watch failed", str(directory))
    watches[descriptor] = directory


async def _inotify_events(
    fd: int, watches: dict[int, Path]
) -> AsyncIterator[set[Path]]:
    """Yield the set of changed paths each time the kernel reports one."""
    libc = _libc()
    loop = asyncio.get_running_loop()
    while True:
        ready = loop.create_future()
        loop.add_reader(fd, _resolve, ready)
        try:
            await ready
        finally:
            loop.remove_reader(fd)
        # One read drains every event queued so far, so a burst of writes
        # yields one wake carrying all of them.
        changed = _read_inotify(libc, fd, watches)
        if changed:
            yield changed


def _resolve(ready: asyncio.Future[None]) -> None:
    """Complete ``ready`` once, ignoring a second reader callback."""
    if not ready.done():
        ready.set_result(None)


def _read_inotify(libc: ctypes.CDLL, fd: int, watches: dict[int, Path]) -> set[Path]:
    """Read every queued event; return the paths they name."""
    try:
        raw = os.read(fd, 64 * 1024)
    except BlockingIOError:
        return set()
    return _read_events(raw, libc, fd, watches)


def _read_events(
    raw: bytes, libc: ctypes.CDLL, fd: int, watches: dict[int, Path]
) -> set[Path]:
    """Decode one read's worth of ``struct inotify_event``s into paths.

    A new subdirectory is watched as it is seen, and then LISTED: a file
    written into it before that watch existed is named by no later event, so
    the listing is the only thing that can report it (``inotify(7)``).
    """
    changed: set[Path] = set()
    offset = 0
    while offset < len(raw):
        descriptor, mask, _cookie, length = _EVENT_HEADER.unpack_from(raw, offset)
        offset += _EVENT_HEADER.size
        name = raw[offset : offset + length].split(b"\0", 1)[0]
        offset += length
        if mask & _IN_Q_OVERFLOW:
            # Events were discarded and are named by nothing that follows. Fall
            # back to listing what is watched, which is the only way anything
            # dropped can still be seen. Checked BEFORE the descriptor lookup:
            # an overflow carries ``wd = -1``, so the unknown-watch skip below
            # would swallow it.
            _logger.warning("inotify queue overflowed; rescanning watched trees")
            changed |= _rescan(watches)
            continue
        if mask & _IN_IGNORED:
            # The kernel may hand this number to a later directory.
            _ = watches.pop(descriptor, None)
            continue
        parent = watches.get(descriptor)
        if parent is None or not name:
            continue
        path = parent / os.fsdecode(name)
        if mask & _IN_ISDIR:
            changed |= _adopt(libc, fd, path, watches)
            continue
        changed.add(path)
    return changed


def _rescan(watches: dict[int, Path]) -> set[Path]:
    """Every file directly under a watched directory.

    Non-recursive on purpose: each subdirectory of a watched tree carries its
    own watch, so it is already in ``watches`` and listed in its own right.
    """
    found: set[Path] = set()
    for directory in watches.values():
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        found |= {path for path in entries if path.is_file()}
    return found


def _adopt(
    libc: ctypes.CDLL, fd: int, directory: Path, watches: dict[int, Path]
) -> set[Path]:
    """Watch a directory that just appeared; report what it already holds.

    The listing is not belt-and-braces: between the directory's creation and
    this watch, a writer may already have filled it, and those files are named
    by no event the kernel will ever send.
    """
    try:
        _watch_tree(libc, fd, directory, watches)
    except FileNotFoundError:
        # It vanished as fast as it appeared; nothing under it to report.
        return set()
    except OSError:
        # The kernel refused (typically ``max_user_watches``). Unlike a
        # vanished directory this one still exists and will keep receiving
        # writes nobody watches, for the life of the run.
        _logger.warning("inotify refused a watch on %s", directory, exc_info=True)
        return set()
    found: set[Path] = set()
    for path in directory.rglob("*"):
        if path.is_file():
            found.add(path)
    return found


def _libc() -> ctypes.CDLL:
    """Return libc with inotify bound, raising when it is unavailable.

    Raises:
      NotImplementedError: libc has no inotify (a non-Linux kernel).

    """
    name = ctypes.util.find_library("c")
    libc = ctypes.CDLL(name, use_errno=True)
    if not hasattr(libc, "inotify_init1"):
        raise NotImplementedError("libc has no inotify")
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_init1.restype = ctypes.c_int
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.inotify_add_watch.restype = ctypes.c_int
    return libc
