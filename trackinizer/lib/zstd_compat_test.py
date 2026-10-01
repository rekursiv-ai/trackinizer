"""Tests for trackinizer.lib.zstd_compat."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import importlib

import pytest

from trackinizer.lib import zstd_compat


if TYPE_CHECKING:
    from collections.abc import Callable


_PAYLOAD = b"a durable zstandard payload" * 20


class _FailingDecoder:
    eof = False
    unused_data = b""

    def decompress(self, data: bytes) -> bytes:
        del data
        raise RuntimeError("corrupt frame")

    def decompressobj(self) -> _FailingDecoder:
        return self


class _FakeModule:
    ZstdError: type[Exception] = RuntimeError

    def __init__(self) -> None:
        self.ZstdDecompressor: Callable[[], zstd_compat._ZstdDecompressor] = (
            _FailingDecoder
        )
        self.calls: list[tuple[bytes, int]] = []

    def compress(self, data: bytes, level: int = 3) -> bytes:
        self.calls.append((data, level))
        return b"encoded"

    def decompress(self, data: bytes) -> bytes:
        return _FailingDecoder().decompress(data)


def test_round_trip() -> None:
    encoded = zstd_compat.compress(_PAYLOAD, level=7)

    assert zstd_compat.decompress(encoded) == _PAYLOAD


def test_backend_fallback_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Mock()
    backend.compress.return_value = b"encoded"
    backend.decompress.return_value = _PAYLOAD
    monkeypatch.setattr(zstd_compat, "_backend", lambda: backend)

    assert zstd_compat.compress(_PAYLOAD, level=9) == b"encoded"
    assert zstd_compat.decompress(b"encoded") == _PAYLOAD
    backend.compress.assert_called_once_with(_PAYLOAD, level=9)
    backend.decompress.assert_called_once_with(b"encoded")


@pytest.mark.parametrize(
    "backend_type",
    [zstd_compat._StdlibBackend, zstd_compat._PackageBackend],
)
@pytest.mark.parametrize("level", [None, 7])
def test_compression_level_is_forwarded(
    backend_type: type[zstd_compat._StdlibBackend | zstd_compat._PackageBackend],
    level: int | None,
) -> None:
    module = _FakeModule()

    assert backend_type(module).compress(_PAYLOAD, level=level) == b"encoded"
    assert module.calls == [(_PAYLOAD, 3 if level is None else level)]


@pytest.mark.parametrize(
    "backend_type",
    [zstd_compat._StdlibBackend, zstd_compat._PackageBackend],
)
def test_backend_error_message_is_preserved(
    backend_type: type[zstd_compat._StdlibBackend | zstd_compat._PackageBackend],
) -> None:
    module = _FakeModule()

    with pytest.raises(ValueError, match=r"^corrupt frame$"):
        backend_type(module).decompress(b"corrupt")


@pytest.mark.parametrize(
    "make",
    [zstd_compat._stdlib_backend, zstd_compat._package_backend],
)
def test_each_backend_round_trips_at_its_default_level(
    make: Callable[[], zstd_compat._Backend | None],
) -> None:
    backend = make()
    if backend is None:
        pytest.skip("backend not importable")

    assert backend.decompress(backend.compress(_PAYLOAD, level=None)) == _PAYLOAD


@pytest.mark.parametrize(
    "make",
    [zstd_compat._stdlib_backend, zstd_compat._package_backend],
)
def test_corrupt_input_is_a_value_error_on_every_backend(
    make: Callable[[], zstd_compat._Backend | None],
) -> None:
    """Callers catch one exception, whichever library decoded."""
    backend = make()
    if backend is None:
        pytest.skip("backend not importable")

    with pytest.raises(ValueError):  # noqa: PT011 -- the backends' messages differ.
        backend.decompress(b"not a zstandard frame")


@pytest.mark.parametrize(
    "make",
    [zstd_compat._stdlib_backend, zstd_compat._package_backend],
)
@pytest.mark.parametrize("prefix_frames", [0, 1])
def test_truncated_frames_are_rejected(
    make: Callable[[], zstd_compat._Backend | None],
    prefix_frames: int,
) -> None:
    backend = make()
    if backend is None:
        pytest.skip("backend not importable")
    encoded = backend.compress(_PAYLOAD, level=3)

    message = (
        "^Incomplete zstandard frame$"
        if make is zstd_compat._package_backend
        else r".+"
    )
    with pytest.raises(ValueError, match=message):
        backend.decompress(encoded * prefix_frames + encoded[:-1])


@pytest.mark.parametrize(
    "make",
    [zstd_compat._stdlib_backend, zstd_compat._package_backend],
)
def test_empty_compressed_input_is_rejected(
    make: Callable[[], zstd_compat._Backend | None],
) -> None:
    backend = make()
    if backend is None:
        pytest.skip("backend not importable")

    with pytest.raises(ValueError, match=r".+"):
        backend.decompress(b"")


@pytest.mark.parametrize(
    "make",
    [zstd_compat._stdlib_backend, zstd_compat._package_backend],
)
@pytest.mark.parametrize("payload", [b"", _PAYLOAD])
def test_concatenated_frames_round_trip(
    make: Callable[[], zstd_compat._Backend | None],
    payload: bytes,
) -> None:
    backend = make()
    if backend is None:
        pytest.skip("backend not importable")
    encoded = backend.compress(payload, level=3)

    assert backend.decompress(encoded + encoded) == payload * 2


@pytest.mark.parametrize(
    ("missing", "available"),
    [("compression.zstd", "_package_backend"), ("zstandard", "_stdlib_backend")],
)
def test_an_absent_library_yields_no_backend(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    available: str,
) -> None:
    module = Mock()

    def without(name: str) -> object:
        if name == missing:
            raise ImportError(name)
        return module

    monkeypatch.setattr(importlib, "import_module", without)
    absent = (
        zstd_compat._stdlib_backend
        if missing == "compression.zstd"
        else zstd_compat._package_backend
    )

    assert absent() is None
    assert getattr(zstd_compat, available)() is not None


def test_no_library_at_all_names_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zstd_compat, "_stdlib_backend", lambda: None)
    monkeypatch.setattr(zstd_compat, "_package_backend", lambda: None)
    zstd_compat._backend.cache_clear()
    try:
        with pytest.raises(ImportError, match="zstandard"):
            zstd_compat.compress(_PAYLOAD)
        with pytest.raises(ImportError, match="zstandard"):
            zstd_compat.decompress(_PAYLOAD)
    finally:
        zstd_compat._backend.cache_clear()


def test_cross_backend_interoperability() -> None:
    stdlib = zstd_compat._stdlib_backend()
    package = zstd_compat._package_backend()
    if stdlib is None or package is None:
        pytest.skip("both zstandard backends are not importable")

    stdlib_wire = stdlib.compress(_PAYLOAD, level=3)
    package_wire = package.compress(_PAYLOAD, level=3)

    assert package.decompress(stdlib_wire) == _PAYLOAD
    assert stdlib.decompress(package_wire) == _PAYLOAD


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
