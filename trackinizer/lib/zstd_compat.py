"""One zstandard wire-format interface for every supported Python version.

The monorepo runs Python 3.14 and can use ``compression.zstd`` from the
stdlib, while exported packages support Python 3.12 and must not depend on
that 3.14-only module; there the ``zstandard`` wheel decodes the same frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import importlib


# By name: a 3.12 checker has the stub but no source, so a static import fails
# ``reportMissingModuleSource`` in an export that keeps that check on.
try:
    zstd = cast("_StdlibZstd", importlib.import_module("compression.zstd"))
except ImportError:  # Python < 3.14.
    zstd = None
if TYPE_CHECKING:
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


class _StdlibZstd(Protocol):
    """The part of Python 3.14's ``compression.zstd`` this module calls."""

    ZstdError: type[Exception]

    def compress(self, data: bytes, /, *, level: int) -> bytes: ...

    def decompress(self, data: bytes, /) -> bytes: ...
