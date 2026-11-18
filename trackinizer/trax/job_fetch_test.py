"""Download results: diff against what is held, verify each file, merge on success."""

from __future__ import annotations

from typing import IO, TYPE_CHECKING, NamedTuple, override

import hashlib
import io
import re
import shutil
import tarfile

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax.job_fetch import Fetched, IntegrityError, fetch
from trackinizer.trax.job_ssh import GateError
from trackinizer.wire.wire_job import ManifestEntry


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


def _entry(path: str, data: bytes, *, sealed: bool = True) -> ManifestEntry:
    digest = hashlib.sha256(data).hexdigest() if sealed else None
    return ManifestEntry(path=path, size=len(data), sha256=digest)


def _tar(*members: tarfile.TarInfo | tuple[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w|") as tar:
        for member in members:
            if isinstance(member, tuple):
                name, data = member
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(member)
    return out.getvalue()


class _Source:
    """Plays the node's download: writes a canned tar, then maybe fails."""

    def __init__(self, payload: bytes, *, then: Exception | None = None) -> None:
        self.payload = payload
        self.then = then
        self.asked: list[list[str]] = []

    def download(self, paths: Sequence[str], out: IO[bytes]) -> None:
        self.asked.append(list(paths))
        _ = out.write(self.payload)
        if self.then is not None:
            raise self.then


def _results(tmp_path: Path) -> Path:
    return tmp_path / "results"


def test_files_are_downloaded_verified_and_merged_under_out(tmp_path: Path) -> None:
    manifest = [_entry("a.txt", b"alpha"), _entry("deep/er/b.bin", b"\x00\x01")]
    source = _Source(_tar(("a.txt", b"alpha"), ("deep/er/b.bin", b"\x00\x01")))

    done = fetch(source, manifest, results=_results(tmp_path))

    assert (_results(tmp_path) / "out" / "a.txt").read_bytes() == b"alpha"
    assert (_results(tmp_path) / "out" / "deep" / "er" / "b.bin").read_bytes() == (
        b"\x00\x01"
    )
    assert source.asked == [["a.txt", "deep/er/b.bin"]]
    assert done == Fetched(downloaded=2, skipped=0, bytes=7)
    assert not (_results(tmp_path) / "out.partial").exists()


def test_a_file_already_held_with_the_same_size_and_digest_is_not_asked_for(
    tmp_path: Path,
) -> None:
    out = _results(tmp_path) / "out"
    out.mkdir(parents=True)
    (out / "kept.txt").write_bytes(b"same")
    (out / "stale.txt").write_bytes(b"olds")
    (out / "unsealed.txt").write_bytes(b"data")
    manifest = [
        _entry("kept.txt", b"same"),
        _entry("stale.txt", b"news"),
        _entry("unsealed.txt", b"data", sealed=False),
    ]
    source = _Source(_tar(("stale.txt", b"news"), ("unsealed.txt", b"data")))

    done = fetch(source, manifest, results=_results(tmp_path))

    assert source.asked == [["stale.txt", "unsealed.txt"]]
    assert (out / "stale.txt").read_bytes() == b"news"
    assert (done.downloaded, done.skipped) == (2, 1)


def test_nothing_is_asked_when_every_file_is_held(tmp_path: Path) -> None:
    out = _results(tmp_path) / "out"
    out.mkdir(parents=True)
    (out / "a.txt").write_bytes(b"x")
    source = _Source(b"")

    done = fetch(source, [_entry("a.txt", b"x")], results=_results(tmp_path))

    assert source.asked == []
    assert done == Fetched(downloaded=0, skipped=1, bytes=0)


def _symlink(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    return info


def _device(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.CHRTYPE
    return info


def _not_listed(name: str) -> str:
    return rf"^{re.escape(repr(name))} in the download is not in the manifest$"


_GOOD = ("good.txt", b"good")


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([_GOOD, ("../evil", b"zz")], _not_listed("../evil")),
        ([("../evil", b"zz")], _not_listed("../evil")),
        ([_GOOD, ("/etc/passwd", b"zz")], _not_listed("/etc/passwd")),
        ([_GOOD, ("sub/../../evil", b"zz")], _not_listed("sub/../../evil")),
        ([_GOOD, ("unlisted.txt", b"zz")], _not_listed("unlisted.txt")),
        ([_GOOD, ("good.txt", b"again")], r"^'good\.txt' is in the download twice$"),
        (
            [_GOOD, _symlink("link.txt")],
            r"^'link\.txt' in the download is not a regular file$",
        ),
        (
            [_symlink("link.txt")],
            r"^'link\.txt' in the download is not a regular file$",
        ),
        (
            [_GOOD, _device("dev.txt")],
            r"^'dev\.txt' in the download is not a regular file$",
        ),
    ],
    ids=[
        "dotdot",
        "dotdot-first",
        "absolute",
        "nested-dotdot",
        "unlisted",
        "duplicate",
        "symlink",
        "symlink-first",
        "device",
    ],
)
def test_a_dangerous_member_is_refused_and_nothing_is_merged(
    tmp_path: Path,
    members: list[tarfile.TarInfo | tuple[str, bytes]],
    message: str,
) -> None:
    manifest = [
        _entry("good.txt", b"good"),
        _entry("link.txt", b"x"),
        _entry("dev.txt", b"x"),
    ]
    source = _Source(_tar(*members))

    with pytest.raises(IntegrityError, match=message):
        _ = fetch(source, manifest, results=_results(tmp_path))

    assert not (_results(tmp_path) / "out").exists()
    assert not (_results(tmp_path) / "out.partial").exists()


class _Endless(_Source):
    """Keeps writing small pieces until the reader goes away."""

    @override
    def download(self, paths: Sequence[str], out: IO[bytes]) -> None:
        self.asked.append(list(paths))
        _ = out.write(self.payload)
        while True:
            _ = out.write(b"x" * 100)


def test_a_bad_member_stops_reading_and_the_sender_ends_without_a_stray_error(
    tmp_path: Path,
) -> None:
    source = _Endless(_tar(("../evil", b"z")))

    with pytest.raises(IntegrityError):
        _ = fetch(source, [_entry("a.txt", b"x")], results=_results(tmp_path))


def test_a_digest_mismatch_is_refused_and_nothing_is_merged(tmp_path: Path) -> None:
    manifest = [_entry("a.txt", b"alpha"), _entry("b.txt", b"beta")]
    source = _Source(_tar(("a.txt", b"alpha"), ("b.txt", b"BETA")))

    with pytest.raises(
        IntegrityError,
        match=r"^b\.txt does not match its sha256 in the manifest$",
    ):
        _ = fetch(source, manifest, results=_results(tmp_path))

    assert not (_results(tmp_path) / "out").exists()


def test_a_size_mismatch_is_refused_even_without_a_digest(tmp_path: Path) -> None:
    manifest = [_entry("a.txt", b"alpha", sealed=False)]
    source = _Source(_tar(("a.txt", b"alpha-longer")))

    with pytest.raises(
        IntegrityError,
        match=r"^a\.txt is 12 bytes, the manifest says 5$",
    ):
        _ = fetch(source, manifest, results=_results(tmp_path))

    assert not (_results(tmp_path) / "out").exists()


def test_a_member_larger_than_its_entry_is_refused_before_its_bytes_are_read(
    tmp_path: Path,
) -> None:
    # The header claims a gibibyte and the stream carries a sliver of it: only a
    # check of the header itself, not of the bytes written, sees the lie.
    header = tarfile.TarInfo("a.txt")
    header.size = 1 << 30
    source = _Source(header.tobuf(format=tarfile.USTAR_FORMAT) + b"a" * 1_000)

    with pytest.raises(
        IntegrityError,
        match=r"^a\.txt is 1073741824 bytes, the manifest says 5$",
    ):
        _ = fetch(source, [_entry("a.txt", b"alpha")], results=_results(tmp_path))

    assert not (_results(tmp_path) / "out.partial").exists()


def test_a_large_file_is_hashed_across_every_write(tmp_path: Path) -> None:
    data = bytes(range(256)) * 12_000
    manifest = [_entry("big.bin", data)]

    done = fetch(_Source(_tar(("big.bin", data))), manifest, results=_results(tmp_path))

    assert (_results(tmp_path) / "out" / "big.bin").read_bytes() == data
    assert done.bytes == len(data)


def test_an_interrupted_download_keeps_the_verified_files_and_resumes(
    tmp_path: Path,
) -> None:
    manifest = [_entry("a.txt", b"alpha"), _entry("b.txt", b"b" * 4_000)]
    whole = _tar(("a.txt", b"alpha"), ("b.txt", b"b" * 4_000))
    cut = whole[: 512 + 512 + 512 + 1_000]
    broken = _Source(cut, then=GateError("download", 255))

    with pytest.raises(GateError):
        _ = fetch(broken, manifest, results=_results(tmp_path))

    out = _results(tmp_path) / "out"
    assert (out / "a.txt").read_bytes() == b"alpha"
    assert not (out / "b.txt").exists()
    assert not (_results(tmp_path) / "out.partial").exists()

    again = _Source(_tar(("b.txt", b"b" * 4_000)))
    done = fetch(again, manifest, results=_results(tmp_path))

    assert again.asked == [["b.txt"]]
    assert (done.downloaded, done.skipped) == (1, 1)
    assert (out / "b.txt").read_bytes() == b"b" * 4_000


def test_a_node_that_fails_before_sending_anything_is_reported_as_it_failed(
    tmp_path: Path,
) -> None:
    source = _Source(b"", then=GateError("download", 255))

    with pytest.raises(GateError):
        _ = fetch(source, [_entry("a.txt", b"x")], results=_results(tmp_path))

    assert not (_results(tmp_path) / "out").exists()


def test_a_stream_that_stops_short_without_an_error_is_reported_as_cut(
    tmp_path: Path,
) -> None:
    whole = _tar(("a.txt", b"a" * 4_000))
    source = _Source(whole[: 512 + 1_000])

    with pytest.raises(ClientError, match=r"^the download ended early: ") as err:
        _ = fetch(source, [_entry("a.txt", b"a" * 4_000)], results=_results(tmp_path))

    assert not isinstance(err.value, IntegrityError)
    assert not (_results(tmp_path) / "out" / "a.txt").exists()


@pytest.mark.parametrize(
    ("sent", "message"),
    [
        (("a.txt",), r"^2 files were not sent: b\.txt$"),
        ((), r"^3 files were not sent: a\.txt$"),
        (("a.txt", "b.txt"), r"^1 file was not sent: c\.txt$"),
    ],
)
def test_files_the_node_never_sent_are_counted_after_the_others_merge(
    tmp_path: Path,
    sent: tuple[str, ...],
    message: str,
) -> None:
    names = ("a.txt", "b.txt", "c.txt")
    manifest = [_entry(name, name.encode()) for name in names]
    source = _Source(_tar(*((name, name.encode()) for name in sent)))

    with pytest.raises(ClientError, match=message):
        _ = fetch(source, manifest, results=_results(tmp_path))

    for name in sent:
        assert (_results(tmp_path) / "out" / name).read_bytes() == name.encode()


def test_leftovers_of_an_earlier_download_are_cleared(tmp_path: Path) -> None:
    stale = _results(tmp_path) / "out.partial" / "old.bin"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    _ = fetch(
        _Source(_tar(("a.txt", b"a"))),
        [_entry("a.txt", b"a")],
        results=_results(tmp_path),
    )

    assert not stale.parent.exists()


def test_the_results_directory_and_its_parents_are_made(tmp_path: Path) -> None:
    deep = tmp_path / "x" / "y" / "results"

    _ = fetch(_Source(_tar(("a.txt", b"a"))), [_entry("a.txt", b"a")], results=deep)

    assert (deep / "out" / "a.txt").read_bytes() == b"a"


def _free_disk(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    def disk_usage(path: object) -> _Usage:
        del path
        return _Usage(100, 100 - free, free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)


def test_too_little_disk_refuses_before_any_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _free_disk(monkeypatch, 4)
    source = _Source(b"")

    with pytest.raises(
        ClientError,
        match=r"^downloading needs 5 bytes of disk but 4 is free$",
    ):
        _ = fetch(source, [_entry("a.txt", b"alpha")], results=_results(tmp_path))

    assert source.asked == []


def test_exactly_enough_disk_is_enough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _free_disk(monkeypatch, 5)
    source = _Source(_tar(("a.txt", b"alpha")))

    done = fetch(source, [_entry("a.txt", b"alpha")], results=_results(tmp_path))

    assert done.downloaded == 1


def test_a_held_file_of_another_size_or_a_symlink_is_downloaded_again(
    tmp_path: Path,
) -> None:
    out = _results(tmp_path) / "out"
    out.mkdir(parents=True)
    (out / "short.txt").write_bytes(b"sh")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"right")
    (out / "link.txt").symlink_to(elsewhere)
    manifest = [_entry("short.txt", b"short"), _entry("link.txt", b"right")]
    source = _Source(_tar(("short.txt", b"short"), ("link.txt", b"right")))

    done = fetch(source, manifest, results=_results(tmp_path))

    assert source.asked == [["short.txt", "link.txt"]]
    assert (out / "short.txt").read_bytes() == b"short"
    assert not (out / "link.txt").is_symlink()
    assert done.skipped == 0


def test_a_path_of_one_character_is_a_path(tmp_path: Path) -> None:
    entry = ManifestEntry(path="a", size=1, sha256=None)

    done = fetch(_Source(_tar(("a", b"a"))), [entry], results=_results(tmp_path))

    assert done.downloaded == 1


def test_a_path_of_4096_characters_is_asked_for(tmp_path: Path) -> None:
    path = "x" * 4_096
    source = _Source(_tar())
    entry = ManifestEntry(path=path, size=1, sha256=None)

    with pytest.raises(ClientError, match=r"^1 file was not sent: "):
        _ = fetch(source, [entry], results=_results(tmp_path))

    assert source.asked == [[path]]


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs.txt",
        "../up.txt",
        "a/../b.txt",
        "./here.txt",
        "a/./b.txt",
        "a//b.txt",
        "trailing/",
        "line\nbreak",
        "nul\x00",
        "x" * 4_097,
    ],
)
def test_an_unsafe_manifest_path_is_refused_before_any_download(
    tmp_path: Path,
    path: str,
) -> None:
    source = _Source(b"")
    entry = ManifestEntry(path=path, size=1, sha256=None)

    with pytest.raises(ClientError, match=r"^unsafe path in the manifest: "):
        _ = fetch(source, [entry], results=_results(tmp_path))

    assert source.asked == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
