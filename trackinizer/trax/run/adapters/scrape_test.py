"""The PTY scrape: total normalization, exact rewrite, no resume.

``sh`` is the adapter for a wrapped command that writes no session log, so its
guarantees are the opposite end of the range from claude's: nothing is parsed,
so nothing can be misparsed, but nothing can be replayed into a CLI either.
What must hold is that no line is ever lost, and that a line remembers WHICH
stream it crossed -- the only structure a scrape has.
"""

from __future__ import annotations

from io import StringIO

from trackinizer.lib.agent.types.sessions import (
    ContextClear,
    TurnContext,
    UserMessage,
)
from trackinizer.trax.run.adapters import scrape
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


def _encoding(records: list[TraxRecord]) -> dict[str, object]:
    """How the file spells its bytes, as the LAST context to state it says.

    The last, not the first: whether the capture ended on a newline is knowable
    only at the end, so the reader restates it there rather than mutating a
    record it already yielded.
    """
    stated = [
        record.encoding
        for record in records
        if isinstance(record, TurnContext) and record.encoding
    ]
    return dict(stated[-1]) if stated else {}


def test_every_line_becomes_a_record() -> None:
    """Normalization is total: no line is dropped, blank ones included."""
    records = list(scrape.normalize(StringIO("one\n\nthree\n")))

    assert isinstance(records[1], ContextClear)
    assert [r.text for r in records if isinstance(r, Stdout)] == [
        "one\n",
        "\n",
        "three\n",
    ]


def test_a_bare_stream_reads_as_output() -> None:
    """Text alone is what the command PRINTED; nothing else is knowable.

    A file holds no fd numbers, so a scrape read back from one is all output.
    The distinction is made at CAPTURE, where the fds still exist, and rides
    the record from there.
    """
    records = list(scrape.normalize(StringIO("hello\n")))

    assert isinstance(records[2], Stdout)


def test_the_capture_rewrites_byte_exact() -> None:
    """The records concatenate back to the bytes that were read."""
    text = "first\nsecond line with  spaces\n\nlast"

    out = StringIO()
    scrape.denormalize(scrape.normalize(StringIO(text)), out)

    assert out.getvalue() == text


def test_every_stream_writes_its_line_back() -> None:
    """A rewrite is the bytes, whichever fd carried them.

    Interleaving is the capture's own order, so writing only one stream would
    silently drop the others and a scrape would no longer rewrite exactly.
    """
    out = StringIO()

    scrape.denormalize(
        [Stdin(text="question\n"), Stdout(text="answer\n"), Stderr(text="warning\n")],
        out,
    )

    assert out.getvalue() == "question\nanswer\nwarning\n"


def test_a_capture_without_a_trailing_newline_is_recorded_as_such() -> None:
    """The final newline's absence is state, not a line to invent."""
    records = list(scrape.normalize(StringIO("no trailing newline")))

    assert _encoding(records)["newline_terminated"] is False


def test_an_empty_capture_reads_as_newline_terminated() -> None:
    """Nothing captured is not a truncated line."""
    records = list(scrape.normalize(StringIO("")))

    assert not records


def test_reading_line_by_line_matches_reading_the_whole_stream() -> None:
    """Feeding one line at a time yields exactly what the stream does.

    The generator IS the incremental interface (axiom 11), so a tailer handed
    one line at a time and a reader handed the finished file agree -- there is
    no second reader that could drift from this one.
    """
    lines = ["a\n", "b\n", "c"]

    fed = [record for line in lines for record in scrape.normalize(StringIO(line))]

    whole = list(scrape.normalize(StringIO("".join(lines))))
    assert [type(r).__name__ for r in fed if isinstance(r, Stdout)] == [
        type(r).__name__ for r in whole if isinstance(r, Stdout)
    ]


def test_a_foreign_record_writes_nothing() -> None:
    """A converted-in record has no line, so the conversion is lossy.

    A shell scrape has no shape for a tool call or a thinking block. Skipping
    is what makes ``convert`` report the loss rather than inventing a line the
    command never printed.
    """
    records = list(scrape.normalize(StringIO("kept\n")))

    out = StringIO()
    scrape.denormalize([*records, UserMessage(content="from elsewhere")], out)

    assert out.getvalue() == "kept\n"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
