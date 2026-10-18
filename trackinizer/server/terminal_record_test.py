"""The terminal recorder, over PGlite: a private session of ``Stdout`` lines."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.terminal_record import TerminalRecorder, plain_text
from trackinizer.server.terminals import Terminal
from trackinizer.types.streams import Stdout
from trackinizer.wire.key_shapes import MASK
from trackinizer.wire.wire_session_ir import MAX_RECORD_BATCH


if TYPE_CHECKING:
    from trackinizer.lib import postgres


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]

_USER = "ada@x.y"


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: postgres.PGliteEngine) -> Store:
    await reset_schema(pglite_engine)
    made = Store(pglite_engine, embed=StubEmbedder())
    await made.bootstrap()
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES (gen_random_uuid(), $1, 'ada', 'writer', 'active')",
            _USER,
        )
    return made


async def _no_sleep(seconds: float) -> None:
    del seconds


async def _lines(store: Store, session_id: uuid.UUID) -> list[str]:
    parts = await store.read_session_manifests(session_id)
    assert [part.name for part in parts] == ["stream"]
    rows = await store.read_session_records(
        session_id,
        part=parts[0].part,
        limit=5_000,
    )
    assert [row.idx for row in rows] == list(range(len(rows)))
    return [str(row.payload["text"]) for row in rows if row.kind == Stdout.__name__]


async def _terminal(
    recorder: TerminalRecorder,
    *,
    workspace: uuid.UUID | None,
) -> Terminal:
    session_id = await recorder.start(user=_USER, machine="gx10", workspace=workspace)
    return Terminal(
        id=uuid.uuid4(),
        user=_USER,
        machine_id=uuid.uuid4(),
        machine="gx10",
        session_id=session_id,
        rows=24,
        cols=80,
        opened_at=0.0,
    )


async def test_a_terminal_is_a_private_labelled_session_of_stdout_lines(
    store: Store,
) -> None:
    station = uuid.uuid4()
    recorder = TerminalRecorder(store, sleep=_no_sleep)
    term = await _terminal(recorder, workspace=station)

    await recorder.output(term, b"$ ls\r\nfile")
    await recorder.output(term, b" a\r\n$ ")
    await recorder.ended(term, status=0)

    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, agentsession_cli AS cli, agentsession_audience AS audience, labels, "
            "agentsession_ended AS ended, account FROM inquiries WHERE id = $1",
            term.session_id,
        )
    assert row is not None
    assert row["title"] == "Terminal on gx10"
    assert (row["cli"], row["audience"]) == ("terminal", "private")
    assert row["ended"] is not None
    assert row["account"] == _USER
    assert set(from_plain(row["labels"], list[str])) == {
        "terminal",
        "machine:gx10",
        f"station:{station}",
    }
    assert await _lines(store, term.session_id) == ["$ ls\n", "file a\n", "$ "]


async def test_a_key_shape_is_masked_in_the_recording_and_a_pem_across_lines(
    store: Store,
) -> None:
    recorder = TerminalRecorder(store, sleep=_no_sleep)
    term = await _terminal(recorder, workspace=None)
    key = "ghp_" + "f" * 36

    await recorder.output(
        term,
        f"echo {key}\n-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\nok\n".encode(),
    )
    await recorder.ended(term, status=0)

    assert await _lines(store, term.session_id) == [
        f"echo {MASK}\n",
        f"{MASK}\n",
        f"{MASK}\n",
        f"{MASK}\n",
        "ok\n",
    ]


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("\x1b[31mred\x1b[0m ok\n", "red ok\n"),
        ("abc\rX\x1b[K\n", "X\n"),
        ("abc\rXY\n", "XYc\n"),
        ("abc\x08\x08Z\n", "aZc\n"),
        ("\x1b]0;title\x07$ \x1b]8;;http://x\x1b\\link", "$ link"),
        ("\x1bOA\x07tab\there", "tab\there"),
        ("plain", "plain"),
        ("\x1b[2K\r$ \n", "$ \n"),
    ],
)
async def test_a_line_is_read_as_the_screen_shows_it(raw: str, shown: str) -> None:
    assert plain_text(raw) == shown


async def test_a_key_broken_up_by_escape_sequences_is_still_masked_in_the_recording(
    store: Store,
) -> None:
    recorder = TerminalRecorder(store, sleep=_no_sleep)
    term = await _terminal(recorder, workspace=None)
    key = "ghp_" + "f" * 36
    erased = "\x1b[K".join(key[i : i + 5] for i in range(0, len(key), 5))
    coloured = key[:20] + "\x1b[1m" + key[20:]
    redrawn = "ghp_xxxx\rghp_" + "f" * 36

    await recorder.output(term, f"{erased}\n{coloured}\n{redrawn}\x1b[K\n".encode())
    await recorder.ended(term, status=0)

    assert await _lines(store, term.session_id) == [f"{MASK}\n"] * 3


async def test_the_cap_writes_one_marker_and_later_output_is_dropped(
    store: Store,
) -> None:
    recorder = TerminalRecorder(store, sleep=_no_sleep, cap_bytes=10)
    term = await _terminal(recorder, workspace=None)

    await recorder.output(term, b"one\ntwo\nthree\n")
    await recorder.output(term, b"four\n")
    await recorder.ended(term, status=0)

    assert await _lines(store, term.session_id) == [
        "one\n",
        "two\n",
        "th",
        "... (recording truncated)\n",
    ]


async def test_a_full_batch_is_stored_at_once_and_a_short_one_after_the_interval(
    store: Store,
) -> None:
    waited = asyncio.Event()

    async def release(seconds: float) -> None:
        del seconds
        await waited.wait()

    recorder = TerminalRecorder(store, sleep=release)
    term = await _terminal(recorder, workspace=None)

    await recorder.output(term, b"early\n")
    assert await store.read_session_manifests(term.session_id) == []

    waited.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if await store.read_session_manifests(term.session_id):
            break
    assert await _lines(store, term.session_id) == ["early\n"]

    await recorder.output(term, b"x\n" * (MAX_RECORD_BATCH + 5))
    assert len(await _lines(store, term.session_id)) > MAX_RECORD_BATCH
    await recorder.ended(term, status=0)
    assert len(await _lines(store, term.session_id)) == MAX_RECORD_BATCH + 6


async def test_liveness_is_touched_and_an_abandoned_start_ends_the_session(
    store: Store,
) -> None:
    recorder = TerminalRecorder(store, sleep=_no_sleep)
    session_id = await recorder.start(user=_USER, machine="gx10", workspace=None)

    await recorder.touch_live()
    await recorder.abandon(session_id)
    await recorder.touch_live()

    async with store.engine.acquire() as conn:
        seen = await conn.fetchval(
            "SELECT count(*) FROM session_liveness WHERE session_id = $1",
            session_id,
        )
        ended = await conn.fetchval(
            "SELECT agentsession_ended FROM inquiries WHERE id = $1",
            session_id,
        )
    assert seen == 1
    assert ended is not None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
