"""Wire contract for the code store: each record's code, and any two records' diff.

A blob is one text file, named by the sha256 hex of its UTF-8 bytes and stored once
per owner; dedupe never crosses owners. A snapshot maps a record's paths to blob
hashes and names the record whose snapshot it builds on. An uploader asks which
blobs the store lacks (``CODE_MISSING_PATH``), sends those (``CODE_BLOBS_PATH``),
then files the snapshot (``CODE_SNAPSHOT_PATH``). A reader gets a snapshot, one file
of it, or the unified diff of two (``CODE_DIFF_PATH``). A private snapshot is read by
its owner alone, and an unreadable one answers 404 as a missing one does.

This package is part of the publishable client distribution, so it must not import
``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

import uuid

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


__all__ = [
    "CODE_API_PATHS",
    "CODE_BLOBS_PATH",
    "CODE_DIFF_PATH",
    "CODE_FILE_PATH",
    "CODE_MISSING_PATH",
    "CODE_SNAPSHOT_PATH",
    "DEFAULT_DIFF_CONTEXT",
    "MAX_BLOBS_PER_REQUEST",
    "MAX_BLOB_BYTES",
    "MAX_DIFF_CHARS",
    "MAX_DIFF_CONTEXT",
    "MAX_MISSING_HASHES",
    "MAX_PATH_CHARS",
    "MAX_REQUEST_TEXT_BYTES",
    "MAX_SNAPSHOT_FILES",
    "MISSING_BLOBS_CODE",
    "NO_PARENT_SNAPSHOT_CODE",
    "SHA256_PATTERN",
    "CodeBlob",
    "CodeBlobsPut",
    "CodeBlobsStored",
    "CodeDiff",
    "CodeFile",
    "CodeFileDiff",
    "CodeMissing",
    "CodeMissingRequest",
    "CodeSnapshot",
    "CodeSnapshotFile",
    "CodeSnapshotHead",
    "CodeSnapshotPut",
]


CODE_MISSING_PATH: Final = "/api/code/blobs/missing"
CODE_BLOBS_PATH: Final = "/api/code/blobs"
CODE_SNAPSHOT_PATH: Final = "/api/code/records/{record_id}/snapshot"
CODE_FILE_PATH: Final = "/api/code/records/{record_id}/file"
CODE_DIFF_PATH: Final = "/api/code/diff"
CODE_API_PATHS: Final = (
    CODE_MISSING_PATH,
    CODE_BLOBS_PATH,
    CODE_SNAPSHOT_PATH,
    CODE_FILE_PATH,
    CODE_DIFF_PATH,
)
"""Registry the route drift test checks against the live app and the docs."""

SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
"""A blob's name: the lowercase sha256 hex of its UTF-8 bytes."""

MAX_BLOB_BYTES: Final = 1_048_576
"""Largest blob, in UTF-8 bytes."""

MAX_REQUEST_TEXT_BYTES: Final = 8 * MAX_BLOB_BYTES
"""Most UTF-8 bytes of text one ``CODE_BLOBS_PATH`` request carries."""

MAX_BLOBS_PER_REQUEST: Final = 1_000
"""Most blobs one ``CODE_BLOBS_PATH`` request carries."""

MAX_MISSING_HASHES: Final = 5_000
"""Most hashes one ``CODE_MISSING_PATH`` request asks about."""

MAX_SNAPSHOT_FILES: Final = 20_000
"""Most files one snapshot maps."""

MAX_PATH_CHARS: Final = 1_024
"""Longest path in a snapshot."""

MAX_DIFF_CHARS: Final = 2_000_000
"""Most characters of diff text one ``CODE_DIFF_PATH`` answer carries."""

DEFAULT_DIFF_CONTEXT: Final = 3
MAX_DIFF_CONTEXT: Final = 50
"""Most lines of context around each change a diff asks for."""

MISSING_BLOBS_CODE: Final = "code_blobs_missing"
"""The 409 a snapshot gets when one of its blobs is not stored for its owner."""

NO_PARENT_SNAPSHOT_CODE: Final = "code_parent_missing"
"""The 409 a snapshot gets when its parent record has no snapshot of its owner."""


def _lowered(value: str) -> str:
    return value.lower()


Owner = Annotated[str, Field(min_length=1), AfterValidator(_lowered)]
"""The account email a blob or snapshot is filed under, compared lowercase."""

Sha256Hex = Annotated[str, Field(pattern=SHA256_PATTERN)]


class CodeMissingRequest(BaseModel):
    """Ask which of ``sha256`` the store lacks for ``owner``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: Owner
    sha256: list[Sha256Hex]
    """At most ``MAX_MISSING_HASHES``."""


class CodeMissing(BaseModel):
    """The hashes the store lacks for the owner, each once, in the order asked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    missing: list[Sha256Hex]


class CodeBlob(BaseModel):
    """One file's text and the sha256 hex of its UTF-8 bytes, which the server checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: Sha256Hex
    text: str
    """At most ``MAX_BLOB_BYTES`` in UTF-8."""


class CodeBlobsPut(BaseModel):
    """Store blobs for ``owner``; one already stored is a no-op."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: Owner
    blobs: list[CodeBlob]
    """At most ``MAX_BLOBS_PER_REQUEST``, of at most ``MAX_REQUEST_TEXT_BYTES``."""


class CodeBlobsStored(BaseModel):
    """How many of the blobs sent were new to the store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stored: int


class CodeSnapshotPut(BaseModel):
    """File a record's code, replacing the snapshot it had."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: Owner
    private: bool
    """Read by ``owner`` alone."""

    parent_record_id: uuid.UUID | None
    """The record whose snapshot this one builds on; it must hold one of ``owner``."""

    files: dict[str, Sha256Hex]
    """Path to blob hash. A path is relative POSIX, with no empty, ``.`` or ``..``
    segment and no NUL, of at most ``MAX_PATH_CHARS``; at most
    ``MAX_SNAPSHOT_FILES`` paths, each blob stored for ``owner``."""

    partial: str = ""
    """One line saying what the uploader left out, when it stopped short."""


class CodeSnapshotHead(BaseModel):
    """A snapshot without its files. The owner's email is never sent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: uuid.UUID
    parent_record_id: uuid.UUID | None
    private: bool
    file_count: int
    total_bytes: int
    """The UTF-8 bytes of every file, a blob mapped twice counted twice."""

    partial: str
    updated_at: datetime


class CodeSnapshotFile(BaseModel):
    """One path of a snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: Sha256Hex
    size: int
    """UTF-8 bytes."""


class CodeSnapshot(CodeSnapshotHead):
    """A snapshot and its files, sorted by path."""

    files: list[CodeSnapshotFile]


class CodeFile(BaseModel):
    """One file of a snapshot, with its text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: Sha256Hex
    text: str


class CodeFileDiff(BaseModel):
    """How one path differs between two snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    change: Literal["added", "modified", "deleted"]
    old_sha256: Sha256Hex | None
    """``None`` for an added file."""

    new_sha256: Sha256Hex | None
    """``None`` for a deleted file."""

    diff: str
    """A unified diff headed ``--- a/<path>`` and ``+++ b/<path>``, whatever the
    change. A last line with no newline is followed by
    ``\\ No newline at end of file``."""


class CodeDiff(BaseModel):
    """The paths that differ between two snapshots, sorted, with their diffs.

    The diffs hold at most ``MAX_DIFF_CHARS`` characters in all. The file that would
    pass it is cut after its last whole line inside the bound, and no later file is
    listed; ``more`` then says so.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    files: list[CodeFileDiff]
    more: bool
