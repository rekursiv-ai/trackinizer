"""The code store keeps blobs once per owner, reads snapshots by rule, and cuts diffs.

The diff and validation helpers are pure, so they are tested alone, and a mocked
connection pins the SQL the blob writes send. Everything else runs against a real
PGlite engine: the read rule, the owner test inside the upsert and the cascades are
SQL, which a mock connection cannot stand in for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4

import hashlib

from treekle import from_plain

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.lib import zstd_compat
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.audience import NOBODY, SessionReader
from trackinizer.server.store import code_store
from trackinizer.server.store.code_store import (
    MissingBlobsError,
    NoParentSnapshotError,
    SnapshotOwnerError,
    _check_files,
    _diff_chunk,
    _unified,
    _verified,
    diff_snapshots,
    missing_blobs,
    put_blobs,
    put_snapshot,
    read_file,
    read_snapshot,
)
from trackinizer.types.errors import NotFoundError, ValidationError
from trackinizer.wire.audience import AUDIENCES, OPEN, PRIVATE
from trackinizer.wire.wire_code import (
    MAX_BLOB_BYTES,
    MAX_BLOBS_PER_REQUEST,
    MAX_MISSING_HASHES,
    MAX_PATH_CHARS,
    MAX_REQUEST_TEXT_BYTES,
    MAX_SNAPSHOT_FILES,
    CodeBlob,
    CodeSnapshotHead,
    CodeSnapshotPut,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PGliteEngine


_ADA: Final = "ada@example.com"
_BOB: Final = "bob@example.com"
_INSERT_BLOBS: Final = (
    "INSERT INTO code_blobs (owner, sha256, size, data_zst) "
    "SELECT $1, b.sha256, b.size, b.data_zst "
    "FROM unnest($2::bytea[], $3::int[], $4::bytea[]) AS b(sha256, size, data_zst) "
    "ON CONFLICT (owner, sha256) DO NOTHING RETURNING sha256"
)
_HELD_BLOBS: Final = (
    "SELECT sha256 FROM code_blobs WHERE owner = $1 AND sha256 = ANY($2)"
)


@pytest.mark.asyncio
async def test_put_blobs_sends_each_blob_once_compressed_and_counts_the_new() -> None:
    engine = FakeEngine()
    engine.conn.fetch.return_value = [{"sha256": bytes.fromhex(_sha("é"))}]

    stored = await put_blobs(
        cast(DatabaseEngine, engine),
        owner=_ADA,
        blobs=[_blob("é"), _blob(""), _blob("é")],
    )

    [call] = engine.conn.fetch.call_args_list
    sql, owner, hashes, sizes, data = call.args
    assert (sql, owner) == (_INSERT_BLOBS, _ADA)
    assert hashes == [bytes.fromhex(_sha("é")), bytes.fromhex(_sha(""))]
    assert sizes == [2, 0]
    assert [zstd_compat.decompress(d) for d in from_plain(data, list[bytes])] == [
        "é".encode(),
        b"",
    ]
    assert stored == 1


@pytest.mark.asyncio
async def test_put_blobs_takes_up_to_its_bounds_and_refuses_one_more() -> None:
    engine = FakeEngine()
    full = [_blob(letter * MAX_BLOB_BYTES) for letter in "abcdefgh"]

    assert sum(len(b.text) for b in full) == MAX_REQUEST_TEXT_BYTES
    for fits in ([_blob("x")] * MAX_BLOBS_PER_REQUEST, full):
        await put_blobs(cast(DatabaseEngine, engine), owner=_ADA, blobs=fits)
    with pytest.raises(ValidationError) as many:
        await put_blobs(
            cast(DatabaseEngine, engine),
            owner=_ADA,
            blobs=[_blob("x")] * (MAX_BLOBS_PER_REQUEST + 1),
        )
    with pytest.raises(ValidationError) as large:
        await put_blobs(
            cast(DatabaseEngine, engine),
            owner=_ADA,
            blobs=[*full, _blob("x")],
        )

    assert str(many.value) == "one request carries at most 1000 blobs, not 1001"
    assert str(large.value) == (
        "one request carries at most 8388608 bytes of text, not 8388609"
    )
    assert engine.conn.fetch.call_count == 2


@pytest.mark.asyncio
async def test_missing_blobs_asks_once_per_hash_and_keeps_the_order() -> None:
    engine = FakeEngine()
    held, other = _sha("held"), _sha("other")
    engine.conn.fetch.return_value = [{"sha256": bytes.fromhex(held)}]

    missing = await missing_blobs(
        cast(DatabaseEngine, engine),
        owner=_ADA,
        hashes=[other, held, other],
    )
    await missing_blobs(
        cast(DatabaseEngine, engine),
        owner=_ADA,
        hashes=[held] * MAX_MISSING_HASHES,
    )
    with pytest.raises(ValidationError) as many:
        await missing_blobs(
            cast(DatabaseEngine, engine),
            owner=_ADA,
            hashes=[held] * (MAX_MISSING_HASHES + 1),
        )

    first, full = engine.conn.fetch.call_args_list
    assert full.args[2] == [bytes.fromhex(held)]
    assert first.args == (
        _HELD_BLOBS,
        _ADA,
        [bytes.fromhex(other), bytes.fromhex(held)],
    )
    assert missing == [other]
    assert str(many.value) == "one request asks about at most 5000 hashes, not 5001"


def test_a_diff_is_headed_a_and_b_for_every_change() -> None:
    added = _unified("x.py", old="", new="a\nb\n", context=3)
    deleted = _unified("x.py", old="a\n", new="", context=3)
    modified = _unified("x.py", old="a\nb\n", new="a\nc\n", context=3)

    assert added == "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    assert deleted == "--- a/x.py\n+++ b/x.py\n@@ -1 +0,0 @@\n-a\n"
    assert modified == "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"


def test_a_last_line_without_a_newline_is_marked() -> None:
    diff = _unified("x.py", old="a\nb", new="a\nc", context=1)

    assert diff == (
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n"
        "-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n"
    )


def test_only_a_newline_ends_a_line() -> None:
    diff = _unified("x.py", old="a\rb\x0cc\n", new="a\rb\x0cd\n", context=0)

    assert diff == "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\rb\x0cc\n+a\rb\x0cd\n"


def test_context_bounds_the_lines_around_a_change() -> None:
    old = "".join(f"{n}\n" for n in range(10))
    new = old.replace("5\n", "five\n")

    narrow = _unified("x.py", old=old, new=new, context=1)
    wide = _unified("x.py", old=old, new=new, context=3)

    assert narrow.splitlines()[2:] == ["@@ -5,3 +5,3 @@", " 4", "-5", "+five", " 6"]
    assert wide.splitlines()[2] == "@@ -3,7 +3,7 @@"


def test_a_chunk_is_cut_after_its_last_whole_line_inside_the_budget() -> None:
    texts = {"one": "a\n", "two": "b\nc\nd\n", "three": "e\n"}
    blobs = {(_ADA, _sha(t)): zstd_compat.compress(t.encode()) for t in texts.values()}
    chunk = [(f"{name}.py", None, _sha(text)) for name, text in texts.items()]
    first = _unified("one.py", old="", new="a\n", context=3)
    second_head = "--- a/two.py\n+++ b/two.py\n@@ -0,0 +1,3 @@\n+b\n"

    diffs, left = _diff_chunk(
        chunk,
        sides=(_ADA, _ADA),
        blobs=blobs,
        context=3,
        budget=len(first) + len(second_head) + 2,
    )
    whole, whole_left = _diff_chunk(
        chunk,
        sides=(_ADA, _ADA),
        blobs=blobs,
        context=3,
        budget=10_000,
    )
    none_fit, none_left = _diff_chunk(
        chunk[1:],
        sides=(_ADA, _ADA),
        blobs=blobs,
        context=3,
        budget=3,
    )

    assert [(d.path, d.diff) for d in diffs] == [
        ("one.py", first),
        ("two.py", second_head),
    ]
    assert left < 0
    assert [d.path for d in whole] == ["one.py", "two.py", "three.py"]
    assert whole_left == 10_000 - sum(len(d.diff) for d in whole)
    assert (none_fit, none_left) == ([], -1)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/a.py",
        "a/",
        "a//b.py",
        "./a.py",
        "a/./b.py",
        "../a.py",
        "a/../b.py",
        "a\x00b.py",
        "a" * (MAX_PATH_CHARS + 1),
    ],
)
def test_a_path_is_plain_relative_posix(path: str) -> None:
    with pytest.raises(ValidationError) as refused:
        _check_files({path: _sha("")})

    assert str(refused.value) == (
        f"{path[:100]!r} is not a relative POSIX path of at most 1024 characters "
        "with no empty, '.' or '..' segment"
    )


def test_plain_relative_paths_pass_up_to_the_bounds() -> None:
    _check_files(
        {
            p: _sha("")
            for p in ("a.py", "a/b.py", ".hidden/x", "a..b", "a" * MAX_PATH_CHARS)
        },
    )
    _check_files({f"{n}.py": _sha("") for n in range(MAX_SNAPSHOT_FILES)})
    with pytest.raises(ValidationError) as many:
        _check_files({f"{n}.py": _sha("") for n in range(MAX_SNAPSHOT_FILES + 1)})

    assert str(many.value) == "a snapshot maps at most 20000 files, not 20001"


def test_a_blob_is_its_texts_utf8_under_its_hash() -> None:
    full = "a" * MAX_BLOB_BYTES
    big = f"{full}a"

    assert _verified(CodeBlob(sha256=_sha("é"), text="é")) == "é".encode()
    assert len(_verified(CodeBlob(sha256=_sha(full), text=full))) == MAX_BLOB_BYTES
    for blob, problem in (
        (
            CodeBlob(sha256=_sha("b"), text="a"),
            f"blob {_sha('b')} is not the sha256 of its text",
        ),
        (
            CodeBlob(sha256=_sha(big), text=big),
            f"blob {_sha(big)} holds 1048577 bytes, over 1048576",
        ),
        (
            CodeBlob(sha256=_sha(""), text="\ud800"),
            f"blob {_sha('')} has no UTF-8 form",
        ),
    ):
        with pytest.raises(ValidationError) as refused:
            _verified(blob)
        assert str(refused.value) == problem


@pytest.fixture
def engine(pglite_db: PGliteEngine) -> PGliteEngine:
    """Return the session's PGlite engine over a freshly bootstrapped schema."""
    return pglite_db


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestBlobs:
    async def test_missing_lists_each_unstored_hash_once_in_order(
        self,
        engine: PGliteEngine,
    ) -> None:
        held, other, third = _sha("held"), _sha("other"), _sha("third")
        await put_blobs(engine, owner=_ADA, blobs=[_blob("held")])

        missing = await missing_blobs(
            engine,
            owner=_ADA,
            hashes=[other, held, other, third],
        )

        assert missing == [other, third]

    async def test_dedupe_never_crosses_owners(self, engine: PGliteEngine) -> None:
        await put_blobs(engine, owner=_ADA, blobs=[_blob("shared")])

        assert await missing_blobs(engine, owner=_BOB, hashes=[_sha("shared")]) == [
            _sha("shared"),
        ]
        assert await put_blobs(engine, owner=_BOB, blobs=[_blob("shared")]) == 1

    async def test_put_counts_new_blobs_and_a_repeat_is_a_no_op(
        self,
        engine: PGliteEngine,
    ) -> None:
        first = await put_blobs(
            engine,
            owner=_ADA,
            blobs=[_blob("héllo"), _blob("b"), _blob("héllo")],
        )
        second = await put_blobs(engine, owner=_ADA, blobs=[_blob("héllo"), _blob("c")])
        async with engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT size, data_zst FROM code_blobs WHERE sha256 = $1",
                bytes.fromhex(_sha("héllo")),
            )

        assert (first, second) == (2, 1)
        assert row is not None
        assert row["size"] == len("héllo".encode())
        assert zstd_compat.decompress(from_plain(row["data_zst"], bytes)) == (
            "héllo".encode()
        )

    async def test_put_refuses_a_hash_its_text_does_not_have(
        self,
        engine: PGliteEngine,
    ) -> None:
        with pytest.raises(ValidationError, match="not the sha256"):
            await put_blobs(
                engine,
                owner=_ADA,
                blobs=[_blob("good"), CodeBlob(sha256=_sha("x"), text="y")],
            )

        assert await missing_blobs(engine, owner=_ADA, hashes=[_sha("good")]) == [
            _sha("good"),
        ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestSnapshots:
    async def test_a_snapshot_counts_its_files_and_their_bytes(
        self,
        engine: PGliteEngine,
    ) -> None:
        record = await _record(engine)

        head = await _file(engine, record, {"a.py": "x\n", "b.py": "x\n", "c/d": "é"})

        assert (head.record_id, head.parent_record_id) == (record, None)
        assert (head.file_count, head.total_bytes) == (3, 6)
        assert (head.private, head.partial) == (False, "")

    async def test_filing_again_replaces_the_snapshot(
        self,
        engine: PGliteEngine,
    ) -> None:
        record = await _record(engine)
        first = await _file(engine, record, {"a.py": "1\n"})
        again = await _file(engine, record, {"a.py": "1\n"})
        changed = await _file(engine, record, {"b.py": "2\n"})
        async with engine.acquire() as conn:
            rows = await conn.fetchval("SELECT count(*) FROM code_snapshots")
        snapshot = await read_snapshot(engine, record_id=record, reader=_reader(_ADA))

        assert again.model_dump(exclude={"updated_at"}) == first.model_dump(
            exclude={"updated_at"},
        )
        assert again.updated_at >= first.updated_at
        assert rows == 1
        assert changed.total_bytes == 2
        assert snapshot is not None
        assert [f.path for f in snapshot.files] == ["b.py"]

    async def test_an_unknown_record_is_not_found(self, engine: PGliteEngine) -> None:
        with pytest.raises(NotFoundError):
            await _file(engine, uuid4(), {"a.py": ""})

    async def test_a_snapshot_waits_for_each_of_its_blobs(
        self,
        engine: PGliteEngine,
    ) -> None:
        record = await _record(engine)
        await put_blobs(engine, owner=_ADA, blobs=[_blob("stored")])
        await put_blobs(engine, owner=_BOB, blobs=[_blob("bobs")])

        with pytest.raises(MissingBlobsError, match="1 of the snapshot's 2 blobs"):
            await put_snapshot(
                engine,
                record_id=record,
                body=_put({"a.py": _sha("stored"), "b.py": _sha("bobs")}),
                created_by=None,
                replace_others=False,
            )

        filed = await read_snapshot(engine, record_id=record, reader=_reader(_ADA))

        assert MissingBlobsError.code == "code_blobs_missing"
        assert filed is None

    async def test_a_parent_holds_a_snapshot_of_the_same_owner(
        self,
        engine: PGliteEngine,
    ) -> None:
        bare, bobs, adas, child = [await _record(engine) for _ in range(4)]
        await _file(engine, bobs, {"a.py": ""}, owner=_BOB)
        await _file(engine, adas, {"a.py": ""})

        for parent in (bare, bobs):
            with pytest.raises(NoParentSnapshotError):
                await _file(engine, child, {"a.py": ""}, parent=parent)
        with pytest.raises(ValidationError, match="its own record"):
            await _file(engine, child, {"a.py": ""}, parent=child)
        head = await _file(engine, child, {"a.py": ""}, parent=adas)

        assert NoParentSnapshotError.code == "code_parent_missing"
        assert head.parent_record_id == adas

    async def test_only_its_owner_or_staff_replaces_a_snapshot(
        self,
        engine: PGliteEngine,
    ) -> None:
        record = await _record(engine)
        await _file(engine, record, {"ada.py": ""})

        with pytest.raises(SnapshotOwnerError):
            await _file(engine, record, {"bob.py": ""}, owner=_BOB)
        kept = await read_snapshot(engine, record_id=record, reader=_reader(_BOB))
        await _file(engine, record, {"bob.py": ""}, owner=_BOB, replace_others=True)
        replaced = await read_snapshot(engine, record_id=record, reader=_reader(_BOB))

        assert kept is not None
        assert [f.path for f in kept.files] == ["ada.py"]
        assert replaced is not None
        assert [f.path for f in replaced.files] == ["bob.py"]

    async def test_a_path_that_climbs_is_refused(self, engine: PGliteEngine) -> None:
        record = await _record(engine)

        with pytest.raises(ValidationError, match="relative POSIX"):
            await _file(engine, record, {"../escape.py": ""})

    async def test_purging_a_record_drops_its_snapshot_and_unlinks_its_children(
        self,
        engine: PGliteEngine,
    ) -> None:
        parent, child = await _record(engine), await _record(engine)
        await _file(engine, parent, {"a.py": ""})
        await _file(engine, child, {"a.py": ""}, parent=parent)
        async with engine.acquire() as conn:
            await conn.execute("DELETE FROM inquiries WHERE id = $1", parent)
        gone = await read_snapshot(engine, record_id=parent, reader=_reader(_ADA))
        kept = await read_snapshot(engine, record_id=child, reader=_reader(_ADA))

        assert gone is None
        assert kept is not None
        assert kept.parent_record_id is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReads:
    async def test_a_snapshot_lists_its_files_by_path_with_sizes(
        self,
        engine: PGliteEngine,
    ) -> None:
        record = await _record(engine)
        await _file(engine, record, {"z.py": "zz", "a/b.py": "é", "a.py": ""})

        snapshot = await read_snapshot(engine, record_id=record, reader=_reader(_BOB))

        assert snapshot is not None
        assert [(f.path, f.sha256, f.size) for f in snapshot.files] == [
            ("a.py", _sha(""), 0),
            ("a/b.py", _sha("é"), 2),
            ("z.py", _sha("zz"), 2),
        ]
        assert _ADA not in snapshot.model_dump_json()

    async def test_a_file_is_read_by_its_path(self, engine: PGliteEngine) -> None:
        record = await _record(engine)
        await _file(engine, record, {"a.py": "print('hi')\n"})

        found = await read_file(
            engine,
            record_id=record,
            path="a.py",
            reader=_reader(_BOB),
        )
        absent = await read_file(
            engine,
            record_id=record,
            path="b.py",
            reader=_reader(_BOB),
        )

        assert found is not None
        assert (found.path, found.sha256, found.text) == (
            "a.py",
            _sha("print('hi')\n"),
            "print('hi')\n",
        )
        assert absent is None

    async def test_a_private_snapshot_is_read_by_its_owner_alone(
        self,
        engine: PGliteEngine,
    ) -> None:
        mine, theirs = await _record(engine), await _record(engine)
        await _file(engine, mine, {"a.py": "secret\n"}, private=True)
        await _file(engine, theirs, {"a.py": "open\n"})
        overseer = _reader(_BOB, oversight=True)

        assert await read_snapshot(engine, record_id=mine, reader=_reader(_ADA))
        for reader in (overseer, _reader(_BOB), NOBODY):
            assert await read_snapshot(engine, record_id=mine, reader=reader) is None
            assert (
                await read_file(engine, record_id=mine, path="a.py", reader=reader)
                is None
            )
            assert (
                await diff_snapshots(
                    engine,
                    from_id=theirs,
                    to_id=mine,
                    context=3,
                    reader=reader,
                )
                is None
            )
        assert await read_snapshot(engine, record_id=theirs, reader=_reader(_BOB))

    async def test_a_sessions_snapshot_is_read_by_who_reads_its_transcript(
        self,
        engine: PGliteEngine,
    ) -> None:
        session = await _record(engine, kind="AgentSession", audience="staff")
        await _file(engine, session, {"a.py": ""})
        outsider = SessionReader(audiences=frozenset({OPEN, PRIVATE}), email=_BOB)

        assert await read_snapshot(engine, record_id=session, reader=_reader(_BOB))
        assert await read_snapshot(engine, record_id=session, reader=outsider) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestDiffs:
    async def test_a_diff_lists_added_deleted_and_modified_paths_sorted(
        self,
        engine: PGliteEngine,
    ) -> None:
        old, new = await _record(engine), await _record(engine, account=_BOB)
        await _file(engine, old, {"same.py": "s\n", "b.py": "1\n", "c.py": "gone\n"})
        await _file(
            engine,
            new,
            {"same.py": "s\n", "b.py": "2\n", "a.py": "new\n"},
            owner=_BOB,
        )

        diff = await diff_snapshots(
            engine,
            from_id=old,
            to_id=new,
            context=3,
            reader=_reader(_ADA),
        )

        assert diff is not None
        assert not diff.more
        assert [
            (f.path, f.change, f.old_sha256, f.new_sha256, f.diff) for f in diff.files
        ] == [
            (
                "a.py",
                "added",
                None,
                _sha("new\n"),
                _unified("a.py", old="", new="new\n", context=3),
            ),
            (
                "b.py",
                "modified",
                _sha("1\n"),
                _sha("2\n"),
                _unified("b.py", old="1\n", new="2\n", context=3),
            ),
            (
                "c.py",
                "deleted",
                _sha("gone\n"),
                None,
                _unified("c.py", old="gone\n", new="", context=3),
            ),
        ]

    async def test_a_diff_of_a_snapshot_with_itself_is_empty(
        self,
        engine: PGliteEngine,
    ) -> None:
        record = await _record(engine)
        await _file(engine, record, {"a.py": "x\n"})

        diff = await diff_snapshots(
            engine,
            from_id=record,
            to_id=record,
            context=0,
            reader=_reader(_ADA),
        )

        assert diff is not None
        assert (diff.files, diff.more) == ([], False)

    async def test_a_diff_is_cut_whole_lines_within_its_bound(
        self,
        engine: PGliteEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old, new = await _record(engine), await _record(engine)
        await _file(engine, old, {"keep.py": ""})
        texts = {"a.py": "1\n2\n", "b.py": "3\n4\n5\n", "c.py": "6\n"}
        await _file(engine, new, {"keep.py": "", **texts})
        first = _unified("a.py", old="", new=texts["a.py"], context=3)
        second = _unified("b.py", old="", new=texts["b.py"], context=3)
        # One path per fetch, so the bound carries from one fetch to the next.
        monkeypatch.setattr(code_store, "_BLOBS_PER_FETCH", 1)
        monkeypatch.setattr(code_store, "MAX_DIFF_CHARS", len(first) + len(second) - 1)

        diff = await diff_snapshots(
            engine,
            from_id=old,
            to_id=new,
            context=3,
            reader=_reader(_ADA),
        )

        assert diff is not None
        assert diff.more
        assert [(f.path, f.diff) for f in diff.files] == [
            ("a.py", first),
            ("b.py", second.removesuffix("+5\n")),
        ]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _blob(text: str) -> CodeBlob:
    return CodeBlob(sha256=_sha(text), text=text)


def _put(
    files: dict[str, str],
    *,
    owner: str = _ADA,
    private: bool = False,
    parent: UUID | None = None,
) -> CodeSnapshotPut:
    return CodeSnapshotPut(
        owner=owner,
        private=private,
        parent_record_id=parent,
        files=files,
    )


def _reader(email: str, *, oversight: bool = False) -> SessionReader:
    """Describe a caller who reads every session's transcript, under ``email``."""
    return SessionReader(
        audiences=AUDIENCES,
        email=email,
        every_session=True,
        oversight=oversight,
    )


async def _record(
    engine: PGliteEngine,
    *,
    kind: str = "Experiment",
    audience: str | None = None,
    account: str = _ADA,
) -> UUID:
    """Insert one row of ``kind``; return its id."""
    record = uuid4()
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) VALUES ($1, $2, CASE WHEN $2 = 'AgentSession' "
            "THEN nextval('seq_agentsession') ELSE nextval('seq_experiment') END, "
            "'active', $3, 'run', $4)",
            record,
            kind,
            account,
            audience,
        )
    return record


async def _file(
    engine: PGliteEngine,
    record: UUID,
    texts: dict[str, str],
    *,
    owner: str = _ADA,
    private: bool = False,
    parent: UUID | None = None,
    replace_others: bool = False,
) -> CodeSnapshotHead:
    """Store each text as a blob, then file them as ``record``'s snapshot."""
    await put_blobs(engine, owner=owner, blobs=[_blob(t) for t in texts.values()])
    return await put_snapshot(
        engine,
        record_id=record,
        body=_put(
            {path: _sha(text) for path, text in texts.items()},
            owner=owner,
            private=private,
            parent=parent,
        ),
        created_by=None,
        replace_others=replace_others,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
