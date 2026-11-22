"""Unit tests for snapshot decisions and owner-keyed blob reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import hashlib

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid, set_field_row
from trackinizer.lib import zstd_compat
from trackinizer.server.audience import NOBODY
from trackinizer.server.store import code_store
from trackinizer.server.store.code_store import (
    MissingBlobsError,
    NoParentSnapshotError,
    SnapshotOwnerError,
    diff_snapshots,
    put_snapshot,
    read_file,
    read_snapshot,
)
from trackinizer.types.errors import NotFoundError, ValidationError
from trackinizer.wire.wire_code import CodeSnapshotPut


if TYPE_CHECKING:
    from uuid import UUID


@pytest.mark.asyncio
async def test_snapshot_totals_count_duplicate_blobs_per_path() -> None:
    conn = make_conn()
    conn.fetchval.side_effect = [True, "ada@example.com"]
    conn.fetch.return_value = [{"sha256": bytes.fromhex(_sha("é")), "size": 2}]
    record, parent, creator = new_uuid(), new_uuid(), new_uuid()
    set_field_row(conn, _head(record, parent=parent))
    store, _ = make_store(conn)
    head = await put_snapshot(
        store.engine,
        record_id=record,
        body=CodeSnapshotPut(
            owner="ada@example.com",
            private=False,
            parent_record_id=parent,
            files={"a": _sha("é"), "b": _sha("é")},
            partial="partial",
        ),
        created_by=creator,
        replace_others=True,
    )
    assert (head.record_id, head.parent_record_id) == (record, parent)
    call = conn.fetchrow.call_args_list[0]
    assert call.args[1:] == (
        record,
        parent,
        "ada@example.com",
        False,
        {"a": _sha("é"), "b": _sha("é")},
        2,
        4,
        "partial",
        creator,
        True,
    )
    assert "WHERE code_snapshots.owner = EXCLUDED.owner OR $10" in str(call.args[0])
    assert [str(c.args[0]) for c in conn.execute.call_args_list] == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["self", "missing", "parent", "blobs", "owner"])
async def test_snapshot_refusal_never_commits(failure: str) -> None:
    conn = make_conn()
    conn.fetchval.side_effect = [failure != "missing", "other@example.com"]
    conn.fetch.return_value = (
        []
        if failure == "blobs"
        else [
            {"sha256": bytes.fromhex(_sha("x")), "size": 1},
        ]
    )
    record = new_uuid()
    parent = (
        record if failure == "self" else new_uuid() if failure == "parent" else None
    )
    store, _ = make_store(conn)
    errors = {
        "self": ValidationError,
        "missing": NotFoundError,
        "parent": NoParentSnapshotError,
        "blobs": MissingBlobsError,
        "owner": SnapshotOwnerError,
    }
    conn.reset_mock()
    with pytest.raises(errors[failure]):
        await put_snapshot(
            store.engine,
            record_id=record,
            body=CodeSnapshotPut(
                owner="ada@example.com",
                private=False,
                parent_record_id=parent,
                files={"a": _sha("x")},
            ),
            created_by=None,
            replace_others=False,
        )
    assert "COMMIT" not in [str(c.args[0]) for c in conn.execute.call_args_list]
    if failure == "self":
        assert conn.mock_calls == []
    else:
        conn.fetch.assert_any_await("ROLLBACK")


@pytest.mark.asyncio
async def test_snapshot_read_sorts_paths_and_uses_owner_scoped_sizes() -> None:
    conn = make_conn()
    record = new_uuid()
    set_field_row(
        conn,
        {
            **_head(record),
            "owner": "ada@example.com",
            "files": {"z": _sha("é"), "a": _sha("é")},
        },
    )
    conn.fetch.return_value = [{"sha256": bytes.fromhex(_sha("é")), "size": 2}]
    store, _ = make_store(conn)
    snapshot = await read_snapshot(store.engine, record_id=record, reader=NOBODY)
    assert snapshot is not None
    assert [(f.path, f.sha256, f.size) for f in snapshot.files] == [
        ("a", _sha("é"), 2),
        ("z", _sha("é"), 2),
    ]
    assert conn.fetch.call_args_list[0].args[1:] == (
        "ada@example.com",
        [bytes.fromhex(_sha("é"))],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unreadable", "absent", "present"])
async def test_file_read_fetches_only_a_readable_mapped_path(case: str) -> None:
    conn = make_conn()
    record = new_uuid()
    set_field_row(
        conn,
        None
        if case == "unreadable"
        else {
            **_head(record),
            "owner": "ada@example.com",
            "files": {"a": _sha("é")},
        },
    )
    conn.fetch.return_value = [
        {
            "owner": "ada@example.com",
            "sha256": bytes.fromhex(_sha("é")),
            "data_zst": zstd_compat.compress("é".encode()),
        },
    ]
    store, _ = make_store(conn)
    found = await read_file(
        store.engine,
        record_id=record,
        path="b" if case == "absent" else "a",
        reader=NOBODY,
    )
    if case == "present":
        assert found is not None
        assert (found.path, found.sha256, found.text) == ("a", _sha("é"), "é")
        assert conn.fetch.call_args_list[0].args[1:] == (
            ["ada@example.com"],
            [bytes.fromhex(_sha("é"))],
        )
    else:
        assert found is None
        conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_snapshot_and_diff_return_none_without_blob_reads() -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    assert (
        await read_snapshot(store.engine, record_id=new_uuid(), reader=NOBODY) is None
    )
    assert (
        await diff_snapshots(
            store.engine,
            from_id=new_uuid(),
            to_id=new_uuid(),
            context=0,
            reader=NOBODY,
        )
        is None
    )
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [False, True])
async def test_diff_fetches_owner_hash_pairs_and_reports_a_cut(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cut: bool,
) -> None:
    conn = make_conn()
    old, new = new_uuid(), new_uuid()
    conn.field_queue.extend(
        [
            {
                **_head(old),
                "owner": "ada@example.com",
                "files": {"a": _sha("old\n"), "gone": _sha("old\n")},
            },
            {
                **_head(new),
                "owner": "bob@example.com",
                "files": {"a": _sha("new\n"), "new": _sha("new\n")},
            },
        ],
    )
    conn.fetch.return_value = [
        {
            "owner": owner,
            "sha256": bytes.fromhex(_sha(text)),
            "data_zst": zstd_compat.compress(text.encode()),
        }
        for owner, text in [("ada@example.com", "old\n"), ("bob@example.com", "new\n")]
    ]
    if cut:
        monkeypatch.setattr(code_store, "MAX_DIFF_CHARS", 1)
    store, _ = make_store(conn)
    diff = await diff_snapshots(
        store.engine,
        from_id=old,
        to_id=new,
        context=0,
        reader=NOBODY,
    )
    assert diff is not None
    assert diff.more is cut
    if cut:
        assert diff.files == []
    else:
        assert [(f.path, f.change) for f in diff.files] == [
            ("a", "modified"),
            ("gone", "deleted"),
            ("new", "added"),
        ]
        assert "-old\n+new\n" in diff.files[0].diff
    owners, hashes = conn.fetch.call_args_list[0].args[1:]
    assert owners == [
        "ada@example.com",
        "ada@example.com",
        "bob@example.com",
        "bob@example.com",
    ]
    assert (
        hashes
        == [bytes.fromhex(_sha("old\n"))] * 2 + [bytes.fromhex(_sha("new\n"))] * 2
    )


def _head(record: UUID, *, parent: UUID | None = None) -> dict[str, object]:
    return {
        "record_id": record,
        "parent_record_id": parent,
        "private": False,
        "file_count": 2,
        "total_bytes": 4,
        "partial": "",
        "updated_at": datetime(2026, 11, 6, tzinfo=UTC),
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
