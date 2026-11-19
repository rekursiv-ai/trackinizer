"""The tail of a byte stream, addressed by absolute offset."""

from __future__ import annotations


__all__ = ["Ring"]


class Ring:
    """The last ``capacity`` bytes of a stream, addressed by absolute offset."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._data = bytearray()
        self.end = 0
        """Offset one past the last byte appended."""

    @property
    def start(self) -> int:
        """Offset of the oldest byte kept."""
        return self.end - len(self._data)

    def append(self, data: bytes) -> None:
        """Append ``data``, dropping the oldest bytes past the capacity."""
        self._data.extend(data)
        self.end += len(data)
        del self._data[: max(0, len(self._data) - self._capacity)]

    def seek(self, offset: int) -> None:
        """Continue the stream at ``offset``, keeping the bytes kept before it.

        A sender that resends from ``offset`` resends the same bytes, so what the ring
        holds before it stays valid. An offset past the end leaves a gap, and one before
        the start keeps nothing, so either forgets every byte kept.

        Args:
          offset: Where the next byte appended falls in the stream.

        """
        if self.start <= offset <= self.end:
            del self._data[offset - self.start :]
        else:
            self._data.clear()
        self.end = offset

    def since(self, offset: int) -> tuple[bytes, int, bool]:
        """Return the bytes from ``offset``, where they start, and whether some were lost.

        Args:
          offset: The stream position the caller has reached.

        Returns:
          data: The bytes from the later of ``offset`` and the ring's start.
          resume: The offset ``data`` starts at.
          lost: Whether ``offset`` had fallen out of the ring.

        """
        offset = min(offset, self.end)
        lost = offset < self.start
        offset = max(offset, self.start)
        return bytes(self._data[offset - self.start :]), offset, lost
