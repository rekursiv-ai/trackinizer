"""Tests for trackinizer.lib.zstd_compat."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, TextIO, cast

import importlib
import io
import os
import re
import sys

import pytest
import zstandard

from trackinizer.lib import zstd_compat


if TYPE_CHECKING:
    from pathlib import Path


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
            open=zstd_compat._Frames,
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
    # Varied enough that levels 3 and 4 encode it differently.
    data = " ".join(str(i * i % 1009) for i in range(500)).encode()

    assert zstd_compat.compress(data) == zstd_compat.compress(data, level=3)
    assert zstd_compat.compress(data) != zstd_compat.compress(data, level=4)


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


def test_open_text_reads_a_frame_larger_than_one_read(tmp_path: Path) -> None:
    """A rollout's frame spans many of the file's reads, then the next frame starts."""
    text = os.urandom(2 * io.DEFAULT_BUFFER_SIZE).hex() + "\n"
    path = tmp_path / "t.jsonl.zst"
    _ = path.write_bytes(zstd_compat.compress(text.encode()) + _STREAMING_FRAME)
    assert path.stat().st_size > 2 * io.DEFAULT_BUFFER_SIZE
    expected = text + "hello world " * 1000

    with zstd_compat.open_text(path, encoding="utf-8") as handle:
        assert _text(handle, most=len(expected)) == expected


def test_open_text_reads_every_frame_as_text(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl.zst"
    _ = path.write_bytes(zstd_compat.compress("\u00e9\r\n".encode()) + _STREAMING_FRAME)
    expected = "\u00e9\r\n" + "hello world " * 1000

    with zstd_compat.open_text(path, encoding="utf-8", newline="") as handle:
        assert _text(handle, most=len(expected)) == expected
    with zstd_compat.open_text(path, encoding="utf-8") as handle:
        assert handle.readline(len(expected)) == "\u00e9\n"


def test_open_text_spells_bytes_as_its_errors_say(tmp_path: Path) -> None:
    path = tmp_path / "t.zst"
    _ = path.write_bytes(zstd_compat.compress(b"a\xffb\n"))
    most = len(b"a\xffb\n")

    with zstd_compat.open_text(path, encoding="utf-8", errors="replace") as handle:
        assert _text(handle, most=most) == f"a{chr(0xFFFD)}b\n"
    with zstd_compat.open_text(path, encoding="latin-1") as handle:
        assert _text(handle, most=most) == f"a{chr(0xFF)}b\n"
    with (
        zstd_compat.open_text(path, encoding="utf-8") as handle,
        pytest.raises(UnicodeDecodeError),
    ):
        _ = _text(handle, most=most)


@pytest.mark.parametrize("data", ["junk", "truncated", "empty"])
def test_open_text_reads_a_corrupt_file_as_a_value_error(
    tmp_path: Path,
    wheel: bool,
    data: str,
) -> None:
    """Callers catch one exception, whichever library decoded, as ``decompress``'s do.

    An empty file holds no frame, which the stdlib refuses as a frame cut short.
    """
    path = tmp_path / "t.zst"
    _ = path.write_bytes(
        {
            "junk": b"not a zstandard frame",
            "truncated": zstd_compat.compress(_PAYLOAD)[:-1],
            "empty": b"",
        }[data],
    )
    message = r"^Incomplete zstandard frame$" if wheel and data != "junk" else r".+"

    with (
        zstd_compat.open_text(path, encoding="utf-8") as handle,
        pytest.raises(ValueError, match=message) as raised,
    ):
        _ = _text(handle, most=len(_PAYLOAD))
    assert type(raised.value) is ValueError


# Not ``handle.read()``: it trusts the reader to end, and one that never does, as a
# mutant that hands back the same bytes again, took 14 GB in 6 s, nearly all of a
# 16 GB CI runner, before any assertion could fail. Nor one ``read(most + 1)``: a
# size that large reaches the reader as one request bigger than any frame it
# decodes, so a reader that loses its place within a frame would go unseen.
def _text(handle: TextIO, *, most: int) -> str:
    """Return the rest of ``handle``, a buffer at a time, stopping past ``most`` chars."""
    text = ""
    while len(text) <= most and (part := handle.read(io.DEFAULT_BUFFER_SIZE)):
        text += part
    return text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
