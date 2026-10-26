"""Frame a pty's output into lines, and read captured lines as stream records.

A terminal child's bytes arrive in arbitrary chunks. :class:`LineCapture` cuts them into
whole lines, and :func:`normalize` reads those lines as the records a ``sh`` session
holds -- the reading the agent runner applies to a session's output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import logging

from trackinizer.sagent.session.types import ContextClear, TurnContext
from trackinizer.types.streams import Stdout, TraxRecord


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import TextIO


__all__ = ["LineCapture", "normalize"]


# One captured line's byte cap. Enforced at INGEST (``LineCapture.feed``),
# not only at line emit: the cap exists for a child that never emits a
# newline at all (a progress bar rewriting itself, a binary blob), and a
# clamp that waits for the newline never fires for exactly that child.
_MAX_LINE_BYTES: Final = 16_384


def normalize(stream: TextIO) -> Iterator[TraxRecord]:
    """Normalize a captured stream into its records, one line at a time.

    Yields as it reads, so a caller tailing a live capture sees each line's
    record when the line lands rather than when the stream ends (axiom 11:
    neither side is held). Draining the iterator is what a whole-file read is.

    Args:
      stream: Captured text stream.

    Yields:
      record: Each record one captured line produced, in stream order. Every
        line is a :class:`Stdout`: a file records no descriptor, so the stream
        a line crossed is knowable only at capture.

    """
    total = 0
    ends_newline = True
    for line in stream:
        if not total:
            # Settings before the acts they govern. A scrape declares nothing
            # -- no CLI wrote these bytes -- so the context states only what
            # the file itself shows, and the clear names the empty context a
            # scrape genuinely begins from.
            yield TurnContext(encoding={"newline_terminated": True})
            yield ContextClear(extra={"$opens": True})
        total += 1
        ends_newline = line.endswith("\n")
        yield Stdout(text=line)
    if total and not ends_newline:
        # The last line lost its newline, which is knowable only here. Restated
        # rather than mutated: a record already yielded is the caller's, and a
        # later state record superseding an earlier one is how a stream says so.
        yield TurnContext(encoding={"newline_terminated": False})


class LineCapture:
    r"""Frame PTY output bytes into lines and hand each to one consumer.

    Fed raw master-fd chunks by the pump (which owns no framing); buffers
    across chunk boundaries and delivers each completed line WITH its ``\n``
    (clamped to :data:`_MAX_LINE_BYTES`). ``close`` flushes an unterminated
    tail so a child that exits mid-line still gets its last words captured --
    without a newline, since there was none.

    The terminator is kept because the reader downstream is total: every line
    becomes one record and the records concatenate back to the bytes read, so
    a stripped ``\n`` would make a scrape rewrite one byte short per line and
    report the capture as unterminated when it was not.

    Two hazards this class must contain, because ``feed`` runs on the pump's
    IO thread where an escape terminates the whole run:

    - Memory: the byte bound applies at ingest, not only at line emit -- a
      child that never prints a newline (a progress bar rewriting itself)
      would otherwise grow the buffer without limit while the emit-side
      clamp never fires. Past the cap, bytes are DROPPED until the next
      newline; the truncation is marked on the emitted line.
    - Exceptions: a consumer failure is logged and the line skipped,
      mirroring the file drain's ``_process_chunk`` resilience -- one bad
      line must not kill a live interactive session.
    """

    def __init__(self, emit: Callable[[bytes], None]) -> None:
        self._emit = emit
        self._buffer = bytearray()
        # Bytes discarded from the CURRENT (unterminated) line once the
        # buffer hit the cap; > 0 marks the line truncated when it emits.
        self._dropped = 0

    def feed(self, chunk: bytes) -> None:
        """Buffer ``chunk``, delivering each completed line.

        Args:
          chunk: Bytes to append to buffer (delivers complete lines).

        """
        self._buffer.extend(chunk)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                # No newline: keep at most the cap; drop the excess now so a
                # newline-free child cannot grow memory without bound.
                if len(self._buffer) > _MAX_LINE_BYTES:
                    self._dropped += len(self._buffer) - _MAX_LINE_BYTES
                    del self._buffer[_MAX_LINE_BYTES:]
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            self._emit_line(line, terminated=True)

    def close(self) -> None:
        """Flush a trailing unterminated line, if any."""
        if self._buffer:
            self._emit_line(bytes(self._buffer), terminated=False)
            self._buffer.clear()

    # The terminator is re-attached AFTER clamping, so a truncation marker never lands
    # past the newline and the line stays one line.
    def _emit_line(self, raw: bytes, *, terminated: bool) -> None:
        """Clamp one framed line and hand it to the consumer."""
        truncated = self._dropped > 0
        self._dropped = 0
        if len(raw) > _MAX_LINE_BYTES:
            raw = raw[:_MAX_LINE_BYTES]
            truncated = True
        if truncated:
            raw += b"... (truncated)"
        if terminated:
            raw += b"\n"
        # Guarded like the file drain's ``_process_chunk``: this runs on the
        # pump's IO thread, so an escaping consumer error would unwind the
        # pump and terminate the live run over one bad line.
        try:
            self._emit(raw)
        except Exception:
            logging.getLogger(__name__).warning(
                "stream capture: dropping unparseable line",
                exc_info=True,
            )
