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


def test_a_reset_restarts_the_stream_at_an_offset_with_nothing_kept() -> None:
    ring = Ring(capacity=8)
    ring.append(b"abc")

    ring.reset(10)
    ring.append(b"xy")

    assert (ring.start, ring.end) == (10, 12)
    assert ring.since(10) == (b"xy", 10, False)
    assert ring.since(0) == (b"xy", 10, True)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
