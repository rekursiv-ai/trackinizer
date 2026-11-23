"""Tests for the ``code`` verb: what it reads, what it files, and what it refuses.

The store itself is tested in ``server/api/code_routes_test.py``; what matters here is the
verb's own arithmetic. A snapshot is a WHOLE map of path to blob hash, so every mutation
reads the current map, changes one path and files it again -- these check that the map it
files is the one it should, that a blob already stored is not sent twice, and that the
flags a caller did not pass keep the values the snapshot already had.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import hashlib
import uuid

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import cli
from trackinizer.wire import wire_code


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.client.client import Client


RECORD = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")
OWNER = "me@acme.co"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Store:
    """A code store in memory: the blobs filed per owner, and one snapshot per record."""

    def __init__(self, snapshot: object | None = None) -> None:
        self.blobs: dict[str, str] = {}
        self.snapshot = snapshot
        self.puts: list[dict[str, object]] = []
        self.blob_puts: list[dict[str, str]] = []
        self.asked: list[list[str]] = []

    def my_email(self) -> str:
        return OWNER

    def code_snapshot(self, record_id: uuid.UUID) -> object | None:
        del record_id
        return self.snapshot

    def code_missing(self, owner: str, *, hashes: Sequence[str]) -> list[str]:
        del owner
        self.asked.append(list(hashes))
        return [sha for sha in hashes if sha not in self.blobs]

    def code_put_blobs(self, owner: str, *, blobs: Mapping[str, str]) -> int:
        del owner
        self.blob_puts.append(dict(blobs))
        new = sum(1 for sha in blobs if sha not in self.blobs)
        self.blobs.update(blobs)
        return new

    def code_put_snapshot(
        self,
        record_id: uuid.UUID,
        *,
        owner: str,
        private: bool,
        parent_record_id: uuid.UUID | None,
        files: Mapping[str, str],
        partial: str = "",
    ) -> object:
        self.puts.append(
            {
                "record_id": record_id,
                "owner": owner,
                "private": private,
                "parent_record_id": parent_record_id,
                "files": dict(files),
                "partial": partial,
            },
        )
        self.snapshot = _snapshot(files, private=private, parent=parent_record_id, partial=partial)
        return self.snapshot

    def code_file(self, record_id: uuid.UUID, *, path: str) -> object | None:
        del record_id
        files = {f.path: f.sha256 for f in getattr(self.snapshot, "files", [])}
        if path not in files:
            return None
        return _File(path=path, sha256=files[path], text=self.blobs.get(files[path], ""))

    def code_diff(self, from_record: uuid.UUID, to_record: uuid.UUID, *, context: int) -> object | None:
        self.diff_asked = (from_record, to_record, context)
        return _Diff(files=[_FileDiff(path="a.ts", change="modified", diff="@@ -1 +1 @@\n-a\n+b\n")], more=False)


class _File:
    def __init__(self, *, path: str, sha256: str, text: str) -> None:
        self.path, self.sha256, self.text = path, sha256, text


class _SnapFile:
    def __init__(self, path: str, sha256: str, size: int) -> None:
        self.path, self.sha256, self.size = path, sha256, size


class _FileDiff:
    def __init__(self, *, path: str, change: str, diff: str) -> None:
        self.path, self.change, self.diff = path, change, diff


class _Diff:
    def __init__(self, *, files: list[_FileDiff], more: bool) -> None:
        self.files, self.more = files, more


class _Snapshot:
    def __init__(
        self,
        *,
        files: list[_SnapFile],
        private: bool,
        parent_record_id: uuid.UUID | None,
        partial: str,
    ) -> None:
        self.files = files
        self.private = private
        self.parent_record_id = parent_record_id
        self.partial = partial
        self.file_count = len(files)
        self.total_bytes = sum(f.size for f in files)


def _snapshot(
    files: Mapping[str, str],
    *,
    private: bool = True,
    parent: uuid.UUID | None = None,
    partial: str = "",
) -> _Snapshot:
    return _Snapshot(
        files=[_SnapFile(path, sha, len(path)) for path, sha in sorted(files.items())],
        private=private,
        parent_record_id=parent,
        partial=partial,
    )


def _run(store: _Store, *args: str) -> None:
    cli.parse_and_run(["code", *args], client_factory=lambda: cast("Client", store))


def test_a_write_files_the_whole_map_with_the_one_path_changed() -> None:
    """A snapshot is replaced, not patched, so an untouched path must survive the write."""
    kept = _sha("old")
    store = _Store(_snapshot({"keep.ts": kept}))
    store.blobs[kept] = "old"

    _run(store, str(RECORD), "write", "src/app.ts", "to", "hello\n")

    assert store.puts[-1]["files"] == {"keep.ts": kept, "src/app.ts": _sha("hello\n")}
    assert store.puts[-1]["record_id"] == RECORD
    assert store.puts[-1]["owner"] == OWNER


def test_a_blob_the_store_already_holds_is_not_sent_again() -> None:
    """Content addressing is the point: rewriting a file to its own bytes uploads nothing."""
    text = "hello\n"
    store = _Store(_snapshot({"a.ts": _sha(text)}))
    store.blobs[_sha(text)] = text

    _run(store, str(RECORD), "write", "a.ts", "to", text)

    assert store.asked == [[_sha(text)]]
    assert store.blob_puts == []
    # The map is still filed, so a no-op write is still a write.
    assert store.puts[-1]["files"] == {"a.ts": _sha(text)}


def test_a_first_write_against_a_record_with_no_snapshot_starts_an_empty_map() -> None:
    """No snapshot is not an error; it is the empty tree a first import writes into."""
    store = _Store(None)
    _run(store, str(RECORD), "write", "a.ts", "to", "x\n")
    assert store.puts[-1]["files"] == {"a.ts": _sha("x\n")}
    assert store.puts[-1]["private"] is True


def test_del_drops_one_path_and_leaves_the_rest() -> None:
    """The blob stays in the store; only the map loses the path."""
    store = _Store(_snapshot({"a.ts": _sha("a"), "b.ts": _sha("b")}))
    _run(store, str(RECORD), "del", "a.ts")
    assert store.puts[-1]["files"] == {"b.ts": _sha("b")}


def test_del_of_an_absent_path_is_refused_rather_than_filing_the_same_map() -> None:
    """Silently refiling an unchanged map would report success for a delete that did nothing."""
    store = _Store(_snapshot({"a.ts": _sha("a")}))
    with pytest.raises(ClientError, match="no file at 'gone.ts'"):
        _run(store, str(RECORD), "del", "gone.ts")
    assert store.puts == []


def test_del_against_a_record_with_no_code_says_so() -> None:
    store = _Store(None)
    with pytest.raises(ClientError, match="no code is filed"):
        _run(store, str(RECORD), "del", "a.ts")


def test_a_write_keeps_the_privacy_and_parent_the_snapshot_already_had() -> None:
    """Flags not passed must not silently reset a snapshot's state."""
    store = _Store(_snapshot({"a.ts": _sha("a")}, private=False, parent=OTHER, partial="cut short"))
    _run(store, str(RECORD), "write", "b.ts", "to", "b")
    put = store.puts[-1]
    assert put["private"] is False
    assert put["parent_record_id"] == OTHER
    assert put["partial"] == "cut short"


