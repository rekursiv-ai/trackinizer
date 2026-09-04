"""Drive a streaming normalizer from a runner that PUSHES.

``normalize`` pulls: it is a generator over a text stream, which is what lets
one implementation serve both a finished file and a session still being written
(axiom 11). The runner is the other way round -- a filesystem watch hands it
one line at a time, with nothing to pull from.

:class:`Tail` is that turn, and the turn costs a thread. It cannot be done by
handing the generator a stream that reports EOF between lines: a generator whose
``for line in stream`` loop ends is FINISHED, so the next pushed line would find
nothing left to resume. Re-reading the accumulated text per line is the other
non-threaded option and is quadratic -- 10k lines of a captured session become
50M line-parses.

So the generator runs on its own thread and blocks for its next line, and
:meth:`Tail.feed` hands one over and collects what it produced. There is no
second reader here and no second parse: the adapter's own ``normalize`` is what
runs, so a dialect fix reaches capture and conversion at once.

Typed to :data:`TraxRecord` rather than the shared IR, because this drives
EVERY adapter: the CLI dialects yield the shared members, and the scrape adds
the three stream records only it can emit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from io import StringIO
from typing import Final, TextIO, override

import queue
import threading

from trackinizer.lib.agent.types.sessions import TurnContext
from trackinizer.types.streams import TraxRecord


__all__ = ["Tail"]


type Normalize = Callable[[TextIO], Iterator[TraxRecord]]


class _Signal:
    """A marker passed between the two threads; never a record."""

    __slots__ = ()


_WANTS_A_LINE: Final = _Signal()
"""The reader has finished the line it was given and is asking for the next."""

_ENDED: Final = _Signal()
"""The reader's generator returned: the stream is over and nothing follows."""

_NO_MORE_LINES: Final = _Signal()
"""There will be no further lines, so the reader may finish."""


class _Failed(_Signal):
    """The reader raised; the exception travels back to whoever fed the chunk."""

    __slots__ = ("error",)

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error


