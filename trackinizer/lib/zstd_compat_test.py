"""Tests for trackinizer.lib.zstd_compat."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from trackinizer.lib import zstd_compat


_PAYLOAD = b"a durable zstandard payload" * 20


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
    backend.compress.assert_called_once_with(_PAYLOAD, 9)
    backend.decompress.assert_called_once_with(b"encoded")


def test_cross_backend_interoperability() -> None:
    stdlib = zstd_compat._stdlib_backend()
    package = zstd_compat._package_backend()
    if stdlib is None or package is None:
        pytest.skip("both zstandard backends are not importable")

    stdlib_wire = stdlib.compress(_PAYLOAD, 3)
    package_wire = package.compress(_PAYLOAD, 3)

    assert package.decompress(stdlib_wire) == _PAYLOAD
    assert stdlib.decompress(package_wire) == _PAYLOAD


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
