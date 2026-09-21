"""One zstandard wire-format interface for every supported Python version.

The monorepo runs Python 3.14 and can use ``compression.zstd`` from the
stdlib, while exported packages support Python 3.12 and must not depend on
that 3.14-only module.  Backend selection happens once, memoized on first
use, so callers use the same API and wire format in either environment.
"""

from __future__ import annotations

from functools import cache
from io import BytesIO
from typing import TYPE_CHECKING, Protocol, cast


if TYPE_CHECKING:
    from collections.abc import Callable

import importlib


class _CompressionModule(Protocol):
    ZstdError: type[Exception]

    def compress(self, data: bytes, level: int = ...) -> bytes: ...

    def decompress(self, data: bytes) -> bytes: ...


class _Reader(Protocol):
    def read(self) -> bytes: ...


class _ZstdDecompressor(Protocol):
    def stream_reader(self, source: BytesIO) -> _Reader: ...


class _ZstandardModule(_CompressionModule, Protocol):
    ZstdDecompressor: Callable[[], _ZstdDecompressor]


class _Backend(Protocol):
    def compress(self, data: bytes, level: int | None) -> bytes: ...

    def decompress(self, data: bytes) -> bytes: ...


def compress(data: bytes, *, level: int | None = None) -> bytes:
    """Compress ``data`` as a zstandard frame."""
    return _backend().compress(data, level)


def decompress(data: bytes) -> bytes:
    """Decompress one or more zstandard frames from ``data``.

    Args:
      data: Zstandard-compressed bytes.

    Returns:
      result: The decompressed bytes.

    """
    return _backend().decompress(data)


class _StdlibBackend:
    def __init__(self, module: _CompressionModule) -> None:
        self._module = module

    def compress(self, data: bytes, level: int | None) -> bytes:
        if level is None:
            return self._module.compress(data)
        return self._module.compress(data, level)

    def decompress(self, data: bytes) -> bytes:
        try:
            return self._module.decompress(data)
        except self._module.ZstdError as error:
            raise ValueError(str(error)) from None


class _PackageBackend:
    def __init__(self, module: _ZstandardModule) -> None:
        self._module = module

    def compress(self, data: bytes, level: int | None) -> bytes:
        if level is None:
            return self._module.compress(data)
        return self._module.compress(data, level)

    def decompress(self, data: bytes) -> bytes:
        try:
            reader = self._module.ZstdDecompressor().stream_reader(BytesIO(data))
            return reader.read()
        except self._module.ZstdError as error:
            raise ValueError(str(error)) from None


def _stdlib_backend() -> _Backend | None:
    try:
        module = cast(_CompressionModule, importlib.import_module("compression.zstd"))
    except ImportError:
        return None
    return _StdlibBackend(module)


def _package_backend() -> _Backend | None:
    try:
        module = cast(
            _ZstandardModule,
            cast(object, importlib.import_module("zstandard")),
        )
    except ImportError:
        return None
    return _PackageBackend(module)


class _MissingBackend:
    def compress(self, data: bytes, level: int | None) -> bytes:
        del data, level
        raise ImportError("zstandard support requires Python 3.14 or zstandard")

    def decompress(self, data: bytes) -> bytes:
        del data
        raise ImportError("zstandard support requires Python 3.14 or zstandard")


@cache
def _backend() -> _Backend:
    """Select and memoize the available zstandard backend."""
    return _stdlib_backend() or _package_backend() or _MissingBackend()
