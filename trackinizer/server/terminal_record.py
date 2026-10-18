"""Record a terminal's output as a private AgentSession.

The server records at the relay point, so neither the host nor the browser can skip
it. The shell's output becomes ``Stdout`` lines in the part named ``stream``, the same
records ``trax run sh`` writes, so the Console shows them like any ``sh`` session.
Keystrokes are not recorded: the pty echoes what the shell shows, and input with echo
off (a password) never reaches the recording.

Lines pass the credential shapes of :mod:`~trackinizer.wire.key_shapes` before
they are stored; the live bytes the browser sees are never touched. Past
:data:`~trackinizer.wire.wire_terminals.RECORD_CAP_BYTES` one marker line is
written and the rest is relayed unrecorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import asyncio
import logging
import re

from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.types.line_capture import LineCapture, normalize
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import TraxRecord
from trackinizer.wire.audience import PRIVATE
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.key_shapes import KeyMasker
from trackinizer.wire.wire_session_ir import MAX_RECORD_BATCH
from trackinizer.wire.wire_terminals import RECORD_CAP_BYTES


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from trackinizer.server.store.core import Store
    from trackinizer.server.terminals import Terminal


__all__ = ["TerminalRecorder", "plain_text"]

_logger = logging.getLogger(__name__)

_MAX_BACKLOG_RECORDS: Final = 10 * MAX_RECORD_BATCH


@dataclass(slots=True, kw_only=True, eq=False)
class _Recording:
    """One terminal's capture, from its first byte to its end."""

    session_id: UUID
    actor: str
    ir_id: UUID = field(default_factory=uuid4)
    tail: Tail[TraxRecord] = field(default_factory=lambda: Tail(normalize))
    masker: KeyMasker = field(default_factory=KeyMasker)
    capture: LineCapture | None = None
    pending: list[TraxRecord] = field(default_factory=list)
    stored: int = 0
    """Records already written; the next record's ``idx``."""

    bytes_seen: int = 0
    flusher: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held while a batch is stored, so two flushes never share an ``idx`` base."""


class TerminalRecorder:
    """Starts a terminal's session, stores its output in batches, and ends it.

    Args:
      store: Where sessions and records live.
      flush_sec: How long output may wait before it is stored.
      sleep: Waits the given seconds; only the flush timer uses it.
      cap_bytes: Output recorded per session before one marker line replaces the rest.

    """

    def __init__(
        self,
        store: Store,
        *,
        flush_sec: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cap_bytes: int = RECORD_CAP_BYTES,
    ) -> None:
        self._store = store
        self._flush_sec = flush_sec
        self._sleep = sleep
        self._cap_bytes = cap_bytes
        self._recordings: dict[UUID, _Recording] = {}
        self._live: dict[UUID, str] = {}

    async def start(
        self,
        *,
        user: str,
        machine: str,
        workspace: UUID | None,
        requested_by: str | None = None,
    ) -> UUID:
        """Open the private session a terminal is recorded to; return its id.

        Args:
          user: The opener, to whom the session is attributed and private.
          machine: The machine's name, for the title and a label.
          workspace: The station the terminal opens from, for a label.
          requested_by: The actor that asked for it, for a label; only shown.

        Returns:
          session_id: The new session.

        """
        session_id, actor, _ = await self._store.start_session(
            SubmitAgentSession(
                title=f"Terminal on {machine}",
                cli="terminal",
                account=user,
            ),
            requested_actor="terminal",
            audience=PRIVATE,
        )
        labels = ["terminal", f"machine:{machine}"]
        if workspace is not None:
            labels.append(f"station:{workspace}")
        if requested_by not in (None, ""):
            labels.append(f"requested-by:{requested_by}")
        await self._store.set_labels(session_id, labels, actor=actor)
        self._live[session_id] = actor
        return session_id

    async def abandon(self, session_id: UUID) -> None:
        """End the session of a terminal that was never opened."""
        await self._end_session(session_id)

    async def touch_live(self) -> None:
        """Note that every recorded terminal's session is still alive."""
        now = datetime.now(UTC)
        for session_id in list(self._live):
            try:
                await self._store.record_session_seen(session_id, at=now, polled=True)
            except Exception:
                _logger.warning("terminal liveness: %s", session_id, exc_info=True)

    async def output(self, terminal: Terminal, data: bytes) -> None:
        """Record bytes the shell wrote, flushing when a batch is full.

        Args:
          terminal: The terminal that wrote them.
          data: What the shell wrote.

        """
        recording = self._recordings.get(terminal.id)
        if recording is None:
            recording = self._recordings[terminal.id] = _Recording(
                session_id=terminal.session_id,
                actor=self._live[terminal.session_id],
            )
            recording.capture = LineCapture(
                lambda raw: self._line(recording, raw.decode(errors="replace")),
            )
        if recording.bytes_seen > self._cap_bytes:
            return
        room = self._cap_bytes - recording.bytes_seen
        recording.bytes_seen += len(data)
        if recording.capture is None:
            raise ValueError("Expected recording.capture is not None.")
        recording.capture.feed(data[:room])
        if recording.bytes_seen > self._cap_bytes:
            recording.capture.close()
            self._line(recording, "... (recording truncated)\n")
        if len(recording.pending) >= MAX_RECORD_BATCH:
            await self._flush(recording)
        elif recording.pending and recording.flusher is None:
            recording.flusher = asyncio.create_task(self._flush_later(recording))

    async def ended(self, terminal: Terminal, *, status: int | None) -> None:
        """Store what is left and end the session.

        Args:
          terminal: The terminal that ended.
          status: The shell's exit status if known; the session does not keep it.

        """
        del status
        recording = self._recordings.pop(terminal.id, None)
        if recording is not None:
            if recording.flusher is not None:
                recording.flusher.cancel()
            if recording.capture is None:
                raise ValueError("Expected recording.capture is not None.")
            recording.capture.close()
            recording.pending.extend(recording.tail.close())
            await self._flush(recording)
        await self._end_session(terminal.session_id)

    def _line(self, recording: _Recording, text: str) -> None:
        """Mask one framed line and keep the records it makes."""
        recording.pending.extend(
            recording.tail.feed(recording.masker.mask(plain_text(text))),
        )

    async def _flush_later(self, recording: _Recording) -> None:
        """Store the recording's records after the flush interval."""
        await self._sleep(self._flush_sec)
        recording.flusher = None
        await self._flush(recording)

    async def _flush(self, recording: _Recording) -> None:
        """Store pending records; on failure keep them, dropping the oldest past a bound."""
        async with recording.lock:
            await self._store_pending(recording)

    async def _store_pending(self, recording: _Recording) -> None:
        """Write the recording's pending records as one batch."""
        if not recording.pending:
            return
        batch = recording.pending
        recording.pending = []
        # Audience: internal -- the server writes the session it opened for the
        # terminal's owner, and returns nothing to any caller.
        try:
            part = await self._store.upsert_session_manifest(
                recording.session_id,
                name="stream",
                metadata={},
                ir_id=recording.ir_id,
                format="",
                records=recording.stored + len(batch),
            )
            await self._store.append_session_records(
                recording.session_id,
                [
                    SessionRecordRow.of(
                        session_id=recording.session_id,
                        part=part,
                        idx=recording.stored + n,
                        record=record,
                    )
                    for n, record in enumerate(batch)
                ],
            )
        except Exception:
            _logger.warning("terminal recording: store failed", exc_info=True)
            recording.pending = (batch + recording.pending)[-_MAX_BACKLOG_RECORDS:]
            return
        recording.stored += len(batch)

    async def _end_session(self, session_id: UUID) -> None:
        """End the terminal's session; a failure is logged, never raised."""
        actor = self._live.pop(session_id, None)
        if actor is None:
            return
        try:
            await self._store.end_session(
                session_id,
                ended=datetime.now(UTC),
                actor=actor,
            )
        except Exception:
            _logger.warning("terminal recording: end failed", exc_info=True)


