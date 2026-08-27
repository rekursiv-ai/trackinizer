"""Tests for the IO-stream adapter and its line framing."""

from __future__ import annotations

from datetime import UTC, datetime

from trackinizer.trax.run.adapters.iostream import (
    _MAX_LINE_BYTES,
    IOStreamAdapter,
    LineCapture,
)
from trackinizer.trax.run.custom_types import Event
from trackinizer.types.agent_session_events import AssistantMessage


def _texts(events: list[Event]) -> list[str]:
    """The captured line texts; asserts every event is an AssistantMessage."""
    out: list[str] = []
    for event in events:
        assert isinstance(event.message, AssistantMessage)
        out.append(event.message.text)
    return out


def _collect() -> tuple[list[Event], LineCapture]:
    adapter = IOStreamAdapter()
    events: list[Event] = []
    capture = LineCapture(
        lambda raw: adapter.parse(raw, whole_file=False),
        events.append,
    )
    return events, capture


class TestAdapter:
    def test_parse_strips_csi_and_carriage_return(self) -> None:
        adapter = IOStreamAdapter()
        events = list(adapter.parse(b"\x1b[32mgreen\x1b[0m text\r", whole_file=False))
        assert _texts(events) == ["green text"]

    def test_parse_strips_osc_sequences(self) -> None:
        r"""OSC (title-set etc.) must not survive into captured text.

        Shells routinely emit ``\x1b]0;title\x07``; the contract is "the
        text, not the rendering", and OSC is rendering exactly like CSI.
        """
        adapter = IOStreamAdapter()
        events = list(adapter.parse(b"\x1b]0;my title\x07hello", whole_file=False))
        assert _texts(events) == ["hello"]

    def test_blank_line_yields_nothing(self) -> None:
        adapter = IOStreamAdapter()
        assert list(adapter.parse(b"\r", whole_file=False)) == []

    def test_events_carry_timestamps(self) -> None:
        adapter = IOStreamAdapter()
        before = datetime.now(UTC)
        (event,) = adapter.parse(b"x", whole_file=False)
        assert event.timestamp is not None
        assert event.timestamp >= before


class TestLineCapture:
    def test_frames_lines_across_chunk_boundaries(self) -> None:
        events, capture = _collect()
        capture.feed(b"al")
        capture.feed(b"pha\nbe")
        assert _texts(events) == ["alpha"]
        capture.feed(b"ta\n")
        assert _texts(events) == ["alpha", "beta"]

    def test_close_flushes_unterminated_tail(self) -> None:
        events, capture = _collect()
        capture.feed(b"last words")
        assert events == []
        capture.close()
        assert _texts(events) == ["last words"]

    def test_unterminated_output_does_not_grow_without_bound(self) -> None:
        """Newline-free output must not accumulate memory indefinitely.

        The clamp exists for "a progress bar rewriting itself" -- output
        that never emits a newline. Clamping only at line-emit time never
        fires for such a child, so the bound must apply at ingest.
        """
        _, capture = _collect()
        for _ in range(100):
            capture.feed(b"x" * 10_000)
        assert len(capture._buffer) <= 2 * _MAX_LINE_BYTES, (
            "ingest must clamp; a newline-free child grew the buffer to "
            f"{len(capture._buffer)} bytes"
        )

    def test_clamped_line_is_emitted_with_marker(self) -> None:
        events, capture = _collect()
        capture.feed(b"y" * (_MAX_LINE_BYTES + 100) + b"\n")
        (text,) = _texts(events)
        assert "truncated" in text
        assert len(text) <= _MAX_LINE_BYTES + 64

    def test_parse_error_skips_line_and_continues(self) -> None:
        """A raising parser must not propagate into the pump thread.

        ``feed`` runs on the pump's IO path (``_forward``); an escaping
        exception unwinds ``_pump`` and terminates the whole run. The file
        drain guards the identical seam (session.py ``_process_chunk``); the
        stream path owes the same resilience.
        """
        events: list[Event] = []

        def flaky_parse(raw: bytes) -> list[Event]:
            if b"bad" in raw:
                raise ValueError("malformed line")
            return list(IOStreamAdapter().parse(raw, whole_file=False))

        capture = LineCapture(flaky_parse, events.append)
        capture.feed(b"bad\ngood\n")
        assert _texts(events) == ["good"]


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
