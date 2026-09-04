"""A captured session reads back byte-identical to the file it came from.

The claim phase 4 rests on: ingest stores what the CLI wrote, losslessly, so
the transcript in the database can be handed back to a CLI as its own native
file (phase 6's resume) rather than merely rendered for a human.

Real corpus fixtures rather than synthetic lines, and the whole pipeline
rather than a store call: the fixtures carry the shapes that break a
round-trip -- a claude ``TurnContext`` naming its own index, sealed thinking
whose ciphertext is split into another table, provider-native fields no IR
member has a home for -- and every one of those is a chance for capture to
lose a byte that a hand-written line would not have.

The path under test is the whole one:

    file -> Normalizer.feed -> SessionRecordRow -> session_records
         -> read_session_records -> ciphertext splice -> denormalize -> bytes
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.sessions.convert import _Adapter
from trackinizer.lib.agent.types.sessions import (
    SessionRecord,
    Thinking,
    TurnContext,
)
from trackinizer.lib.custom_json import json_freeze
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.trax.run.adapters.tail import Tail
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


# Asked of the MODULE that owns it, not counted in parents from here: the
# export republishes this tree one directory shallower, so a fixed hop
# count resolved outside the package and the fixtures vanished.
_TESTDATA: Final = Path(claude.__file__).resolve().parent / "testdata"


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """A bootstrapped store on the shared integration database."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _session_row(store: Store) -> UUID:
    """An AgentSession row the records can hang off."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'readback test')",
            session_id,
        )
    return session_id


def _adapter_for(name: str) -> _Adapter:
    """The IR module whose dialect ``name`` is in."""
    return codex if name.startswith("codex") else claude


async def _ingest(store: Store, path: Path, session_id: UUID) -> int:
    """Drive the real capture path over ``path``; return the part it landed in.

    Feeds the file one line at a time through the same ``Tail`` the runner
    uses, so what reaches the database is what a live ``trax run`` would have
    stored -- not a whole-file drain the tailer never performs.
    """
    reader = Tail(_adapter_for(path.name).normalize)
    records: list[TraxRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            records.extend(reader.feed(line))
    records.extend(reader.close())
    # The manifest FIRST: it is what assigns the part number the records are
    # keyed by, exactly as the append route orders it. Numbering the rows
    # before knowing the part filed a second file's records under the first
    # file's, where they overwrote it.
    part = await store.upsert_session_manifest(
        session_id,
        name=path.name,
        metadata=json_freeze(reader.encoding),
        # Identity left the IR, so the client mints one per FILE. It names the
        # stored part, not the transcript: what claude writes into every line
        # rides each record's own residual.
        ir_id=uuid4(),
        format="codex" if path.name.startswith("codex") else "claude",
        records=len(records),
    )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id, part=part, idx=idx, record=record
            )
            for idx, record in enumerate(records)
        ],
    )
    return part


async def _materialize(store: Store, session_id: UUID, part: int) -> str:
    """Read one part back and write it out in its native format.

    Ciphertext is spliced at materialization, which is the whole reason it can
    live in a separate table: the record stores ``encrypted=""`` and the bytes
    rejoin on ``(session_id, part, idx)`` exactly here.
    """
    manifest = next(
        m for m in await store.read_session_manifests(session_id) if m.part == part
    )
    rows = await store.read_session_records(
        session_id, part=part, limit=manifest.records + 1
    )
    # Narrowed from the store's own wider vocabulary: these rows came from a
    # claude or codex file, so a stream record here would mean the fixture was
    # filed under the wrong part -- asserted, since the writer below has no
    # line to emit for one.
    records: list[SessionRecord] = []
    for row in rows:
        record = row.record()
        assert not isinstance(record, Stdin | Stdout | Stderr), row.kind
        if isinstance(record, Thinking) and row.ciphertext is not None:
            record = replace(record, encrypted=row.ciphertext)
        records.append(record)
    # The encoding RESTATED as the leading context: it is what the source file
    # declared, and a rewrite without it escapes different characters -- the
    # file then differs while every record matches. A stored part's records
    # begin at its own ``idx`` 0, so nothing else carries it.
    out = StringIO()
    _adapter_for(manifest.name).denormalize(
        [TurnContext(encoding=manifest.metadata), *records], out
    )
    return out.getvalue()


def _fixtures() -> Sequence[str]:
    """Every captured session fixture, by name."""
    return sorted(path.name for path in _TESTDATA.glob("*.jsonl"))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("name", _fixtures())
async def test_a_captured_session_reads_back_byte_identical(
    store: Store, name: str
) -> None:
    """What ingest stored rewrites to the bytes the CLI wrote.

    Byte-exactness, not equivalence: a resumed session is handed back to the
    CLI as its own file, and a CLI is entitled to reject a transcript it did
    not write. Anything less than identical is a resume that may not load.
    """
    path = _TESTDATA / name
    session_id = await _session_row(store)

    part = await _ingest(store, path, session_id)
    rewritten = await _materialize(store, session_id, part)

    assert rewritten == path.read_text(encoding="utf-8")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("name", _fixtures())
async def test_re_ingesting_a_session_writes_nothing_new(
    store: Store, name: str
) -> None:
    """A whole file fed twice stores one copy, because ``idx`` is derived.

    The rearm case: a watch that dies and rebuilds re-reads the file from the
    start, so every record arrives a second time. This is the property that
    makes that safe, measured over a real transcript rather than two lines.
    """
    path = _TESTDATA / name
    session_id = await _session_row(store)

    await _ingest(store, path, session_id)
    before = await store.read_session_records(session_id, part=0, limit=100_000)
    await _ingest(store, path, session_id)
    after = await store.read_session_records(session_id, part=0, limit=100_000)

    assert [row.idx for row in after] == [row.idx for row in before]
    assert [row.payload for row in after] == [row.payload for row in before]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_codex_session_does_not_double(store: Store) -> None:
    """Codex's launch line declares the session; it is not a record twice.

    Its rollout opens with a ``session_meta`` line that carries metadata
    rather than a turn. A reader that emitted it as a record AND read its
    fields into the manifest would store the launch twice, and the rewrite
    would carry two of them.
    """
    path = _TESTDATA / "codex_main.jsonl"
    session_id = await _session_row(store)

    part = await _ingest(store, path, session_id)
    rewritten = await _materialize(store, session_id, part)

    native = path.read_text(encoding="utf-8")
    assert rewritten.count('"session_meta"') == native.count('"session_meta"')


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ciphertext_survives_the_round_trip(store: Store) -> None:
    """Sealed thinking rejoins its record at materialization.

    The bytes live in ``session_ciphertext`` and the payload holds ``""``, so
    a rewrite that did not splice would emit an empty ``encrypted`` field --
    byte-different, and a resume the provider rejects.
    """
    path = _TESTDATA / "codex_main.jsonl"
    session_id = await _session_row(store)
    part = await _ingest(store, path, session_id)

    rows = await store.read_session_records(session_id, part=part, limit=100_000)
    sealed = [row for row in rows if row.ciphertext]

    assert sealed, "the fixture carries no sealed thinking; pick another"
    # Stored stripped, returned whole: both halves of the split are asserted.
    for row in sealed:
        record = row.record()
        assert isinstance(record, Thinking)
        assert record.encrypted == "", "the payload retained its ciphertext"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_two_parts_each_denormalize_to_their_own_file(store: Store) -> None:
    """A session spanning two files rewrites as two files, not one stream.

    Ingest cannot fuse -- it tails a growing session and the second file may
    not exist yet -- so each part is stored and rewritten on its own. That is
    what lets a resumed run materialize exactly the file it continues.
    """
    session_id = await _session_row(store)
    first = _TESTDATA / "claude_main.jsonl"
    second = _TESTDATA / "claude_sidechain.jsonl"

    part_a = await _ingest(store, first, session_id)
    part_b = await _ingest(store, second, session_id)

    assert part_a != part_b
    assert await _materialize(store, session_id, part_a) == first.read_text(
        encoding="utf-8"
    )
    assert await _materialize(store, session_id, part_b) == second.read_text(
        encoding="utf-8"
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_manifest_metadata_survives_storage(store: Store) -> None:
    """Rewriting needs how the file SPELLED its bytes, not just its records.

    Claude's ascii-escaping convention rides on the ``TurnContext`` in force (a
    majority flag plus its exception bitmap); without it the rewrite escapes
    the wrong characters and the file differs while every record matches.
    """
    path = _TESTDATA / "claude_main.jsonl"
    session_id = await _session_row(store)
    with path.open(encoding="utf-8") as handle:
        # The LAST context to state one: the majority moves as the file grows,
        # so the reader restates it and the final statement is what holds.
        stated = [
            record.encoding
            for record in claude.normalize(handle)
            if isinstance(record, TurnContext) and record.encoding
        ]
    expected = stated[-1]

    part = await _ingest(store, path, session_id)
    manifest = next(
        m for m in await store.read_session_manifests(session_id) if m.part == part
    )

    assert manifest.metadata == expected


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
