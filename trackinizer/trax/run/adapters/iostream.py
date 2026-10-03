"""The IO-stream contract: wrap any binary, capture its lines verbatim.

Registry key ``sh``: ``trax run --as alice sh -- CMD [ARGS...]``. The wrapped
command has no session log to tail, so the process's own IO is the source of
truth: each completed line becomes one record. Semantic parsing belongs to the
wrapped script, not trax -- the contract is line-delimited UTF-8 text in both
directions (stdin lines arrive via the inbound poller's injection; output lines
are captured verbatim).

The child runs on a pty, like every other adapter, so every captured line is a
``Stdout``: the kernel merges stderr into the one terminal before trax sees a
byte. Pipes would keep the streams apart, and were measured against it: on a
child printing three lines then one to stderr, the pty delivered each line as
printed (0.01s, 0.31s, 0.61s) while the pipes delivered nothing until the
child exited at 0.91s and flushed its block buffer -- and a child killed first
loses what it held. Liveness wins, and the pty is also what lets a
``--detach`` host serve the child to a viewer. A command that needs stderr
apart redirects it itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import logging

from trackinizer.trax.run.adapters import scrape
from trackinizer.trax.run.adapters.tail import Tail


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path


__all__ = ["IOStreamAdapter", "LineCapture"]


# One captured line's byte cap. Enforced at INGEST (``LineCapture.feed``),
# not only at line emit: the cap exists for a child that never emits a
# newline at all (a progress bar rewriting itself, a binary blob), and a
# clamp that waits for the newline never fires for exactly that child.
_MAX_LINE_BYTES: Final = 16_384


class IOStreamAdapter:
    """Wrap an arbitrary binary; its own IO streams are the session source.

    Every file-tailing method is vacuous -- there is no log. The runner
    detects the empty :attr:`cli_binary`, takes the command from the ``--``
    args, and frames the pty's output with a :class:`LineCapture` that feeds
    each completed line to this adapter's normalizer.

    The READER is the configurable seam: a new stream dialect (say JSON-lines
    into richer records) is this class returning a different one, with its own
    ``name`` in the runner's adapter table. The framing (chunk buffering, the
    line-length clamp) stays in :class:`LineCapture`, shared by every stream
    adapter.
    """

    name: str = "sh"
    cli_binary: str = ""
    whole_file: bool = False
    parent_session_env: frozenset[str] = frozenset[str]()
    stream_source: bool = True

    def session_dirs(self) -> Iterable[Path]:
        """Return the directories this CLI writes sessions under."""
        return ()

    def matches_session_file(self, path: Path) -> bool:
        """Return whether ``path`` is one of this CLI's session files."""
        del path
        return False

    def session_scope(self) -> Path | None:
        """Return the scope key a session file is filed under.

        Returns:
          result: The Path | None.

        """
        # The capture source is the child's own IO; no session file to scope.
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        """Return the session id encoded in ``path``.

        Args:
          path: File path to extract session ID from.

        Returns:
          result: Session ID string, or None if not present in this adapter.

        """
        del path
        return None

    def reader(self) -> Tail:
        """Return a fresh IR reader for one captured stream.

        Every line becomes a stream record: the contract is verbatim
        line-delimited text, so nothing is parsed and nothing can be
        misparsed. Semantic structure belongs to the wrapped script; WHICH
        stream carried the line is the one thing capture knows and the file
        does not, which is why the runner tags it rather than this reader.

        Returns:
          result: Tail reader consuming line-delimited plaintext.

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
