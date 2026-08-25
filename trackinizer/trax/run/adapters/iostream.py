"""The IO-stream contract: wrap any binary, capture its lines verbatim.

Registry key ``sh``: ``trax run --as alice sh -- CMD [ARGS...]``. The wrapped
command has no session log to tail, so the process's own PTY stream is the
source of truth: each completed output line becomes one
:class:`AssistantMessage` event. Semantic parsing belongs to the wrapped
script, not trax -- the contract is line-delimited UTF-8 text in both
directions (stdin lines arrive via the inbound poller's injection; stdout
lines are captured verbatim).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import logging
import re

from trackinizer.trax.run.adapters.base import Event
from trackinizer.types.agent_session_events import AssistantMessage


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
    """Wrap an arbitrary binary; the PTY stream is the session source.

    Every file-tailing method is vacuous -- there is no log. The runner
    detects the empty :attr:`cli_binary`, takes the command from the ``--``
    args, and attaches a :class:`LineCapture` that feeds each completed
    output line to :meth:`parse`.

    ``parse`` IS the configurable seam: a new stream adapter is this class
    with a different ``parse`` (say, JSON-lines into richer typed events)
    and its own ``name`` registered in the runner's adapter table. The
    framing (chunk buffering, the line-length clamp) stays in
    :class:`LineCapture`, shared by every stream adapter.
    """

    name: str = "sh"
    cli_binary: str = ""
    whole_file: bool = False
    stream_source: bool = True

    def session_dirs(self) -> Iterable[Path]:
        return ()

    def matches_session_file(self, path: Path) -> bool:
        del path
        return False

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        r"""One completed output line -> events. The verbatim-text contract.

        Strips terminal escape sequences (CSI/OSC/DCS) and the PTY's ``\r``
        so the captured text is what the child printed, not how a terminal
        rendered it. A blank line yields nothing.
        """
        del whole_file  # stream framing is always line-oriented.
        text = _ANSI_ESCAPES.sub(b"", raw).rstrip(b"\r").decode(errors="replace")
        if not text:
            return ()
        return (
            Event(
                message=AssistantMessage(text=text),
                timestamp=datetime.now(UTC),
            ),
        )


class LineCapture:
    r"""Frame PTY output bytes into lines; parse each through the adapter.

    Fed raw master-fd chunks by the pump (which owns no framing); buffers
    across chunk boundaries and hands each completed ``\n``-terminated line
    (clamped to :data:`_MAX_LINE_BYTES`) to ``parse`` -- the stream
    adapter's :meth:`~IOStreamAdapter.parse` -- emitting every event it
    yields. ``close`` flushes an unterminated tail so a child that exits
    mid-line still gets its last words captured.

    Two hazards this class must contain, because ``feed`` runs on the pump's
    IO thread where an escape terminates the whole run:

    - Memory: the byte bound applies at ingest, not only at line emit -- a
      child that never prints a newline (a progress bar rewriting itself)
      would otherwise grow the buffer without limit while the emit-side
      clamp never fires. Past the cap, bytes are DROPPED until the next
      newline; the truncation is marked on the emitted line.
    - Exceptions: ``parse``/``emit`` failures are logged and the line
      skipped, mirroring the file drain's ``_process_chunk`` resilience --
      one malformed line must not kill a live interactive session.
    """

    def __init__(
        self,
        parse: Callable[[bytes], Iterable[Event]],
        emit: Callable[[Event], None],
    ) -> None:
        self._parse = parse
        self._emit = emit
        self._buffer = bytearray()
        # Bytes discarded from the CURRENT (unterminated) line once the
        # buffer hit the cap; > 0 marks the line truncated when it emits.
        self._dropped = 0

    def feed(self, chunk: bytes) -> None:
        """Buffer ``chunk``, parsing and emitting per completed line."""
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
            self._emit_line(line)

    def close(self) -> None:
        """Flush a trailing unterminated line, if any."""
        if self._buffer:
            self._emit_line(bytes(self._buffer))
            self._buffer.clear()

    def _emit_line(self, raw: bytes) -> None:
        """Clamp one framed line and emit whatever the adapter parses from it."""
        truncated = self._dropped > 0
        self._dropped = 0
        if len(raw) > _MAX_LINE_BYTES:
            raw = raw[:_MAX_LINE_BYTES]
            truncated = True
        if truncated:
            raw += b"... (truncated)"
        # Guarded like the file drain's ``_process_chunk``: this runs on the
        # pump's IO thread, so an escaping parse/emit error would unwind the
        # pump and terminate the live run over one bad line.
        try:
            for event in self._parse(raw):
                self._emit(event)
        except Exception:
            logging.getLogger(__name__).warning(
                "stream capture: dropping unparseable line", exc_info=True
            )
