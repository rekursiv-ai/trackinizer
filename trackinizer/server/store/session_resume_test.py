"""Resuming a stored session: read it back whole, and re-attach to its row.

The claim phase 6 rests on: a session captured from ANY CLI reads back from the
store as the records it was given, with their sealed halves, and the resumed run
appends to the original AgentSession rather than forking a second one. Writing
the records out as a file a CLI accepts is the runner's half
(``materialize_test.py`` in the endpoint that resumes).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from treekle import immutable

import pytest

from trackinizer.lib.agent.sessions import (
    claude,
    codex,
)
from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.sagent.session.types import SessionRecord, Thinking
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.server.store.core import Store


# Asked of the MODULE that owns it, not counted in parents from here: the
# export republishes this tree one directory shallower, so a fixed hop
# count resolved outside the package and the fixtures vanished.
_TESTDATA: Final = Path(claude.__file__).resolve().parent / "testdata"


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped."""
    return integ_store


# Fed a line at a time through :class:`Tail`, which is what capture does: the reader
# PULLS lines and the runner PUSHES them, so driving the pull side directly would
# exercise a path the runner never takes.
async def _captured(
    store: Store,
    name: str,
) -> tuple[UUID, int, list[SessionRecordRow]]:
    """Ingest a corpus fixture as a session; return its id, part and stored rows."""
    path = _TESTDATA / name
    reader = Tail((codex if name.startswith("codex") else claude).normalize)
    records: list[TraxRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            records.extend(reader.feed(line))
    records.extend(reader.close())
    session_id = await store.submit_agentsession(
        SubmitAgentSession(title=f"resume {name}", cli="claude", account="t@e"),
        audience="open",
    )
    part = await store.upsert_session_manifest(
        session_id,
        name=path.name,
        # How the file SPELLS its bytes, which is all the manifest carries
        # now: identity is not in the IR, so the capturing client mints the
        # ``ir_id`` rather than reading one off the reader.
        metadata=immutable(reader.encoding),
        ir_id=uuid4(),
        format="codex" if name.startswith("codex") else "claude",
        records=len(records),
    )
    rows = [
        SessionRecordRow.of(session_id=session_id, part=part, idx=idx, record=record)
        for idx, record in enumerate(records)
    ]
    await store.append_session_records(session_id, rows)
    return session_id, part, rows


# Narrowed to the shared IR, exactly as ``resume.py::_read_part`` narrows: these parts
# hold a claude or codex file, and only a formatless scrape part carries stream records.
# Asserted rather than cast, so a fixture that broke the invariant would fail here
# instead of inside the claude writer.
async def _read_back(
    store: Store,
    session_id: UUID,
    part: int,
) -> tuple[Sequence[SessionRecord], Sequence[str | None]]:
    """One part's records and ciphertext, as a resume reads them."""
    rows = await store.read_session_records(session_id, part=part, limit=100_000)
    records: list[SessionRecord] = []
    for row in rows:
        record = row.record()
        assert not isinstance(record, Stdin | Stdout | Stderr), row.kind
        records.append(record)
    return records, [row.ciphertext for row in rows]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("name", ["claude_sidechain.jsonl", "codex_main.jsonl"])
async def test_a_stored_session_reads_back_the_records_and_seals_it_was_given(
    store: Store,
    name: str,
) -> None:
    """What a resume reads is what capture stored: record for record, seal for seal.

    A resume writes these back out for a CLI, so a record the store altered, or a
    ciphertext it attached to the wrong record, would reach the provider.
    """
    session_id, part, rows = await _captured(store, name)

    records, sealed = await _read_back(store, session_id, part)

    assert records == [row.record() for row in rows]
    assert sealed == [row.ciphertext for row in rows]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_codex_capture_stores_a_seal_a_resume_must_have(store: Store) -> None:
    """The counterpart of the read-back above: the fixture does carry ciphertext."""
    session_id, part, _rows = await _captured(store, "codex_main.jsonl")

    records, sealed = await _read_back(store, session_id, part)

    assert any(isinstance(r, Thinking) for r in records), "fixture carries no thinking"
    assert any(seal is not None for seal in sealed), "no ciphertext was stored"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_stamping_the_id_re_attaches_rather_than_forking(store: Store) -> None:
    """The load-bearing step: stamp BEFORE the resumed run opens its session.

    ``start_session`` correlates a resume by finding the existing row whose
    ``agentsession_cli_session_id`` matches. Without the stamp it mints a
    second AgentSession and the transcript splits across two rows.
    """
    session_id, _part, _rows = await _captured(store, "claude_sidechain.jsonl")
    minted = uuid4()
    await store.set_cli_session_id(session_id, str(minted), actor="agent")

    resumed, _actor, _seq = await store.start_session(
        SubmitAgentSession(
            title="resumed",
            cli="claude",
            account="t@e",
            cli_session_id=str(minted),
        ),
        requested_actor="agent",
        audience="open",
    )

    assert resumed == session_id, "the resumed run forked a second AgentSession"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_without_the_stamp_the_run_forks(store: Store) -> None:
    """The counterpart, so the test above is not vacuously true."""
    session_id, _part, _rows = await _captured(store, "claude_sidechain.jsonl")

    forked, _actor, _seq = await store.start_session(
        SubmitAgentSession(
            title="not resumed",
            cli="claude",
            account="t@e",
            cli_session_id=str(uuid4()),
        ),
        requested_actor="agent",
        audience="open",
    )

    assert forked != session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_dropped_ciphertext_reads_back_as_missing_beside_the_same_records(
    store: Store,
) -> None:
    """Retention can drop the sealed half; a resume must be able to tell.

    The records are untouched, and each seal reads back as ``None``, which is what
    makes the runner refuse the replay instead of writing a hollow transcript.
    """
    session_id, part, rows = await _captured(store, "codex_main.jsonl")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "DELETE FROM session_ciphertext WHERE session_id = $1",
            session_id,
        )

    records, sealed = await _read_back(store, session_id, part)

    assert records == [row.record() for row in rows]
    assert any(row.ciphertext is not None for row in rows), "nothing was sealed"
    assert sealed == [None] * len(rows)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_session_stays_searchable_without_its_ciphertext(
    store: Store,
) -> None:
    """Dropping the bytes costs the resume, never the transcript.

    This is the retention lever's whole contract: the plaintext record is
    untouched, so search still finds it.
    """
    session_id, part, _rows = await _captured(store, "codex_main.jsonl")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "DELETE FROM session_ciphertext WHERE session_id = $1",
            session_id,
        )

    rows = await store.read_session_records(session_id, part=part, limit=100_000)

    assert rows, "the records vanished with their ciphertext"
    assert any(row.text for row in rows), "the searchable text was lost"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
