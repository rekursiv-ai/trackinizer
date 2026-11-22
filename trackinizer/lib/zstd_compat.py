"""One zstandard wire-format interface for every supported Python version.

The monorepo runs Python 3.14 and can use ``compression.zstd`` from the
stdlib, while exported packages support Python 3.12 and must not depend on
that 3.14-only module; there the ``zstandard`` wheel decodes the same frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TextIO, cast, override

import importlib
import io


# By name: a 3.12 checker has the stub but no source, so a static import fails
# ``reportMissingModuleSource`` in an export that keeps that check on.
try:
    zstd = cast("_StdlibZstd", importlib.import_module("compression.zstd"))
except ImportError:  # Python < 3.14.
    zstd = None
if TYPE_CHECKING:
    from collections.abc import Buffer
    from pathlib import Path

    import zstandard
else:
    from wrapt import lazy_import

    zstandard = lazy_import("zstandard")


def compress(data: bytes, *, level: int = 3) -> bytes:
    """Compress ``data`` as a zstandard frame."""
    if zstd is None:
        return zstandard.compress(data, level=level)
    return zstd.compress(data, level=level)


def decompress(data: bytes) -> bytes:
    """Decompress one or more zstandard frames from ``data``.

    Args:
      data: Zstandard-compressed bytes.

    Returns:
      result: The decompressed bytes.

    Raises:
      ValueError: The input is malformed or contains an incomplete frame.

    """
    if zstd is None:
        return _decompress_frames(data)
    try:
        return zstd.decompress(data)
    except zstd.ZstdError as error:
        raise ValueError(str(error)) from None


def open_text(
    path: Path,
    *,
    encoding: str,
    errors: str = "strict",
    newline: str | None = None,
) -> TextIO:
    """Open a zstandard-compressed file to read as text, decompressing as it is read.

    Every frame is read, as :func:`decompress` reads them, and none is held whole.

    Args:
      path: The compressed file.
      encoding: The text's encoding.
      errors: How bytes the encoding cannot read are taken, as ``open`` takes it.
      newline: How line endings are read, as ``open`` takes it.

    Returns:
      handle: The file's text. Reading a frame that is malformed or cut short raises
        ``ValueError``, whichever library decoded.

    """
    if zstd is None:
        source, failure = _Frames(path), zstandard.ZstdError
    else:
        source, failure = zstd.open(path), zstd.ZstdError
    return io.TextIOWrapper(
        io.BufferedReader(_Decompressed(source, failure=failure)),
        encoding=encoding,
        errors=errors,
        newline=newline,
    )


def _decompress_frames(data: bytes) -> bytes:
    """Decode every frame with the ``zstandard`` wheel."""
    # ``ZstdDecompressor().decompress`` reads only the first frame and rejects
    # frames lacking a content size, which streaming encoders (e.g. Cloudflare)
    # emit; its ``read_across_frames`` raises "not yet implemented".
    try:
        decompressor = zstandard.ZstdDecompressor()
        chunks: list[bytes] = []
        while True:
            decoder = decompressor.decompressobj()
            chunks.append(decoder.decompress(data))
            if not decoder.eof:
                raise ValueError("Incomplete zstandard frame")
            data = decoder.unused_data
            if not data:
                return b"".join(chunks)
    except zstandard.ZstdError as error:
        raise ValueError(str(error)) from None


# Not the wheel's ``open``: its reader returns a frame cut short as far as it got, and
# raises nothing, so a truncated file would read as a shorter one.
class _Frames:
    """A file's frames, decompressed by the ``zstandard`` wheel; one cut short refused.

    Args:
      path: The compressed file.

    """

    def __init__(self, path: Path) -> None:
        self._file = path.open("rb")
        self._decompressor = zstandard.ZstdDecompressor()
        self._decoder = self._decompressor.decompressobj()
        # Open until a frame ends: a file with none is refused, as the stdlib does.
        self._open = True
        self._out: bytes = b""
        self._at: int = 0

    def read(self, size: int, /) -> bytes:
        """Return the next decompressed bytes.

        Args:
          size: The most to return, at least one.

        Returns:
          data: Up to ``size`` bytes; empty at the file's end.

        Raises:
          ValueError: The file ends inside a frame.

        """
        while self._at == len(self._out):
            chunk = self._file.read(
                io.DEFAULT_BUFFER_SIZE,  # pragma: no mutate -- the whole file at once decodes alike.
            )
            if not chunk:
                if self._open:
                    raise ValueError("Incomplete zstandard frame")
                return b""
            self._out, self._at = self._decoded(chunk), 0
        data = self._out[self._at : self._at + size]
        self._at += len(data)
        return data

    def close(self) -> None:
        """Close the file."""
        self._file.close()

    def _decoded(self, chunk: bytes) -> bytes:
        """Return what ``chunk`` decodes to, opening a decoder for each frame it starts."""
        out: list[bytes] = []
        while chunk:
            if self._decoder.eof:
                self._decoder = self._decompressor.decompressobj()
            out.append(self._decoder.decompress(chunk))
            self._open = not self._decoder.eof
            # What follows the frame's end, which is nothing until the frame has ended.
            chunk = self._decoder.unused_data
        return b"".join(out)


class _Decompressed(io.RawIOBase):
    """A compressed file's bytes as they decompress; a bad frame raises ``ValueError``.

    Args:
      source: The library's reader of the file.
      failure: The error the library raises on a bad frame. The stdlib's reader raises
        ``EOFError`` for one cut short at the file's end.

    """

    def __init__(self, source: _Source, *, failure: type[Exception]) -> None:
        super().__init__()
        self._source = source
        self._failure = failure

    @override
    def readable(self) -> bool:
        """Return True: it is read, never written."""
        return True

    @override
    def readinto(self, buffer: Buffer, /) -> int:
        """Fill ``buffer`` with the next decompressed bytes; return how many."""
        view = memoryview(buffer).cast("B")
        try:
            data = self._source.read(len(view))
        except (self._failure, EOFError) as error:
            raise ValueError(str(error)) from None
        view[: len(data)] = data
        return len(data)

    @override
    def close(self) -> None:
        """Close the library's reader, and the file with it."""
        self._source.close()
        super().close()


class _Source(Protocol):
    """A library's reader of a compressed file, giving its bytes decompressed."""

    def read(self, size: int, /) -> bytes: ...

    def close(self) -> None: ...


class _StdlibZstd(Protocol):
    """The part of Python 3.14's ``compression.zstd`` this module calls."""

    ZstdError: type[Exception]

    def compress(self, data: bytes, /, *, level: int) -> bytes: ...

    def decompress(self, data: bytes, /) -> bytes: ...

    def open(self, file: Path, /) -> _Source: ...
