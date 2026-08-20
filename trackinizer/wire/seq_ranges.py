"""Seq-range intervals shared by the CLI, the HTTP client, and the server.

A seq selector is a union of inclusive intervals over a kind's ``seq``
column. Each interval is one :class:`SeqRange`; the union rides the wire
as repeated ``seq_range=<a..b>`` query params, one param per interval,
mirroring how ``filter=`` already repeats (see ``client.list_kind``).

The wire spelling of one interval is ``start..stop`` with either side
optional (``222..``, ``..10``, ``222..260``). The CLI's comma-separated
union (``222..260,279..``) is split into intervals *before* it reaches
the wire, so this module never parses a comma -- it is the single-interval
boundary that both the client and the server agree on.
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "SeqRange",
    "format_interval",
    "parse_interval",
    "parse_seq_range",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class SeqRange:
    """One inclusive ``seq`` interval; either bound may be open (``None``)."""

    start: int | None = None
    stop: int | None = None

    def __post_init__(self) -> None:
        # A fully-open range lowers to an empty bound list, and
        # ``seq_range_clause`` would emit ``()`` -- a SQL syntax error. The
        # wire parser already rejects bare ``..``, but a direct Store caller
        # that constructs the range programmatically bypasses it; reject here
        # so the at-least-one-bound invariant holds wherever a SeqRange exists.
        if self.start is None and self.stop is None:
            raise ValueError("seq range requires at least one bound")


def format_interval(interval: SeqRange) -> str:
    """Render one interval to its wire spelling ``start..stop``.

    An open bound renders as the empty side (``222..``, ``..10``); a
    closed single-row interval renders ``n..n``. The inverse of
    :func:`parse_interval`.
    """
    start = str(interval.start) if interval.start is not None else ""
    stop = str(interval.stop) if interval.stop is not None else ""
    return f"{start}..{stop}"


def parse_interval(text: str) -> SeqRange:
    """Parse one wire interval ``start..stop`` into a :class:`SeqRange`.

    Args:
      text: The wire spelling of a single interval. Exactly one ``..``
        separates an optional integer start from an optional integer
        stop; at least one side must be present.

    Returns:
      interval: The parsed inclusive interval.

    Raises:
      ValueError: If ``text`` is not a single well-formed interval.

    """
    if text.count("..") != 1:
        raise ValueError(f"invalid seq range {text!r}")
    start, stop = text.split("..")
    if not start and not stop:
        raise ValueError("seq range requires a start or stop")
    if start and not start.isdigit():
        raise ValueError(f"invalid seq range start {start!r}")
    if stop and not stop.isdigit():
        raise ValueError(f"invalid seq range stop {stop!r}")
    return SeqRange(
        start=int(start) if start else None,
        stop=int(stop) if stop else None,
    )


def parse_seq_range(text: str, *, min_seq: int = 0) -> SeqRange:
    """Parse one wire interval and reject any bound below ``min_seq``.

    The single seq-range parser for every wire boundary. ``parse_interval``
    handles the grammar; this adds the inclusive lower bound the seq space
    enforces -- ``min_seq=1`` for inquiry ``seq`` (starts at 1), ``min_seq=0``
    for event ``seq`` (harness-assigned from 0). The two callers differ only
    in that argument, so the parse itself lives in exactly one place.

    Args:
      text: The wire spelling of one interval (``a..b``, ``a..``, ``..b``).
      min_seq: Inclusive lower bound every present bound must meet.

    Returns:
      interval: The parsed, range-checked interval.

    Raises:
      ValueError: If the grammar is malformed or a bound is below ``min_seq``.

    """
    interval = parse_interval(text)
    if interval.start is not None and interval.start < min_seq:
        raise ValueError(f"seq range start must be >= {min_seq}")
    if interval.stop is not None and interval.stop < min_seq:
        raise ValueError(f"seq range stop must be >= {min_seq}")
    # An inverted closed interval (``260..222``) selects nothing and is almost
    # always a fat-fingered bound; reject it so the caller sees a 400 rather
    # than a silent empty 200 that reads as "no data".
    if (
        interval.start is not None
        and interval.stop is not None
        and interval.start > interval.stop
    ):
        raise ValueError(
            f"seq range start {interval.start} exceeds stop {interval.stop}"
        )
    return interval
