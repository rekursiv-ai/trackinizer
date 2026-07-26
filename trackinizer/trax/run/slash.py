"""Detect CLI slash-commands in the human's raw PTY keystrokes.

A slash-command (``/exit``, ``/model gpt-5``) is handled inside the CLI's TUI
and never written to its rollout/session log, so the log tailer cannot see it
(``docs/design_session_messaging.md``, "Known gaps"). The pump already owns the
master fd and tees the human's stdin; this consumer reassembles typed lines
from that raw byte stream and emits a :class:`SlashCommand` when the human
submits one beginning with ``/``.

Best-effort by nature: raw-mode stdin carries control bytes (arrows, history
recall, paste), so the detector handles the common editing keys (backspace,
Ctrl-U line-kill, Ctrl-W word-erase, Enter, Ctrl-C) and skips ANSI escape
sequences (arrow keys, function keys, bracketed-paste markers) rather than
appending their printable tail as text. A command recalled from history with
arrow keys is dropped (the escape is consumed, the recalled text never reaches
the detector) rather than mis-recorded; a miss costs only an un-logged command,
never corrupts capture.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import logging

from trackinizer.types.agent_session_events import SlashCommand


_logger = logging.getLogger(__name__)


# Raw-mode control bytes the line accumulator interprets; everything else
# printable (>= 0x20, outside an escape sequence) is appended as line text.
_ENTER = frozenset((0x0D, 0x0A))  # CR submits; LF too, except inside a paste.
_BACKSPACE = frozenset((0x7F, 0x08))  # DEL and BS both erase one char.
_WORD_ERASE = 0x17  # config-globals: ignore -- terminal control byte (Ctrl-W)
_CLEAR_LINE = frozenset((0x15, 0x03))  # Ctrl-U (line-kill) / Ctrl-C (abandon).
_ESC = 0x1B  # config-globals: ignore -- terminal control byte (ANSI ESC)
_ESC_INTRODUCERS = frozenset((0x5B, 0x4F))  # ``[`` (CSI) / ``O`` (SS3).

# Bracketed-paste markers, sans the leading ESC (which the accumulator strips
# before classifying). The terminal brackets pasted text in these so a TUI can
# treat it as one atomic block; the detector uses them to keep an embedded
# newline literal rather than a submit.
_PASTE_START = (
    b"[200~"  # config-globals: ignore -- terminal control sequence (bracketed-paste)
)
_PASTE_END = (
    b"[201~"  # config-globals: ignore -- terminal control sequence (bracketed-paste)
)


class SlashCommandDetector:
    """Reassemble typed lines from raw keystrokes; emit submitted slash-commands.

    Fed the human's keystroke bytes (as the pump tees them), it tracks the
    current line and, on Enter, invokes ``on_command`` with a parsed
    :class:`SlashCommand` plus the submit-time clock when the line began with
    ``/``. Stateful and **not** thread-safe: the pump calls :meth:`feed` from
    its single I/O loop, so one detector lives per run with no internal
    locking.

    Args:
      on_command: Sink for a detected command, called synchronously from
        :meth:`feed` with ``(command, submitted_at)``. Exceptions it raises are
        caught and logged -- a sink bug must not crash the pump's I/O loop and
        dump a traceback into the live TUI.
      clock: Returns the submit-time instant stamped on each command; injectable
        for tests. Defaults to :func:`datetime.now` in UTC.

    """

    def __init__(
        self,
        on_command: Callable[[SlashCommand, datetime], None],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._on_command = on_command
        self._clock = clock
        self._line = bytearray()
        self._in_escape = False
        self._escape = bytearray()  # bytes of the in-progress escape, post-ESC
        self._in_paste = False  # inside a bracketed paste (newlines stay literal)

    def feed(self, data: bytes) -> None:
        """Consume one chunk of raw keystroke bytes, emitting on each Enter."""
        for byte in data:
            self._consume(byte)

    def _consume(self, byte: int) -> None:
        if self._in_escape:
            self._consume_escape(byte)
            return
        if byte == _ESC:
            self._in_escape = True
            self._escape = bytearray()  # accumulate the sequence to classify it
        elif byte in _ENTER:
            # A newline inside a bracketed paste is literal content, not a
            # submit: a multi-line paste is one line of input, so treating an
            # embedded LF as Enter would submit a misleading partial (``/foo``)
            # and orphan the rest. Only a newline typed outside a paste submits.
            if self._in_paste:
                self._line.append(byte)
            else:
                self._submit()
        elif byte in _BACKSPACE:
            if self._line:
                self._line.pop()
        elif byte in _CLEAR_LINE:
            self._line.clear()
        elif byte == _WORD_ERASE:
            self._erase_word()
        elif byte >= 0x20:  # printable; other low control bytes are ignored
            self._line.append(byte)

    def _consume_escape(self, byte: int) -> None:
        """Accumulate an in-progress ANSI escape sequence until its final byte.

        A CSI/SS3 sequence (``ESC [`` / ``ESC O``) ends on a byte in 0x40-0x7E;
        a bare two-byte ``ESC x`` (no ``[``/``O`` introducer) ends on its second
        byte. On completion the sequence is classified: the bracketed-paste
        markers ``ESC [ 2 0 0 ~`` / ``ESC [ 2 0 1 ~`` toggle :attr:`_in_paste`
        (so embedded newlines stay literal); everything else is discarded.
        """
        self._escape.append(byte)
        if len(self._escape) == 1:
            # First byte after ESC: a CSI/SS3 introducer continues; otherwise
            # it is a complete two-byte escape.
            if byte not in _ESC_INTRODUCERS:
                self._end_escape()
            return
        if 0x40 <= byte <= 0x7E:
            self._end_escape()

    def _end_escape(self) -> None:
        """Finish an escape sequence, toggling paste mode on the paste markers."""
        if bytes(self._escape) == _PASTE_START:
            self._in_paste = True
        elif bytes(self._escape) == _PASTE_END:
            self._in_paste = False
        self._in_escape = False
        self._escape = bytearray()

    def _erase_word(self) -> None:
        """Drop the trailing whitespace run plus the word before it (Ctrl-W)."""
        while self._line and self._line[-1] == 0x20:  # space
            self._line.pop()
        while self._line and self._line[-1] != 0x20:
            self._line.pop()

    def _submit(self) -> None:
        """Flush the current line, emitting a command when it starts with ``/``.

        The leading byte must be ``/`` with no preceding whitespace: a CLI
        treats a line as a slash-command only when ``/`` is the first prompt
        character, so a line like `` /exit`` (leading space) is ordinary
        model-visible text, not a command, and must not mint a false
        provenance row.
        """
        line = bytes(self._line).decode(errors="replace")
        self._line.clear()
        if not line.startswith("/"):
            return
        # Split the verb off on the first run of whitespace -- including a
        # newline pasted as part of a multi-line block -- so ``/foo\nbar`` is
        # the command ``foo`` with argument ``bar``, not a verb with a embedded
        # newline. ``split(maxsplit=1)`` collapses the leading-no-arg case too.
        parts = line[1:].split(maxsplit=1)
        if not parts:
            return
        command = SlashCommand(
            command=parts[0],
            args=parts[1].strip() if len(parts) > 1 else "",
        )
        try:
            self._on_command(command, self._clock())
        except Exception:
            _logger.warning("slash-command sink failed for %r", command, exc_info=True)
