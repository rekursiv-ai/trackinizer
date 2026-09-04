"""The IO-stream contract: wrap any binary, capture its lines verbatim.

Registry key ``sh``: ``trax run --as alice sh -- CMD [ARGS...]``. The wrapped
command has no session log to tail, so the process's own IO is the source of
truth: each completed line becomes one record. Semantic parsing belongs to the
wrapped script, not trax -- the contract is line-delimited UTF-8 text in both
directions (stdin lines arrive via the inbound poller's injection; output lines
are captured verbatim).

Piped by default, unlike every other adapter. A pipe run keeps the child's
three descriptors apart, so a line remembers which stream it crossed -- and
that distinction is the only structure a scrape has. The cost is the tty:
``--capture pty`` buys one back (liveness, and a child that wants a terminal)
at the price of ``Stderr``, which the kernel has already merged into the output
by the time trax sees a byte. See :data:`Capture`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final

import logging
import re

from trackinizer.trax.run.adapters import scrape
from trackinizer.trax.run.adapters.custom_types import Capture
from trackinizer.trax.run.adapters.tail import Tail


__all__ = ["IOStreamAdapter", "LineCapture"]


# Terminal escape sequences a child may emit, stripped so captured lines are
# the text, not the rendering: CSI (colors, cursor movement), OSC (title-set,
# ``\x1b]0;...\x07`` or ST-terminated), and DCS/APC/PM string sequences.
_ANSI_ESCAPES: Final = re.compile(
    rb"\x1b\[[0-9;?]*[a-zA-Z]"  # CSI
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, BEL- or ST-terminated
    rb"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS/SOS/PM/APC, ST-terminated
)

# One captured line's byte cap. Enforced at INGEST (``LineCapture.feed``),
# not only at line emit: the cap exists for a child that never emits a
# newline at all (a progress bar rewriting itself, a binary blob), and a
# clamp that waits for the newline never fires for exactly that child.
_MAX_LINE_BYTES: Final = 16_384


class IOStreamAdapter:
    """Wrap an arbitrary binary; its own IO streams are the session source.

    Every file-tailing method is vacuous -- there is no log. The runner
    detects the empty :attr:`cli_binary`, takes the command from the ``--``
    args, and attaches a :class:`LineCapture` per stream that feeds each
    completed line to this adapter's normalizer.

    The READER is the configurable seam: a new stream dialect (say JSON-lines
    into richer records) is this class returning a different one, with its own
    ``name`` in the runner's adapter table. The framing (chunk buffering, the
    line-length clamp) stays in :class:`LineCapture`, shared by every stream
    adapter.
    """

    name: str = "sh"
    cli_binary: str = ""
    whole_file: bool = False
    stream_source: bool = True
    capture: Capture = "pipe"

    def session_dirs(self) -> Iterable[Path]:
        return ()

    def matches_session_file(self, path: Path) -> bool:
        del path
        return False

    def session_scope(self) -> Path | None:
        # The capture source is the child's own IO; no session file to scope.
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def reader(self) -> Tail:
        """A fresh IR reader for one captured stream.

        Every line becomes a stream record: the contract is verbatim
        line-delimited text, so nothing is parsed and nothing can be
        misparsed. Semantic structure belongs to the wrapped script; WHICH
        stream carried the line is the one thing capture knows and the file
        does not, which is why the runner tags it rather than this reader.
        """
        return Tail(scrape.normalize)


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
        """Buffer ``chunk``, delivering each completed line."""
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

    def _emit_line(self, raw: bytes, *, terminated: bool) -> None:
        """Clamp one framed line and hand it to the consumer.

        The terminator is re-attached AFTER clamping, so a truncation marker
        never lands past the newline and the line stays one line.
        """
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
                "stream capture: dropping unparseable line", exc_info=True
            )
