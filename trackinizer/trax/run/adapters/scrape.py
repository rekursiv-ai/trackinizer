"""Read and write a captured stream: the session with no native log.

``trax run sh -- CMD`` wraps a command that writes no session file, so the
process's own IO IS the session. There is nothing to parse -- the contract is
line-delimited text -- and nothing to resume: no CLI wrote these bytes, so none
can be handed them back.

What a line DOES carry is which stream it crossed, and that is the whole
structure a scrape has: it separates an answer from the question that caused
it. So a line becomes a :class:`Stdin`, :class:`Stdout`, or :class:`Stderr`
rather than a shapeless record. Normalization stays TOTAL (no line is ever
dropped) and the rewrite exact (the records concatenate back to the bytes
read), while ``convert`` still reports a conversion out of this format as
lossy, because a stream cannot say which ACT produced it.

Reading a scrape back from a FILE yields only :class:`Stdout`: a file holds no
descriptors. The distinction is made at capture, where the fds still exist
(:mod:`~trackinizer.trax.run.adapters.iostream`), and travels on the
record from there.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TextIO

from trackinizer.lib.agent.types.sessions import ContextClear, TurnContext
from trackinizer.lib.custom_json import json_freeze
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


__all__ = ["denormalize", "normalize"]


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
            yield TurnContext(encoding=json_freeze({"newline_terminated": True}))
            yield ContextClear(extra=json_freeze({"$opens": True}))
        total += 1
        ends_newline = line.endswith("\n")
        yield Stdout(text=line)
    if total and not ends_newline:
        # The last line lost its newline, which is knowable only here. Restated
        # rather than mutated: a record already yielded is the caller's, and a
        # later state record superseding an earlier one is how a stream says so.
        yield TurnContext(encoding=json_freeze({"newline_terminated": False}))


def denormalize(records: Iterable[TraxRecord], stream: TextIO) -> None:
    """Write the captured lines back, exactly as they were read.

    Every stream writes, in the order captured. Which fd carried a line is not
    recoverable from a flat file, so writing one stream and dropping the others
    would lose bytes the capture held -- and the interleaving IS the record.

    A record that is none of the three came from another provider's session
    converted into this format. It has no line to write -- a scrape has no
    shape for a tool call -- so it is skipped here and the conversion reports
    lossy.

    Args:
      records: Captured records, in stream order.
      stream: Destination text stream.

    """
    for record in records:
        if isinstance(record, Stdin | Stdout | Stderr):
            _ = stream.write(record.text)
