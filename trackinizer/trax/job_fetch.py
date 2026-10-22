"""Download a job's result files straight from the node, and verify each one.

The manifest from the node lists every result file with its size and digest. Files
already held with the same size and digest are skipped, so an interrupted download
resumes by asking for the rest. The tar stream is read as it arrives into
``out.partial/``; each file is hashed while it is written and compared to the
manifest. A single bad member discards the whole download. A transport failure
keeps the files that verified.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import IO, TYPE_CHECKING, Protocol

import hashlib
import io
import os
import shutil
import tarfile

from trackinizer.client.errors import ClientError


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.wire.wire_job import ManifestEntry


__all__ = [
    "Fetched",
    "IntegrityError",
    "Source",
    "fetch",
]


class Source(Protocol):
    """Where result files come from: the node's ``download`` verb."""

    def download(self, paths: Sequence[str], out: IO[bytes]) -> None:
        """Write a tar stream of the named files to ``out``."""
        ...


class IntegrityError(ClientError):
    """The download holds something the manifest does not describe."""


@dataclass(frozen=True, kw_only=True, slots=True)
class Fetched:
    """What one download did."""

    downloaded: int
    skipped: int
    bytes: int


def fetch(
    source: Source,
    manifest: Sequence[ManifestEntry],
    *,
    results: Path,
) -> Fetched:
    """Download the files not yet held, verify them, and merge them into ``out/``.

    Args:
      source: The node's download verb.
      manifest: Every result file with its size and digest.
      results: The job's results directory; files land in its ``out/``.

    Returns:
      fetched: How many files came down, how many were already held, and bytes.

    Raises:
      IntegrityError: A member is not a regular manifest file, is repeated, or has
        the wrong size or digest. Nothing is merged.
      ClientError: A path is unsafe, the disk is too small, or the stream ended
        early. Files that verified before the stream ended are merged.

    """
    for entry in manifest:
        _check_path(entry.path)
    out = results / "out"
    wanted = [entry for entry in manifest if not _held(out / entry.path, entry)]
    need = sum(entry.size for entry in wanted)
    results.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(results).free
    if need > free:
        raise ClientError(
            f"downloading needs {need} bytes of disk but {free} is free",
        )
    skipped = len(manifest) - len(wanted)
    if not wanted:
        return Fetched(downloaded=0, skipped=skipped, bytes=0)
    partial = results / "out.partial"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        done, failure = _receive(
            source,
            {entry.path: entry for entry in wanted},
            partial,
        )
    except IntegrityError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    for name in done:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (partial / name).replace(out / name)
    shutil.rmtree(partial, ignore_errors=True)
    if failure is not None:
        raise failure
    return Fetched(
        downloaded=len(done),
        skipped=skipped,
        bytes=sum(entry.size for entry in wanted),
    )


def _receive(
    source: Source,
    expected: Mapping[str, ManifestEntry],
    partial: Path,
) -> tuple[list[str], Exception | None]:
    """Read the tar stream the source writes, returning what verified and why it ended."""
    read_fd, write_fd = os.pipe()
    reader, writer = os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "wb")
    sent: list[Exception] = []
    thread = Thread(target=_send, args=(source, list(expected), writer, sent))
    thread.start()
    done: set[str] = set()
    cut: tarfile.TarError | OSError | None = None
    try:
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            _extract(tar, expected, partial, done)
        # The tar ends before its record padding does; unread bytes would break the
        # pipe under the sender, which then reports a failure that never happened.
        with Path(os.devnull).open("wb") as nowhere:
            shutil.copyfileobj(reader, nowhere)
    except (tarfile.TarError, OSError) as err:
        cut = err
    finally:
        reader.close()
        thread.join()
    names = [name for name in expected if name not in done]
    if sent:
        return sorted(done), sent[0]
    if cut is not None:
        return sorted(done), ClientError(f"the download ended early: {cut}")
    if names:
        count = len(names)
        verb = "was" if count == 1 else "were"
        return sorted(done), ClientError(
            f"{count} file{'' if count == 1 else 's'} {verb} not sent: {names[0]}",
        )
    return sorted(done), None


def _send(
    source: Source,
    paths: Sequence[str],
    writer: IO[bytes],
    sent: list[Exception],
) -> None:
    """Run the download into the pipe and close it, noting any failure."""
    try:
        source.download(paths, writer)
    except Exception as err:  # noqa: BLE001 -- Handed to the reading thread, which re-raises it.
        sent.append(err)
    # The reader may have gone away first; its own error is the one worth reporting.
    with suppress(OSError):
        writer.close()


def _extract(
    tar: tarfile.TarFile,
    expected: Mapping[str, ManifestEntry],
    partial: Path,
    done: set[str],
) -> None:
    """Write each member under ``partial`` while hashing it, and check it."""
    for member in tar:
        name = member.name
        if not member.isreg():
            raise IntegrityError(f"{name!r} in the download is not a regular file")
        if name in done:
            raise IntegrityError(f"{name!r} is in the download twice")
        if (entry := expected.get(name)) is None:
            raise IntegrityError(f"{name!r} in the download is not in the manifest")
        # Checked on the header, before any byte lands: a node cannot fill the disk
        # the manifest's sizes were checked against. ``tarfile`` then yields exactly
        # this many bytes or raises.
        if member.size != entry.size:
            raise IntegrityError(
                f"{name} is {member.size} bytes, the manifest says {entry.size}",
            )
        source = tar.extractfile(member)
        assert isinstance(source, io.BufferedReader)
        target = partial / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as file:
            written = _Hashed(file)
            shutil.copyfileobj(source, written)
        if entry.sha256 is not None and written.digest.hexdigest() != entry.sha256:
            raise IntegrityError(f"{name} does not match its sha256 in the manifest")
        done.add(name)


class _Hashed:
    """A write-through file that hashes the bytes passing into it."""

    def __init__(self, file: IO[bytes]) -> None:
        self.digest = hashlib.sha256()
        self._file = file

    def write(self, data: bytes) -> int:
        """Pass ``data`` on, hashing it."""
        self.digest.update(data)
        return self._file.write(data)


def _held(path: Path, entry: ManifestEntry) -> bool:
    """Whether ``path`` already holds exactly the file the manifest describes."""
    if entry.sha256 is None or path.is_symlink() or not path.is_file():
        return False
    if path.stat().st_size != entry.size:
        return False
    with path.open("rb") as file:
        return hashlib.file_digest(file, hashlib.sha256).hexdigest() == entry.sha256


def _check_path(path: str) -> None:
    """Refuse a manifest path that is not a plain relative file path."""
    parts = path.split("/")
    if (
        len(path) == 0
        or len(path) > 4_096
        or "\n" in path
        or "\0" in path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ClientError(f"unsafe path in the manifest: {path!r}")