def test_public_opens_a_snapshot_and_parent_names_what_it_builds_on() -> None:
    store = _Store(_snapshot({"a.ts": _sha("a")}))
    _run(store, str(RECORD), "write", "b.ts", "to", "b", "--public", "--parent", str(OTHER))
    assert store.puts[-1]["private"] is False
    assert store.puts[-1]["parent_record_id"] == OTHER


def test_owner_defaults_to_the_caller_and_a_flag_overrides_it() -> None:
    """The store refuses an owner that is not the caller's unless they hold staff."""
    store = _Store(None)
    _run(store, str(RECORD), "write", "a.ts", "to", "x")
    assert store.puts[-1]["owner"] == OWNER

    _run(store, str(RECORD), "write", "b.ts", "to", "y", "--owner", "other@acme.co")
    assert store.puts[-1]["owner"] == "other@acme.co"


def test_read_prints_the_file_with_no_newline_added(capsys: pytest.CaptureFixture[str]) -> None:
    """A file must round-trip byte for byte; an echoed newline would corrupt it."""
    store = _Store(_snapshot({"a.ts": _sha("x = 1")}))
    store.blobs[_sha("x = 1")] = "x = 1"
    _run(store, str(RECORD), "read", "a.ts")
    assert capsys.readouterr().out == "x = 1"


