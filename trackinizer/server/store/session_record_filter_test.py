"""Filtering AgentSessions by what their IR records say.

``trax agentsession tool_call re bar`` asks which SESSIONS mention something.
The field is list-shaped -- any record of that kind matching satisfies the
clause -- so it means what ``labels re x`` already means, with the elements in
a side table rather than an array on the row.

Every test runs against real Postgres, because the properties are the
database's: the correlated ``EXISTS``, the tsvector prefilter narrowing before
the regex, and the ciphertext that must never be matchable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import (
    ContextCompaction,
    SessionRecord,
    ShellCommandResult,
    Thinking,
    ToolCall,
    UserMessage,
)
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.inquiries import AgentSession
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.filters import Filter


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """A bootstrapped store on the shared integration database."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _session_with(
    store: Store, records: Sequence[SessionRecord], *, title: str = "s"
) -> UUID:
    """An AgentSession holding ``records``, numbered by stream position."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', $2)",
            session_id,
            title,
        )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(session_id=session_id, part=0, idx=idx, record=record)
            for idx, record in enumerate(records)
        ],
    )
    return session_id


async def _matching(store: Store, *filters: Filter, lowering: bool = True) -> set[UUID]:
    """The AgentSession ids ``filters`` select."""
    rows = await store.list_kind(
        "AgentSession", filters=list(filters), limit=500, lowering=lowering
    )
    return {row.id for row in rows if isinstance(row, AgentSession)}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_phrase_in_a_tool_result_matches(store: Store) -> None:
    """The prose a tool printed is searchable, and scoped to its record kind."""
    hit = await _session_with(
        store,
        [ShellCommandResult(call_id="c", command=("ls",), stdout="pg_advisory_lock")],
    )
    miss = await _session_with(store, [UserMessage(content="unrelated")])

    found = await _matching(
        store, Filter(field="shell_command_result", op="re", value="advisory")
    )

    assert hit in found
    assert miss not in found


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_phrase_in_ciphertext_does_not_match(store: Store) -> None:
    """Sealed reasoning is never searchable, which is why it is split out.

    The whole point of the ciphertext table is that dropping it leaves the
    record searchable -- and the converse: what is IN it was never indexed, so
    a filter cannot reach it even before retention runs.
    """
    sealed = await _session_with(
        store, [Thinking(encrypted="c2VjcmV0cGhyYXNlaGVyZQ==", content="")]
    )

    found = await _matching(store, Filter(field="thinking", op="re", value="c2VjcmV0"))

    assert sealed not in found


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_with_two_matching_records_is_returned_once(
    store: Store,
) -> None:
    """The clause is EXISTS, not a join: two hits are still one row.

    A join would multiply the session by its matching records, and the list's
    LIMIT would then window records rather than sessions.
    """
    session_id = await _session_with(
        store,
        [
            ToolCall(call_id="a", name="Bash", arguments={"command": "grep foo"}),
            ToolCall(call_id="b", name="Bash", arguments={"command": "grep foo"}),
        ],
    )

    rows = await store.list_kind(
        "AgentSession",
        filters=[Filter(field="tool_call", op="re", value="grep")],
        limit=500,
    )

    assert [row.id for row in rows].count(session_id) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_context_compaction_notnull_finds_a_compacted_session(
    store: Store,
) -> None:
    """Presence of the record kind, no operand: did a compact happen."""
    compacted = await _session_with(store, [ContextCompaction(summary="earlier turns")])
    plain = await _session_with(store, [UserMessage(content="hello")])

    found = await _matching(
        store, Filter(field="context_compaction", op="notnull", value="")
    )

    assert compacted in found
    assert plain not in found


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_record_filter_composes_with_an_inquiries_filter(
    store: Store,
) -> None:
    """Both clauses AND into one WHERE, over different tables."""
    wanted = await _session_with(
        store, [UserMessage(content="deploy the thing")], title="keep"
    )
    other = await _session_with(
        store, [UserMessage(content="deploy the thing")], title="drop"
    )

    found = await _matching(
        store,
        Filter(field="user_message", op="re", value="deploy"),
        Filter(field="title", op="is", value="keep"),
    )

    assert wanted in found
    assert other not in found


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_two_evaluators_select_the_same_rows(store: Store) -> None:
    """The existing equivalence gate, extended to record fields.

    A filter that lowers must select what the Python predicate would; a
    disagreement is a wrong answer the caller cannot detect, since neither
    evaluator reports which one ran.
    """
    await _session_with(
        store,
        [
            UserMessage(content="alpha beta"),
            # A path-shaped argument: it lexes as ONE token, so a tsvector
            # prefilter would miss the substring the regex below matches.
            ToolCall(call_id="c", name="Read", arguments={"path": "/var/gamma"}),
        ],
    )
    await _session_with(store, [UserMessage(content="delta")])

    for clause in (
        Filter(field="user_message", op="re", value="alpha"),
        Filter(field="user_message", op="nre", value="alpha"),
        Filter(field="user_message", op="is", value="delta"),
        Filter(field="tool_call", op="notnull", value=""),
        Filter(field="tool_call", op="isnull", value=""),
        Filter(field="tool_call", op="re", value="gamma"),
    ):
        lowered = await _matching(store, clause)
        in_python = await _matching(store, clause, lowering=False)
        assert lowered == in_python, f"{clause.field} {clause.op} disagreed"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_pattern_with_no_word_characters_still_matches(store: Store) -> None:
    """The tsvector prefilter must not drop rows the regex would keep.

    ``plainto_tsquery('simple', '-->')`` is an EMPTY query, which matches
    nothing -- so a prefilter applied unconditionally would silently answer
    "no sessions" for any punctuation-only pattern.
    """
    session_id = await _session_with(store, [UserMessage(content="a --> b")])

    found = await _matching(store, Filter(field="user_message", op="re", value="-->"))

    assert session_id in found


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_record_kind_scopes_the_match(store: Store) -> None:
    """The field names ONE kind: a phrase under another does not satisfy it."""
    session_id = await _session_with(
        store, [UserMessage(content="findme"), ToolCall(call_id="c", name="Read")]
    )

    as_user = await _matching(
        store, Filter(field="user_message", op="re", value="findme")
    )
    as_tool = await _matching(store, Filter(field="tool_call", op="re", value="findme"))

    assert session_id in as_user
    assert session_id not in as_tool


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