# A key echoed by a shell is broken up by the terminal's own control sequences (readline
# redraws, syntax colouring), so a credential shape must be matched on the text a
# person would read, not on the bytes. CSI, OSC and SS3 sequences are removed, and the
# line is replayed on a one-line screen so a backspace or carriage return overwrites
# what it covers and an erase-to-end-of-line truncates.
_TOKEN: Final = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC, ended by BEL or ST.
    r"|\x1b\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~])"  # CSI.
    r"|\x1bO."  # SS3.
    r"|\x1b[ -/]*[0-~]?"  # Any other escape.
    r"|(?P<control>[\x00-\x08\x0a-\x1f\x7f])"
    r"|(?P<text>.)",
    re.DOTALL,
)


def plain_text(line: str) -> str:
    """Return the text a terminal shows for one captured line.

    Args:
      line: One framed line, with its line break.

    Returns:
      text: The line without escape sequences, with backspaces and carriage returns
        applied as overwrites and ``ESC [ K`` as a truncation; the line break is kept.

    """
    ending = "\n" if line.endswith("\n") else ""
    cells: list[str] = []
    column = 0
    for token in _TOKEN.finditer(line.removesuffix("\n")):
        if (final := token["final"]) is not None:
            if final == "K" and token["params"] in ("", "0"):
                del cells[column:]
        elif (control := token["control"]) is not None:
            if control == "\r":
                column = 0
            elif control == "\x08":
                column = max(0, column - 1)
        elif (char := token["text"]) is not None:
            cells[column : column + 1] = [char]
            column += 1
    return "".join(cells) + ending