def test_read_of_an_absent_path_is_an_error_not_an_empty_file() -> None:
    store = _Store(_snapshot({"a.ts": _sha("a")}))
    with pytest.raises(ClientError, match="no file at 'gone.ts'"):
        _run(store, str(RECORD), "read", "gone.ts")


def test_listing_names_the_files_with_their_sizes(capsys: pytest.CaptureFixture[str]) -> None:
    store = _Store(_snapshot({"a.ts": _sha("a"), "b/c.py": _sha("c")}))
    _run(store, str(RECORD))
    out = capsys.readouterr().out
    assert "2 files" in out
    assert "a.ts" in out
    assert "b/c.py" in out


def test_listing_a_record_with_no_code_says_so_rather_than_printing_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty listing and an unfiled record must not look alike."""
    _run(_Store(None), str(RECORD))
    assert "No code is filed" in capsys.readouterr().out


def test_diff_asks_for_the_two_records_in_the_order_given(capsys: pytest.CaptureFixture[str]) -> None:
    store = _Store(_snapshot({"a.ts": _sha("a")}))
    _run(store, str(RECORD), "diff", str(OTHER))
    assert store.diff_asked == (RECORD, OTHER, wire_code.DEFAULT_DIFF_CONTEXT)
    assert "modified" in capsys.readouterr().out


@pytest.mark.parametrize(
    "path",
    ["/abs.ts", "../escape.ts", "a/../b.ts", "a//b.ts", "./a.ts", ""],
)
def test_a_path_the_store_would_reject_is_refused_before_any_request(path: str) -> None:
    """Refused locally, so a bad path never reaches the store as a 422."""
    store = _Store(None)
    with pytest.raises(ClientError):
        _run(store, str(RECORD), "write", path, "to", "x")
    assert store.puts == []
    assert store.blob_puts == []


def test_a_file_over_the_blob_limit_is_refused_before_it_is_sent() -> None:
    """One byte over is a 422; the verb says so rather than spending the request."""
    store = _Store(None)
    with pytest.raises(ClientError, match="over the"):
        _run(store, str(RECORD), "write", "big.txt", "to", "x" * (wire_code.MAX_BLOB_BYTES + 1))
    assert store.blob_puts == []


def test_a_record_that_is_not_a_uuid_is_named_in_the_error() -> None:
    with pytest.raises(ClientError, match="must be a record UUID"):
        _run(_Store(None), "not-a-uuid")


def test_an_unknown_action_lists_the_ones_there_are() -> None:
    with pytest.raises(ClientError, match="unknown code action"):
        _run(_Store(None), str(RECORD), "frobnicate")


def test_write_without_the_to_keyword_is_refused() -> None:
    """`write PATH TEXT` would otherwise file a path named after the text."""
    with pytest.raises(ClientError, match="write takes PATH to"):
        _run(_Store(None), str(RECORD), "write", "a.ts", "hello")


def test_a_write_whose_text_is_not_text_is_refused() -> None:
    """The store holds text; a NUL would be rejected on the wire."""
    with pytest.raises(ClientError, match="NUL"):
        _run(_Store(None), str(RECORD), "write", "a.bin", "to", "a\0b")


def test_the_code_verb_is_reachable_from_the_cli_and_its_help_renders(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The verb is registered, which is what puts `code` in the generated grammar."""
    cli.parse_and_run(["code", "help"], client_factory=lambda: cast("Client", _Store(None)))
    out = capsys.readouterr().out
    assert "trax code RECORD_UUID" in out
    for action in ("read", "write", "del", "diff"):
        assert action in out


def test_write_reads_its_text_from_a_file(tmp_path: Any) -> None:
    """`to @FILE` keeps the file's trailing newline, which is part of the file."""
    source = tmp_path / "src.ts"
    source.write_text("export const a = 1;\n")
    store = _Store(None)
    _run(store, str(RECORD), "write", "a.ts", "to", f"@{source}")
    assert store.blob_puts[-1][_sha("export const a = 1;\n")] == "export const a = 1;\n"
