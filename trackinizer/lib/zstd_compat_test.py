"""Tests for trackinizer.lib.zstd_compat."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import importlib
import re
import sys

import pytest
import zstandard

from trackinizer.lib import zstd_compat


try:
    zstd = cast("zstd_compat._StdlibZstd", importlib.import_module("compression.zstd"))
except ImportError:
    # Python < 3.14, where exported packages run this file: the wheel stands in
    # for the stdlib so the stdlib branch still runs, and its 100% floor holds.
    zstd = cast(
        "zstd_compat._StdlibZstd",
        SimpleNamespace(
            ZstdError=ValueError,
            compress=zstandard.compress,
            decompress=zstd_compat._decompress_frames,
        ),
    )


_PAYLOAD = b"a durable zstandard payload" * 20
# ``b"hello world " * 1000`` as one streaming-mode frame: no content size in
# the header, the shape Cloudflare serves. One-shot ``compress`` cannot mint it.
_STREAMING_FRAME = bytes.fromhex(
    "28b52ffd0058a500006068656c6c6f20776f726c64200100d1ee7c4902",
)

# Each test runs on the stdlib and again with it hidden, as on Python 3.12.
pytestmark = pytest.mark.parametrize("wheel", [False, True], ids=["stdlib", "wheel"])


@pytest.fixture(autouse=True)
def _select_library(wheel: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zstd_compat, "zstd", None if wheel else zstd)


@pytest.mark.parametrize("payload", [b"", _PAYLOAD])
def test_round_trip(payload: bytes) -> None:
    assert zstd_compat.decompress(zstd_compat.compress(payload, level=7)) == payload


def test_level_is_forwarded() -> None:
    payload = b" ".join(str(i * i).encode() for i in range(2_000))

    assert len(zstd_compat.compress(payload, level=19)) < len(
        zstd_compat.compress(payload, level=1),
    )


def test_default_level_is_the_zstandard_default() -> None:
    assert zstd_compat.compress(_PAYLOAD) == zstd_compat.compress(_PAYLOAD, level=3)


def test_decodes_a_streaming_frame() -> None:
    assert not _STREAMING_FRAME[4] & 0b1110_0000  # No content size in the header.
    assert zstd_compat.decompress(_STREAMING_FRAME) == b"hello world " * 1000


@pytest.mark.parametrize("payload", [b"", _PAYLOAD])
def test_concatenated_frames_round_trip(payload: bytes) -> None:
    encoded = zstd_compat.compress(payload)

    assert zstd_compat.decompress(encoded + _STREAMING_FRAME + encoded) == (
        payload + b"hello world " * 1000 + payload
    )


@pytest.mark.parametrize("prefix_frames", [0, 1])
def test_truncated_frames_are_rejected(wheel: bool, prefix_frames: int) -> None:
    encoded = zstd_compat.compress(_PAYLOAD)
    message = r"^Incomplete zstandard frame$" if wheel else r".+"

    with pytest.raises(ValueError, match=message):
        zstd_compat.decompress(encoded * prefix_frames + encoded[:-1])


@pytest.mark.parametrize("data", [b"", b"not a zstandard frame"])
def test_malformed_input_is_a_value_error(data: bytes) -> None:
    """Callers catch one exception, whichever library decoded."""
    with pytest.raises(ValueError, match=r".+"):
        zstd_compat.decompress(data)


def test_library_error_message_is_preserved(wheel: bool) -> None:
    if wheel:
        with pytest.raises(zstandard.ZstdError) as raw:
            zstandard.ZstdDecompressor().decompressobj().decompress(b"junk")
    else:
        with pytest.raises(zstd.ZstdError) as raw:
            zstd.decompress(b"junk")

    with pytest.raises(ValueError, match=rf"^{re.escape(str(raw.value))}$"):
        zstd_compat.decompress(b"junk")


def test_wire_format_is_shared(wheel: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = zstd_compat.compress(_PAYLOAD)
    monkeypatch.setattr(zstd_compat, "zstd", zstd if wheel else None)

    assert zstd_compat.decompress(encoded) == _PAYLOAD


@pytest.mark.usefixtures("wheel")
def test_import_without_stdlib_zstd_selects_the_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 3.12 has no ``compression`` package; the module must still import."""
    # Both entries: ``import_module`` returns a cached submodule without its parent.
    monkeypatch.setitem(sys.modules, "compression", None)
    monkeypatch.setitem(sys.modules, "compression.zstd", None)
    try:
        _ = importlib.reload(zstd_compat)
        with pytest.raises(ValueError, match=r"^Incomplete zstandard frame$"):
            zstd_compat.decompress(_STREAMING_FRAME[:-1])
    finally:
        monkeypatch.undo()
        importlib.reload(zstd_compat)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