class Tail:
    """One file's reader, fed a line at a time.

    Stateful and single-threaded FROM THE CALLER's side: one instance per FILE,
    fed in order. A session spans several files (claude splits on compaction,
    codex forks) and sharing one would number the second file's records after
    the first's.
    """

    def __init__(self, normalize: Normalize, *, whole_file: bool = False) -> None:
        self._normalize = normalize
        self._whole_file = whole_file
        self._encoding: dict[str, object] = {}
        self._lines: queue.SimpleQueue[str | _Signal] = queue.SimpleQueue()
        self._produced: queue.SimpleQueue[TraxRecord | _Signal] = queue.SimpleQueue()
        self._reader: threading.Thread | None = None
        self._ended = False

    def feed(self, text: str) -> list[TraxRecord]:
        """Consume one pushed chunk; return the records it produced.

        A LIST, not a generator. The caller emits each record as it arrives and
        an emit can raise -- a full disk, a server that went away -- and a
        generator abandoned mid-chunk leaves its reader suspended on a queue
        nobody drains, so every later chunk of that file reads as empty. One
        chunk is one line, so the list is a line's worth of records.

        Args:
          text: One native line for an append-only CLI, or the whole document
            for one that rewrites in place.

        Returns:
          records: What this chunk produced, in stream order.

        Raises:
          Exception: Whatever the adapter's reader raised on this chunk. Which
            exception is the adapter's business; the runner guards the call,
            and swallowing it here would report a malformed line as an empty
            one.

        """
        if self._whole_file:
            # Rewritten in place, so the bytes are the WHOLE session again
            # rather than a continuation. A reader partway through the previous
            # document cannot resume into a different one -- and the runner
            # marks such a chunk a restart, so each record lands back on the
            # position it already held.
            return [
                self._remembered(record) for record in self._normalize(StringIO(text))
            ]
        if self._ended:
            return []
        self._start()
        self._lines.put(text)
        return self._collect()

    def close(self) -> list[TraxRecord]:
        """Tell the reader no more lines are coming; return what only EOF says.

        Whether the file ended on a newline is knowable nowhere else, and it is
        what a byte-exact rewrite needs. Idempotent.
        """
        if self._whole_file or self._reader is None or self._ended:
            return []
        self._lines.put(_NO_MORE_LINES)
        return self._collect()

    @property
    def encoding(self) -> dict[str, object]:
        """How the file spells its bytes, for the prefix consumed so far.

        Correct for what has been read, never final: claude's ascii-escaping
        convention is a MAJORITY over the lines seen, so it moves as the file
        grows and the reader restates it. The last statement is the one in
        force, which is why this is re-read per batch rather than captured
        once.
        """
        return dict(self._encoding)

    def _restart(self) -> None:
        """Drop the dead reader and the queues it shared, ready to rebuild.

        Fresh QUEUES, not merely a fresh thread: the dead reader posted an
        ``_ENDED`` behind its failure, and reusing the queue would hand that
        to the next chunk as its answer.
        """
        self._reader = None
        self._lines = queue.SimpleQueue()
        self._produced = queue.SimpleQueue()

    def _start(self) -> None:
        """Put the reader on its own thread, on first use."""
        if self._reader is not None:
            return
        # A DAEMON: a caller that abandons a tail mid-session leaves the reader
        # blocked for a line that never comes, and a non-daemon thread there
        # would hold the whole process open at exit.
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        """Run the adapter's generator, posting each record as it lands."""
        try:
            for record in self._normalize(_Pulled(self._lines, self._produced)):
                self._produced.put(record)
        except Exception as err:  # noqa: BLE001 -- an adapter may raise anything.
            # Handed BACK rather than escaping: this runs on the reader's own
            # thread, where a raise would be swallowed by the interpreter and
            # the caller of the offending chunk would see an empty line instead
            # of the failure. Re-raised in ``_collect``, so the runner's own
            # guard reports it against the chunk that caused it.
            self._produced.put(_Failed(err))
        finally:
            self._produced.put(_ENDED)

    def _collect(self) -> list[TraxRecord]:
        """Take records until the reader asks for another line or finishes.

        Raises:
          Exception: Whatever the reader raised, re-raised on the thread that
            fed the chunk.

        """
        out: list[TraxRecord] = []
        while True:
            item = self._produced.get()
            if item is _WANTS_A_LINE:
                return out
            if isinstance(item, _Failed):
                # A raise ENDS a generator -- it cannot be resumed -- so the
                # reader is rebuilt rather than mourned: one bad chunk costs
                # one chunk and the file goes on being captured. What is lost
                # is the state the dead reader held (claude's escaping majority
                # is counted over the lines IT saw), which is the survivable
                # direction: a moved majority respells some lines, a dead
                # reader silently ends capture. No real adapter reaches this --
                # normalization is total, and a malformed line is an
                # ``IncompleteRecord`` -- so it answers a broken one.
                self._restart()
                raise item.error
            if item is _ENDED:
                self._ended = True
                return out
            assert not isinstance(item, _Signal)
            out.append(self._remembered(item))

    def _remembered(self, record: TraxRecord) -> TraxRecord:
        """Keep the whole-file properties a rewrite needs, as they are stated."""
        if isinstance(record, TurnContext) and record.encoding:
            self._encoding = dict(record.encoding)
        return record


class _Pulled(StringIO):
    """The stream a reader iterates, answered one pushed line at a time.

    The handoff is what makes ``feed`` exact: the reader has finished the line
    it was given precisely when it asks for the NEXT one, so it announces the
    ask and then blocks. Everything posted between two asks is what that one
    line produced.
    """

    def __init__(
        self,
        lines: queue.SimpleQueue[str | _Signal],
        produced: queue.SimpleQueue[TraxRecord | _Signal],
    ) -> None:
        super().__init__()
        self._lines = lines
        self._produced = produced
        self._asked = False

    @override
    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    @override
    def __iter__(self) -> _Pulled:
        return self

    @override
    def readline(self, size: int | None = -1, /) -> str:
        del size
        if self._asked:
            # Announced only from the SECOND ask onwards: the first happens
            # before any line was handed over, and announcing it there would
            # end the caller's first collect before its own line was read.
            self._produced.put(_WANTS_A_LINE)
        self._asked = True
        line = self._lines.get()
        return "" if isinstance(line, _Signal) else line

    @override
    def read(self, size: int | None = -1, /) -> str:
        del size
        # A reader that wants the whole document rather than lines -- gemini's
        # -- never runs on this stream: it is driven whole-file instead.
        raise NotImplementedError("a pushed stream is read by lines, never whole")
