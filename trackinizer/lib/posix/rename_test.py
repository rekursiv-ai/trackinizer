"""Tests for renaming without replacing what is at the destination."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import ctypes
import errno
import os
import sys

import pytest

from trackinizer.lib.posix.rename import rename_noreplace


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_a_free_destination_takes_the_path(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    if kind == "file":
        source.write_text("ours\n")
    else:
        source.mkdir()
        (source / "inside").write_text("ours\n")
    destination = tmp_path / "destination"

    rename_noreplace(source, destination)

    assert not source.exists()
    if kind == "file":
        assert destination.read_text() == "ours\n"
    else:
        assert (destination / "inside").read_text() == "ours\n"


# Both are what os.rename replaces without a word: a file, and an empty directory
# when the source is one too.
@pytest.mark.parametrize("kind", ["file", "empty directory"])
def test_an_existing_destination_is_kept(tmp_path: Path, kind: str) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    if kind == "file":
        source.write_text("ours\n")
        destination.write_text("theirs\n")
    else:
        source.mkdir()
        (source / "inside").write_text("ours\n")
        destination.mkdir()

    with pytest.raises(FileExistsError) as refused:
        rename_noreplace(source, destination)

    assert refused.value.filename == destination
    assert source.exists()
    if kind == "file":
        assert destination.read_text() == "theirs\n"
    else:
        assert list(destination.iterdir()) == []


def test_windows_takes_its_own_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows' rename refuses an existing destination by itself, and its libc has
    # neither call, so the rename is the system's own there.
    renamed: list[tuple[Path, Path]] = []

    def rename(path: Path, target: Path) -> Path:
        renamed.append((path, target))
        return target

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(sys, "platform", "win32")

    rename_noreplace(tmp_path / "source", tmp_path / "destination")

    assert renamed == [(tmp_path / "source", tmp_path / "destination")]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows renames natively")
def test_a_system_without_the_call_says_enosys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_rename(name: str | None, *, use_errno: bool) -> object:
        del name, use_errno
        return object()

    monkeypatch.setattr(ctypes, "CDLL", no_rename)
    (tmp_path / "source").write_text("ours\n")

    with pytest.raises(OSError, match="renameat2 or renameatx_np") as refused:
        rename_noreplace(tmp_path / "source", tmp_path / "destination")

    assert refused.value.errno == errno.ENOSYS
    assert (tmp_path / "source").exists()


# A host runs only its own call for real, so each platform's arguments are pinned
# here, where Linux CI also checks the macOS ones.
@pytest.mark.parametrize(
    ("platform", "called", "directory_fd", "flags"),
    [("linux", "renameat2", -100, 1), ("darwin", "renameatx_np", -2, 4)],
)
def test_each_platform_passes_its_own_call_at_fdcwd_and_flag(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str,
    called: str,
    directory_fd: int,
    flags: int,
) -> None:
    library = _Library()

    def libc(name: str | None, *, use_errno: bool) -> _Library:
        assert (name, use_errno) == (None, True)
        return library

    monkeypatch.setattr(ctypes, "CDLL", libc)
    monkeypatch.setattr(sys, "platform", platform)

    rename_noreplace(Path("/a/source"), Path("/a/destination"))

    used = library.renameat2 if called == "renameat2" else library.renameatx_np
    unused = library.renameatx_np if called == "renameat2" else library.renameat2
    assert used.calls == [
        (
            directory_fd,
            os.fsencode("/a/source"),
            directory_fd,
            b"/a/destination",
            flags,
        ),
    ]
    assert unused.calls == []


@dataclass(slots=True, kw_only=True)
class _Call:
    """One libc rename call that records its arguments and succeeds."""

    calls: list[tuple[object, ...]] = field(default_factory=list[tuple[object, ...]])
    argtypes: tuple[object, ...] = ()
    restype: object = None

    def __call__(self, *arguments: object) -> int:
        self.calls.append(arguments)
        return 0


@dataclass(slots=True, kw_only=True)
class _Library:
    """libc as far as the rename uses it."""

    renameat2: _Call = field(default_factory=_Call)
    renameatx_np: _Call = field(default_factory=_Call)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
