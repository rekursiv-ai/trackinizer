"""The ring: the tail of a byte stream, addressed by absolute offset."""

from __future__ import annotations

from trackinizer.lib.posix.ring import Ring


def test_the_ring_keeps_the_tail_and_reads_from_an_offset() -> None:
    ring = Ring(capacity=3)
    ring.append(b"ab")
    ring.append(b"cde")

    assert (ring.start, ring.end) == (2, 5)
    assert ring.since(99) == (b"", 5, False)
    assert ring.since(3) == (b"de", 3, False)
    assert ring.since(0) == (b"cde", 2, True)


def test_a_seek_past_the_end_continues_the_stream_there_with_nothing_kept() -> None:
    ring = Ring(capacity=8)
    ring.append(b"abc")

    ring.seek(10)
    ring.append(b"xy")

    assert (ring.start, ring.end) == (10, 12)
    assert ring.since(10) == (b"xy", 10, False)
    assert ring.since(0) == (b"xy", 10, True)


def test_a_seek_within_the_ring_keeps_every_byte_before_the_offset() -> None:
    ring = Ring(capacity=8)
    ring.append(b"abcdef")

    ring.seek(6)
    assert ring.since(0) == (b"abcdef", 0, False)
    ring.seek(4)
    ring.append(b"xy")

    assert ring.since(0) == (b"abcdxy", 0, False)


def test_a_seek_before_the_start_keeps_nothing() -> None:
    ring = Ring(capacity=3)
    ring.append(b"abcde")

    ring.seek(1)

    assert (ring.start, ring.end) == (1, 1)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
