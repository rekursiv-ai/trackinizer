"""Line framing for the PTY stream capture.

The adapter itself is now just a locator plus a normalizer factory (the
reading is ``trackinizer.trax.run.adapters.scrape``'s, tested there). What remains
here is :class:`LineCapture`: the framing that turns raw master-fd chunks
into whole lines, which no other component owns.
"""

from __future__ import annotations

from pathlib import Path

from trackinizer.trax.run.adapters.iostream import (
    _MAX_LINE_BYTES,
    IOStreamAdapter,
    LineCapture,
)


def _collect() -> tuple[list[bytes], LineCapture]:
    """A capture writing framed lines into a list."""
    lines: list[bytes] = []
    return lines, LineCapture(lines.append)


class TestAdapter:
    def test_has_no_session_files_to_tail(self) -> None:
        """A wrapped command writes no log; the PTY stream is the source."""
        adapter = IOStreamAdapter()

        assert tuple(adapter.session_dirs()) == ()
        assert adapter.session_scope() is None

    def test_names_no_cli_binary(self) -> None:
        """The command comes from the ``--`` args, not the adapter.

        The empty binary is what the runner dispatches on to take the
        command line from the user instead of exec'ing a known CLI.
        """
        assert IOStreamAdapter().cli_binary == ""

    def test_is_not_resumable(self) -> None:
        """No CLI wrote these bytes, so none can be handed them back."""
        assert IOStreamAdapter().session_id_from_path(Path("x")) is None


class TestLineCapture:
    def test_frames_lines_across_chunk_boundaries(self) -> None:
        lines, capture = _collect()

        capture.feed(b"al")
        capture.feed(b"pha\nbe")
        assert lines == [b"alpha\n"]

        capture.feed(b"ta\n")
        assert lines == [b"alpha\n", b"beta\n"]

    def test_a_framed_line_keeps_its_terminator(self) -> None:
        """The reader downstream is TOTAL, so the newline is content.

        Every line becomes one ``IncompleteRecord`` and the records concatenate
        back to the bytes read. A stripped terminator would make a scrape
        rewrite one byte short per line, and the capture would report itself
        unterminated when it was not.
        """
        lines, capture = _collect()

        capture.feed(b"one\ntwo\n")

        assert b"".join(lines) == b"one\ntwo\n"

    def test_close_flushes_unterminated_tail(self) -> None:
        """A child that exits mid-line gets its last words, without a newline.

        The terminator is content, so inventing one here would claim the child
        ended a line it never ended.
        """
        lines, capture = _collect()

        capture.feed(b"last words")
        assert lines == []

        capture.close()
        assert lines == [b"last words"]

    def test_unterminated_output_does_not_grow_without_bound(self) -> None:
        """Newline-free output must not accumulate memory indefinitely.

        The clamp exists for "a progress bar rewriting itself" -- output that
        never emits a newline. Clamping only at line-emit time never fires for
        such a child, so the bound must apply at ingest.
        """
        _, capture = _collect()

        for _ in range(100):
            capture.feed(b"x" * 10_000)

        assert len(capture._buffer) <= 2 * _MAX_LINE_BYTES, (
            "ingest must clamp; a newline-free child grew the buffer to "
            f"{len(capture._buffer)} bytes"
        )

    def test_clamped_line_is_emitted_with_marker(self) -> None:
        """A truncated line says so rather than silently losing its tail.

        The marker lands BEFORE the terminator: a clamped line is still one
        line, and a marker past the newline would split it into two records.
        """
        lines, capture = _collect()

        capture.feed(b"y" * (_MAX_LINE_BYTES + 100) + b"\n")

        (line,) = lines
        assert b"truncated" in line
        assert line.endswith(b"... (truncated)\n")
        assert line.count(b"\n") == 1
        assert len(line) <= _MAX_LINE_BYTES + 64

    def test_a_consumer_error_skips_the_line_and_continues(self) -> None:
        """A raising consumer must not propagate into the pump thread.

        ``feed`` runs on the pump's IO path; an escaping exception unwinds the
        pump and terminates the whole run. The file drain guards the identical
        seam (``session.py::_process_chunk``); the stream path owes the same
        resilience.
        """
        seen: list[bytes] = []

        def flaky(raw: bytes) -> None:
            if b"bad" in raw:
                raise ValueError("malformed line")
            seen.append(raw)

        capture = LineCapture(flaky)
        capture.feed(b"bad\ngood\n")

        assert seen == [b"good\n"]


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
