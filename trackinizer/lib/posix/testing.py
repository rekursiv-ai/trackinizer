"""Test support for followers: an FSEvents that reports on the test's clock.

FSEvents reports a change when ``fseventsd`` gets to it, not when it happens.
Under load a new file was measured reported 0.1s to 4.6s after the write, and
sometimes not within 10s; ``FSEventStreamFlushSync`` returned at once without
hurrying it. A unit test that waits on it therefore passes or fails with the
machine's load. :func:`poll_fsevents` puts a polling watch in its place that
keeps the FSEvents contract :mod:`trackinizer.lib.posix.follow` relies on: whole
trees are watched, and paths arrive resolved, as FSEvents sends them. Linux
needs no stand-in: inotify queues each event as part of the write.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import os
import platform
import threading

from trackinizer.lib.posix import follow


if TYPE_CHECKING:
    import pytest


def poll_fsevents(monkeypatch: pytest.MonkeyPatch, *, writes: bool) -> None:
    """On macOS, watch by polling every few milliseconds instead of FSEvents.

    Args:
      monkeypatch: Installs the stand-in, and removes it at teardown.
      writes: Whether a write to an existing file is reported, or only files
        appearing, vanishing, and being replaced. FSEvents was measured holding
        an append until its writer closed, which is why the line followers arm
        kqueue on every file they follow; ``False`` keeps that dependence
        under test rather than letting a polled write stand in for kqueue.

    """
    if platform.system() == "Darwin":
        monkeypatch.setattr(
            follow,
            "_fsevents_observer",
            partial(_PolledFsEvents, writes=writes),
        )


@runtime_checkable
class _Handler(Protocol):
    """The one method an observer calls on the follower's event handler."""

    def dispatch(self, event: object) -> None:
        """Handle one filesystem event."""
        ...


# Not watchdog's PollingObserver: watchdog is installed on macOS only, so importing it
# here fails the Linux collection of every test module that imports this one.
class _PolledFsEvents:
    """An observer that polls its trees and reports what FSEvents would."""

    def __init__(self, *, writes: bool) -> None:
        self._writes = writes
        self._watches: list[tuple[_Handler, Path]] = []
        self._before: list[dict[Path, tuple[int, int, int]]] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def schedule(self, handler: object, path: str, *, recursive: bool) -> None:
        """Watch the tree under ``path``, reporting its paths resolved.

        Resolved because FSEvents reports physical paths, and the follower's
        root map cuts each one at the depth of the RESOLVED root: a report
        spelled through a symlink of another depth would be dropped.

        Args:
          handler: Receives one event per changed file.
          path: Root of the tree to watch.
          recursive: Ignored: the whole tree is watched, as FSEvents watches
            it, and the follower always asks for that.

        """
        del recursive
        assert isinstance(handler, _Handler)
        self._watches.append((handler, Path(path).resolve()))

    def start(self) -> None:
        """Take the first snapshot before returning, so the watch is armed."""
        self._before = [_stamps(root) for _, root in self._watches]
        self._thread.start()

    def stop(self) -> None:
        """Ask the polling thread to finish."""
        self._stopped.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the polling thread to exit."""
        self._thread.join(timeout)

    def _poll(self) -> None:
        while not self._stopped.wait(0.005):
            for index, (handler, root) in enumerate(self._watches):
                after = _stamps(root)
                for path in sorted(
                    _changed(self._before[index], after, writes=self._writes),
                ):
                    handler.dispatch(_Change(src_path=str(path)))
                self._before[index] = after


@dataclass(frozen=True, slots=True, kw_only=True)
class _Change:
    """The one attribute the follower's handler needs from an event."""

    src_path: str


def _stamps(root: Path) -> dict[Path, tuple[int, int, int]]:
    """Map every file under ``root`` to its inode, mtime, and size."""
    stamps: dict[Path, tuple[int, int, int]] = {}
    for directory, _subdirectories, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue  # Deleted between the listing and the stat.
            stamps[path] = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    return stamps


def _changed(
    before: dict[Path, tuple[int, int, int]],
    after: dict[Path, tuple[int, int, int]],
    *,
    writes: bool,
) -> set[Path]:
    """Files that appeared, vanished, or were replaced; with ``writes``, written."""
    replaced = {
        path
        for path in after
        if path not in before or before[path][0] != after[path][0]
    }
    written = (
        {path for path in after if path in before and before[path] != after[path]}
        if writes
        else set[Path]()
    )
    return replaced | written | (before.keys() - after.keys())
