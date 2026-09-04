"""Resuming a stored session: read it back, write it, re-attach to its row.

The claim phase 6 rests on: a session captured from ANY CLI can be handed back
to claude as a file claude will accept, and the resumed run appends to the
original AgentSession rather than forking a second one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import json

import pytest
import pytest_asyncio

from trackinizer.lib.agent.sessions import (
    claude as claude_ir,
    codex as codex_ir,
)
from trackinizer.lib.agent.types.sessions import SessionRecord, Thinking
from trackinizer.lib.custom_json import DictCodec, json_freeze
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.trax.run.adapters.tail import Tail
from trackinizer.trax.run.errors import CiphertextDroppedError
from trackinizer.trax.run.materialize import materialize_claude
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord
from trackinizer.wire.bodies import SubmitAgentSession


# Asked of the MODULE that owns it, not counted in parents from here: the
# export republishes this tree one directory shallower, so a fixed hop
# count resolved outside the package and the fixtures vanished.
_TESTDATA: Final = Path(claude_ir.__file__).resolve().parent / "testdata"


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """A bootstrapped store on the shared integration database."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


@pytest.fixture(autouse=True)
def local_claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Materialize into a temp project root rather than the operator's."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))


async def _captured(store: Store, name: str) -> tuple[UUID, int]:
    """Ingest a corpus fixture as a session; return its id and part.

    Fed a line at a time through :class:`Tail`, which is what capture does:
    the reader PULLS lines and the runner PUSHES them, so driving the pull
    side directly would exercise a path the runner never takes.
    """
    path = _TESTDATA / name
    reader = Tail((codex_ir if name.startswith("codex") else claude_ir).normalize)
    records: list[TraxRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            records.extend(reader.feed(line))
    records.extend(reader.close())
    session_id = await store.submit_agentsession(
        SubmitAgentSession(title=f"resume {name}", cli="claude", account="t@e")
    )
    part = await store.upsert_session_manifest(
        session_id,
        name=path.name,
        # How the file SPELLS its bytes, which is all the manifest carries
        # now: identity is not in the IR, so the capturing client mints the
        # ``ir_id`` rather than reading one off the reader.
        metadata=json_freeze(reader.encoding),
        ir_id=uuid4(),
        format="codex" if name.startswith("codex") else "claude",
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
    return session_id, part


async def _read_back(
    store: Store, session_id: UUID, part: int
) -> tuple[Sequence[SessionRecord], Sequence[str | None]]:
    """One part's records and ciphertext, as a resume reads them.

    Narrowed to the shared IR, exactly as ``resume.py::_read_part`` narrows:
    these parts hold a claude or codex file, and only a formatless scrape part
    carries stream records. Asserted rather than cast, so a fixture that broke
    the invariant would fail here instead of inside the claude writer.
    """
    rows = await store.read_session_records(session_id, part=part, limit=100_000)
    records: list[SessionRecord] = []
    for row in rows:
        record = row.record()
        assert not isinstance(record, Stdin | Stdout | Stderr), row.kind
        records.append(record)
    return records, [row.ciphertext for row in rows]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_claude_session_resumes_to_its_stored_records(store: Store) -> None:
    """Materialize, then re-normalize: the records must be the stored ones.

    NOT byte-identical to the capture, deliberately -- the session id was
    rewritten, which is the whole point of minting a fresh one. Every RECORD
    survives that rewrite.
    """
    session_id, part = await _captured(store, "claude_sidechain.jsonl")
    records, sealed = await _read_back(store, session_id, part)
    manifests = await store.read_session_manifests(session_id)

    written = materialize_claude(
        records=records, encoding=manifests[0].metadata, sealed=sealed
    )
    with written.path.open(encoding="utf-8") as handle:
        reread = list(claude_ir.normalize(handle))

    # Compared with the session id normalized away on both sides: rewriting it
    # is the POINT of materializing, so a record differing only there is a
    # record that survived. Everything else must match exactly.
    assert _without_session_id(reread) == _without_session_id(records)


def _without_session_id(records: Sequence[TraxRecord]) -> list[TraxRecord]:
    """Each record with any ``sessionId`` dropped from its residual."""
    out: list[TraxRecord] = []
    for record in records:
        residual = DictCodec.coerce(getattr(record, "extra", None))
        if "sessionId" not in residual:
            out.append(record)
            continue
        out.append(
            replace(
                record,
                extra=json_freeze(
                    {k: v for k, v in residual.items() if k != "sessionId"}
                ),
            )
        )
    return out


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_codex_capture_resumes_as_claude(store: Store) -> None:
    """The cross-CLI case: what captured it does not decide what resumes it.

    Codex cannot be re-entered (it has no stable per-session id), but its
    transcript is IR records like any other, so claude can be handed them.
    """
    session_id, part = await _captured(store, "codex_main.jsonl")
    records, sealed = await _read_back(store, session_id, part)
    manifests = await store.read_session_manifests(session_id)

    written = materialize_claude(
        records=records, encoding=manifests[0].metadata, sealed=sealed
    )

    assert written.path.exists()
    with written.path.open(encoding="utf-8") as handle:
        reread = list(claude_ir.normalize(handle))
    # A conversion across formats is lossy in the ENVELOPE, not the content:
    # every message the model and user exchanged comes back.
    assert reread, "a codex capture materialized to an empty transcript"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_materialized_file_names_the_minted_id(store: Store) -> None:
    """Filename, ``sessionId``, and the ``--resume`` argument all agree.

    A file still declaring the CAPTURED id is one the CLI will not associate
    with the id it was asked to resume.
    """
    session_id, part = await _captured(store, "claude_sidechain.jsonl")
    records, sealed = await _read_back(store, session_id, part)
    manifests = await store.read_session_manifests(session_id)

    written = materialize_claude(
        records=records, encoding=manifests[0].metadata, sealed=sealed
    )

    declared = {
        json.loads(line)["sessionId"]
        for line in written.path.read_text(encoding="utf-8").splitlines()
    }
    assert declared == {str(written.cli_session_id)}
    assert written.path.stem == str(written.cli_session_id)
    # The CAPTURED id is the one the fixture's own lines carry -- not the
    # manifest's ``ir_id``, which the capturing client mints and never writes
    # into the file, so asserting on that would pass without the rewrite.
    captured = {
        json.loads(line)["sessionId"]
        for line in (_TESTDATA / "claude_sidechain.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    assert captured, "the fixture declares no sessionId to leak"
    assert not captured & declared, "the captured id leaked through"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_stamping_the_id_re_attaches_rather_than_forking(store: Store) -> None:
    """The load-bearing step: stamp BEFORE the resumed run opens its session.

    ``start_session`` correlates a resume by finding the existing row whose
    ``agentsession_cli_session_id`` matches. Without the stamp it mints a
    second AgentSession and the transcript splits across two rows.
    """
    session_id, _part = await _captured(store, "claude_sidechain.jsonl")
    minted = uuid4()
    await store.set_cli_session_id(session_id, str(minted), actor="agent")

    resumed, _actor, _seq = await store.start_session(
        SubmitAgentSession(
            title="resumed", cli="claude", account="t@e", cli_session_id=str(minted)
        ),
        requested_actor="agent",
    )

    assert resumed == session_id, "the resumed run forked a second AgentSession"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_without_the_stamp_the_run_forks(store: Store) -> None:
    """The counterpart, so the test above is not vacuously true."""
    session_id, _part = await _captured(store, "claude_sidechain.jsonl")

    forked, _actor, _seq = await store.start_session(
        SubmitAgentSession(
            title="not resumed",
            cli="claude",
            account="t@e",
            cli_session_id=str(uuid4()),
        ),
        requested_actor="agent",
    )

    assert forked != session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_dropped_ciphertext_refuses_the_resume(store: Store) -> None:
    """Retention can drop the sealed half; a replay must say so.

    The record stays searchable -- that is what the split buys -- but it
    cannot be handed back to the provider, which validates the field.
    """
    session_id, part = await _captured(store, "codex_main.jsonl")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "DELETE FROM session_ciphertext WHERE session_id = $1", session_id
        )
    records, sealed = await _read_back(store, session_id, part)
    manifests = await store.read_session_manifests(session_id)

    assert any(isinstance(r, Thinking) for r in records), "fixture carries no thinking"
    with pytest.raises(CiphertextDroppedError):
        materialize_claude(
            records=records, encoding=manifests[0].metadata, sealed=sealed
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_session_stays_searchable_without_its_ciphertext(
    store: Store,
) -> None:
    """Dropping the bytes costs the resume, never the transcript.

    This is the retention lever's whole contract: the plaintext record is
    untouched, so search still finds it.
    """
    session_id, part = await _captured(store, "codex_main.jsonl")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "DELETE FROM session_ciphertext WHERE session_id = $1", session_id
        )

    rows = await store.read_session_records(session_id, part=part, limit=100_000)

    assert rows, "the records vanished with their ciphertext"
    assert any(row.text for row in rows), "the searchable text was lost"


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
