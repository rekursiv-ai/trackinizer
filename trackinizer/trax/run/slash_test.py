"""Tests for the keystroke slash-command detector."""

from __future__ import annotations

from datetime import UTC, datetime

from trackinizer.trax.run.slash import SlashCommandDetector
from trackinizer.types.agent_session_events import SlashCommand


def _collect(*chunks: bytes) -> list[SlashCommand]:
    """Feed ``chunks`` to a detector and return the commands it emitted."""
    out: list[SlashCommand] = []
    detector = SlashCommandDetector(lambda command, _at: out.append(command))
    for chunk in chunks:
        detector.feed(chunk)
    return out


class TestSlashCommandDetector:
    def test_bare_command_on_enter(self) -> None:
        assert _collect(b"/exit\r") == [SlashCommand(command="exit", args="")]

    def test_command_with_args(self) -> None:
        assert _collect(b"/model gpt-5\r") == [
            SlashCommand(command="model", args="gpt-5")
        ]

    def test_non_slash_line_is_ignored(self) -> None:
        # Ordinary prompt text is the agent's input, captured from the log --
        # not a slash-command, so the detector stays silent.
        assert _collect(b"hello there\r") == []

    def test_split_across_chunks(self) -> None:
        # Keystrokes arrive byte-by-byte in raw mode; the line accumulates
        # across feeds until Enter.
        assert _collect(b"/ex", b"it", b"\n") == [SlashCommand(command="exit", args="")]

    def test_backspace_edits_the_line(self) -> None:
        # Type "/exitz", rub out the stray trailing char, submit.
        assert _collect(b"/exitz\x7f\r") == [SlashCommand(command="exit", args="")]

    def test_ctrl_u_kills_the_line(self) -> None:
        # Ctrl-U clears a mistyped command before the real one.
        assert _collect(b"/wrong\x15/exit\r") == [SlashCommand(command="exit", args="")]

    def test_low_control_bytes_do_not_corrupt_text(self) -> None:
        # A stray low control byte (here a NUL) is dropped rather than appended
        # as a literal char.
        assert _collect(b"/exit\x00\r") == [SlashCommand(command="exit", args="")]

    def test_escape_sequence_is_swallowed_not_appended(self) -> None:
        # An arrow-key CSI sequence (ESC [ A) mid-line is consumed through its
        # final byte, so it does not corrupt the verb with ``[A`` (R-016).
        assert _collect(b"/model\x1b[A\r") == [SlashCommand(command="model", args="")]

    def test_bracketed_paste_markers_are_swallowed(self) -> None:
        # An outer terminal's bracketed-paste markers (ESC [ 200~ / ESC [ 201~)
        # are escape sequences, consumed rather than leaked into the command.
        assert _collect(b"/say \x1b[200~hi\x1b[201~\r") == [
            SlashCommand(command="say", args="hi")
        ]

    def test_newline_inside_paste_is_not_a_submit(self) -> None:
        # A multi-line paste is one input line: an embedded LF must stay literal,
        # not submit a misleading partial ``/foo`` and orphan ``bar``. The real
        # Enter (after the paste-end marker) submits the whole thing.
        assert _collect(b"\x1b[200~/foo\nbar\x1b[201~\r") == [
            SlashCommand(command="foo", args="bar")
        ]

    def test_multi_line_paste_emits_no_partial(self) -> None:
        # The classic regression: pasted ``/foo\nbar`` must not mint ``/foo``
        # alone. Exactly one command, carrying the full pasted text.
        out = _collect(b"\x1b[200~/foo\nbar baz\x1b[201~\r")
        assert len(out) == 1
        assert out[0].command == "foo"
        assert "bar" in out[0].args

    def test_crlf_inside_paste_stays_literal(self) -> None:
        # Windows-style CRLF inside a paste is two literal bytes, not a submit.
        assert _collect(b"\x1b[200~/foo\r\nbar\x1b[201~\r") == [
            SlashCommand(command="foo", args="bar")
        ]

    def test_newline_outside_paste_still_submits(self) -> None:
        # Outside any paste, a typed newline submits as before -- the paste
        # gate must not suppress ordinary Enter.
        assert _collect(b"/exit\n") == [SlashCommand(command="exit", args="")]

    def test_lone_slash_emits_nothing(self) -> None:
        assert _collect(b"/\r") == []

    def test_leading_whitespace_is_not_a_command(self) -> None:
        # A CLI treats a slash-command only when ``/`` is the first prompt
        # character; `` /exit`` is ordinary model-visible text (REV2-SLASH-001).
        assert _collect(b" /exit\r") == []

    def test_interrupt_abandons_the_line(self) -> None:
        # Ctrl-C abandons the partially typed command.
        assert _collect(b"/exit\x03\r") == []

    def test_word_erase_drops_last_word(self) -> None:
        # Ctrl-W erases ``gpt-4`` so the corrected arg is submitted.
        assert _collect(b"/model gpt-4\x17gpt-5\r") == [
            SlashCommand(command="model", args="gpt-5")
        ]

    def test_multiple_commands(self) -> None:
        assert _collect(b"/clear\r/exit\r") == [
            SlashCommand(command="clear", args=""),
            SlashCommand(command="exit", args=""),
        ]

    def test_sink_exception_does_not_propagate(self) -> None:
        # A raising sink must not crash the pump's I/O loop (R-017).
        def _boom(_command: SlashCommand, _at: object) -> None:
            raise ZeroDivisionError("boom")

        detector = SlashCommandDetector(_boom)
        detector.feed(b"/exit\r")  # must not raise

    def test_command_carries_submit_timestamp(self) -> None:
        # The detector stamps each command with the submit-time clock (R-019).
        fixed = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        stamps: list[object] = []
        detector = SlashCommandDetector(
            lambda _c, at: stamps.append(at), clock=lambda: fixed
        )
        detector.feed(b"/exit\r")
        assert stamps == [fixed]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
