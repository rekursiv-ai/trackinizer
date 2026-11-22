"""Session starts, ends, resumes, and the feed against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncio
import uuid

import asyncpg
import pytest

from trackinizer.conftest import new_uuid
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.server.store.store_test_support import (
    EVERY_SESSION,
    append_user_message,
    fetch_value,
)
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession, SubmitIssue


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Session starts, ends, resumes, and the feed, each read back from the database."""

    async def test_start_session_correlates_resume_by_cli_session_id(
        self,
        integ_store: Store,
    ) -> None:
        """A start with a known ``cli_session_id`` re-attaches the prior session.

        Resume: the same CLI session id maps back to the original AgentSession
        (same id, same granted handle), re-opens it if ended, and reports the
        event log's continuation seq so the resumed run appends instead of
        forking a new seq=0 log.
        """
        # Fresh start with a cli_session_id, capture one event, then end it.
        sid, owner, seq0 = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="abc123",
            ),
            requested_actor="scientist",
            audience="open",
        )
        assert seq0 == 0
        await append_user_message(integ_store, sid, idx=0, text="first")
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor=owner)

        # Resume: same cli_session_id re-attaches the SAME session + handle and
        # re-opens it. This is the correlation phase 6's resume depends on.
        rsid, rowner, rseq = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="abc123",
            ),
            requested_actor="scientist",
            audience="open",
        )
        assert rsid == sid
        assert rowner == owner
        # ``rseq`` is always 0 now: it continued the legacy event log, and a
        # record's key is derived from its position in its source file.
        assert rseq == 0
        # The session is live again (re-opened), so the append succeeds and
        # continues the SAME part rather than forking one.
        await append_user_message(integ_store, sid, idx=1, text="second")
        rows = await integ_store.read_session_records(sid, part=0, limit=100)
        assert [row.idx for row in rows] == [0, 1]

    async def test_resume_is_principal_scoped(self, integ_store: Store) -> None:
        """A resume only re-attaches a session opened by the SAME credential (B2).

        Correlating on ``cli_session_id`` alone would let any writer resume
        another principal's session (cross-principal hijack). Resume must be
        scoped to ``opened_by_api_key_id``, exactly as the inbound-drain route
        is. A different credential gets a FRESH session, not the other's.
        """
        alice_key, bob_key = uuid.uuid4(), uuid.uuid4()
        async with integ_store.engine.acquire() as conn:
            for kid, email in ((alice_key, "alice@x"), (bob_key, "bob@x")):
                uid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO users (id, email, name, role, status) "
                    "VALUES ($1, $2, 'u', 'writer', 'active')",
                    uid,
                    email,
                )
                await conn.execute(
                    "INSERT INTO api_keys (id, user_id, name, prefix, "
                    "secret_hash, role) VALUES ($1, $2, 'k', $3, 'h', 'writer')",
                    kid,
                    uid,
                    str(kid)[:8],
                )
        # Alice opens a session with a cli_session_id.
        a_sid, _a_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="claude",
                cli_session_id="shared-id",
            ),
            requested_actor="alice",
            api_key_id=alice_key,
            audience="open",
        )
        # Bob, knowing the cli_session_id, must NOT resume Alice's session.
        b_sid, _b_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="b",
                cli="claude",
                cli_session_id="shared-id",
            ),
            requested_actor="bob",
            api_key_id=bob_key,
            audience="open",
        )
        assert b_sid != a_sid  # Bob got a fresh session, not Alice's.

        # Alice resuming her OWN session re-attaches it.
        a_resume, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="claude",
                cli_session_id="shared-id",
            ),
            requested_actor="alice",
            api_key_id=alice_key,
            audience="open",
        )
        assert a_resume == a_sid

    async def test_resume_no_auth_matches_by_cli_session_id_alone(
        self,
        integ_store: Store,
    ) -> None:
        """In ``--no-auth`` (api_key_id=None) resume scopes on cli_session_id only.

        Every no-auth session stamps ``opened_by = NULL``, so the
        ``IS NOT DISTINCT FROM NULL`` scope degrades to the cli_session_id --
        and a None-credentialed resume re-attaches the original session. (There
        is no per-principal isolation to enforce in no-auth mode.)
        """
        sid, _owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="noauth-id",
            ),
            requested_actor="scientist",
            api_key_id=None,
            audience="open",
        )
        resumed, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="noauth-id",
            ),
            requested_actor="scientist",
            api_key_id=None,
            audience="open",
        )
        assert resumed == sid  # re-attached, not a fresh session.

    async def test_resume_applies_rooms_on_live_session(
        self,
        integ_store: Store,
    ) -> None:
        """Resuming a still-LIVE session (not ended) also joins new rooms.

        The rooms-apply runs whether or not the session was ended, so a resume
        that adds a room to a live re-attach joins it (and re-adding an existing
        room is an idempotent no-op).
        """
        sid, _owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="live-id",
                rooms=["a"],
            ),
            requested_actor="scientist",
            audience="open",
        )
        # No end_session: the session is still live. Resume with a new room.
        resumed, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="live-id",
                rooms=["a", "b"],
            ),
            requested_actor="scientist",
            audience="open",
        )
        assert resumed == sid
        row = await integ_store.get_inquiry(sid)
        assert isinstance(row, AgentSession)
        assert set(row.rooms or ()) == {"a", "b"}

    async def test_resume_applies_new_body_fields(self, integ_store: Store) -> None:
        """A resume applies new SubmitAgentSession fields, e.g. rooms (B7).

        A resume with ``--room workshop`` must JOIN the new room, not
        silently drop it. The resume tx applies the request's rooms.
        """
        sid, owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="rooms-id",
                rooms=["a"],
            ),
            requested_actor="scientist",
            audience="open",
        )
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor=owner)
        # Resume with a new room set.
        rsid, _, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="rooms-id",
                rooms=["a", "b"],
            ),
            requested_actor="scientist",
            audience="open",
        )
        assert rsid == sid
        row = await integ_store.get_inquiry(sid)
        assert isinstance(row, AgentSession)
        assert set(row.rooms or ()) == {"a", "b"}

    async def test_resume_audit_attributed_to_resuming_caller(
        self,
        integ_store: Store,
    ) -> None:
        """The re-open audit names the resuming caller, not the original owner (B3)."""
        sid, owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="audit-id",
            ),
            requested_actor="alice",
            audience="open",
        )
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor=owner)
        # A different actor resumes; the re-open change must credit THEM.
        await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="run",
                cli="claude",
                cli_session_id="audit-id",
            ),
            requested_actor="bob",
            audience="open",
        )
        async with integ_store.engine.acquire() as conn:
            actor = await fetch_value(
                conn,
                "SELECT actor FROM change_log WHERE subject_id = $1 "
                "AND kind = 'agentsession_ended' ORDER BY created DESC LIMIT 1",
                sid,
            )
        assert actor == "bob"

    async def test_start_session_fresh_when_no_cli_session_id_match(
        self,
        integ_store: Store,
    ) -> None:
        """A null or unmatched ``cli_session_id`` opens a fresh session.

        Two fresh runs with no cli_session_id must NOT correlate to each other
        (the null-match trap): each gets its own session + handle.
        """
        sid_a, owner_a, _ = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="a", cli="claude"),
            requested_actor="scientist",
            audience="open",
        )
        sid_b, owner_b, _ = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="b", cli="claude"),
            requested_actor="scientist",
            audience="open",
        )
        assert sid_a != sid_b
        assert (owner_a, owner_b) == ("scientist", "scientist#2")

    async def test_start_session_replay_drains_leaked_change_id(
        self,
        integ_store: Store,
    ) -> None:
        """A ``start_session`` replay must not leak the change-id into the next write.

        ``start_session`` peeks the client change-id (``_CLIENT_CHANGE_ID``) for
        its replay probe and returns early on a hit WITHOUT consuming it. If it
        leaves the slot set, the NEXT write in the same context wrongly consumes
        that key and either collides on ``change_log.id`` or mis-attributes the
        mutation. The replay-return path must drain the slot, exactly as
        ``_submit_on_conn`` does. (HTTP is safe via task-scoping + middleware;
        this guards direct/same-context callers.)
        """
        key = new_uuid()
        # First start mints the session under key K.
        set_client_change_id(key)
        sid, _owner, _seq = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex"),
            requested_actor="alice",
            audience="open",
        )
        # Replay the same start: K is on the slot, the probe peeks it, returns
        # the original (id, owner) -- and MUST drain the slot.
        set_client_change_id(key)
        replay_sid, _, _ = await integ_store.start_session(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex"),
            requested_actor="alice",
            audience="open",
        )
        assert replay_sid == sid
        # The leaked key must NOT survive into a subsequent keyless write.
        new_sid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="after"),
        )
        async with integ_store.engine.acquire() as conn:
            change_id = await fetch_value(
                conn,
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
                new_sid,
            )
        assert change_id != key

    async def test_end_session_replay_drains_leaked_change_id(
        self,
        integ_store: Store,
    ) -> None:
        """An ``end_session`` replay must not leak the change-id into the next write."""
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex"),
            audience="open",
        )
        key = new_uuid()
        set_client_change_id(key)
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        # Replay the same end under the same key: probe peeks, returns -- drain.
        set_client_change_id(key)
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        # A subsequent keyless write must not consume the leaked key.
        new_sid = await integ_store.submit_issue(
            SubmitIssue(account="tester@example.com", title="after"),
        )
        async with integ_store.engine.acquire() as conn:
            change_id = await fetch_value(
                conn,
                "SELECT id FROM change_log WHERE subject_id = $1 AND kind = 'created'",
                new_sid,
            )
        assert change_id != key

    async def test_end_session_always_lands_complete(self, integ_store: Store) -> None:
        """``end_session`` hardcodes ``status='complete'`` -- no free status param.

        The only legal terminal status for an ended AgentSession is
        ``complete`` (the lifecycle CHECK ties ``ended IS NOT NULL`` to
        ``status='complete'``). ``end_session`` therefore takes no ``status``
        argument; it always stamps ``complete``, so a caller cannot land a
        desynced (ended, non-complete) row through it.
        """
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="run", cli="codex"),
            audience="open",
        )
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        async with integ_store.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, agentsession_ended FROM inquiries WHERE id = $1",
                sid,
            )
        assert row is not None
        assert row["status"] == "complete"
        assert row["agentsession_ended"] is not None

    async def test_reserve_session_actor_suffixes_across_all_sessions(
        self,
        integ_store: Store,
    ) -> None:
        """A routing name is reserved for a session's LIFETIME, never reused.

        A free name passes through; a name held by ANY session (live or ended)
        gets the smallest free ``#N`` suffix. Handles are monotonic, like a
        sequence: ending a session does NOT free its name, because the session
        may resume later and must reclaim its original handle.
        """
        # Free name: returned unchanged.
        assert await integ_store.reserve_session_actor("scientist") == "scientist"

        # Open a session owning "scientist".
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="s",
                cli="claude",
                owner="scientist",
            ),
            actor="scientist",
            audience="open",
        )
        assert await integ_store.reserve_session_actor("scientist") == "scientist#2"

        # A second "scientist" pushes the next reservation to #3.
        await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="s2",
                cli="claude",
                owner="scientist#2",
            ),
            actor="scientist#2",
            audience="open",
        )
        assert await integ_store.reserve_session_actor("scientist") == "scientist#3"

        # Ending the first does NOT free "scientist": the handle is held for the
        # session's lifetime (it may resume). Reservation still skips to #3.
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="scientist")
        assert await integ_store.reserve_session_actor("scientist") == "scientist#3"

    async def test_concurrent_session_starts_get_distinct_routing_names(
        self,
        integ_store: Store,
    ) -> None:
        """Two concurrent starts for one actor get distinct routing names.

        The advisory reserve-then-insert window let both starts read the name
        as free and both win it. The live-owner partial unique index plus
        ``start_session``'s retry make the DB the arbiter, so one gets
        ``scientist`` and the other ``scientist#2`` -- never both ``scientist``.
        """
        results = await asyncio.gather(
            integ_store.start_session(
                SubmitAgentSession(
                    account="tester@example.com",
                    title="a",
                    cli="codex",
                ),
                requested_actor="scientist",
                audience="open",
            ),
            integ_store.start_session(
                SubmitAgentSession(
                    account="tester@example.com",
                    title="b",
                    cli="codex",
                ),
                requested_actor="scientist",
                audience="open",
            ),
        )
        granted = sorted(name for _, name, _ in results)
        assert granted == ["scientist", "scientist#2"]
        # Both rows persisted with the names they were granted.
        for sid, name, _ in results:
            row = await integ_store.get_inquiry(sid)
            assert isinstance(row, AgentSession)
            assert row.owner == name

    async def test_start_session_same_key_replays_original_owner(
        self,
        integ_store: Store,
    ) -> None:
        """A same-key start retry replays the original (id, owner), no phantom.

        ``start_session`` probes the idempotency key BEFORE reserving, so a
        retry returns the original ``alice`` instead of reserving a fresh
        ``alice#2`` (which would burn a suffix and leave the routing handle
        wrong). Two same-key starts therefore yield the identical id and owner.
        """
        key = new_uuid()
        first_id, first_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="codex",
                idempotency_key=key,
            ),
            requested_actor="alice",
            audience="open",
        )
        retry_id, retry_owner, _ = await integ_store.start_session(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="codex",
                idempotency_key=key,
            ),
            requested_actor="alice",
            audience="open",
        )
        assert retry_id == first_id
        assert first_owner == "alice"
        assert retry_owner == "alice"

    async def test_agentsession_lifecycle_check_forbids_zombies(
        self,
        integ_store: Store,
    ) -> None:
        """The DB CHECK ties ``ended`` to ``status='complete'`` for sessions.

        Both zombie states are unrepresentable at storage: born ended while
        not complete (A2a), or marked complete while ended stays NULL (A2b).
        A live session may still be abandoned/invalidated (ended NULL).
        """
        # A2a: a create-time ``ended`` (status defaults 'active') is rejected.
        # The submit body no longer carries ``ended``; drive the raw column
        # to prove the storage CHECK is the enforcement point.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with integ_store.engine.acquire() as conn:
                await insert_inquiry(
                    conn,
                    new_uuid(),
                    "AgentSession",
                    values={
                        "title": "zombie",
                        "account": "tester@example.com",
                        "agentsession_audience": "open",
                        "agentsession_ended": datetime.now(UTC),
                    },
                )

        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="tester@example.com", title="live", cli="codex"),
            audience="open",
        )
        # A2b: completing without stamping ended (set_status alone) -> 409.
        with pytest.raises(ConflictError, match="check constraint"):
            await integ_store.set_status(sid, "complete", actor="u")

        # /end stamps ended + status=complete together -> succeeds.
        await integ_store.end_session(sid, ended=datetime.now(UTC), actor="u")
        ended = await integ_store.get_inquiry(sid)
        assert ended is not None
        assert ended.status == "complete"

        # A live session can be abandoned with ``ended`` left NULL.
        sid2 = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="aband",
                cli="codex",
            ),
            audience="open",
        )
        await integ_store.set_status(sid2, "abandoned", actor="u")
        abandoned = await integ_store.get_inquiry(sid2)
        assert isinstance(abandoned, AgentSession)
        assert abandoned.status == "abandoned"
        assert abandoned.ended is None

    async def test_read_feed_interleaves_sessions_and_filters(
        self,
        integ_store: Store,
    ) -> None:
        """The console feed interleaves turns across sessions, in time order.

        Proves the multi-agent console's backing read: turns from two sessions
        come back ordered by the server write clock (``created``), each tagged
        with its session's routing identity (``actor``/``rooms``/``cli``); the
        ``room``/``actor`` filters and the ``since`` keyset cursor narrow it.
        """
        # ``owner`` is the routing identity the feed surfaces as ``actor``;
        # the start route fills it from the granted name (here set directly).
        sci = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="sci",
                cli="codex",
                owner="scientist",
                rooms=["sear"],
            ),
            audience="open",
        )
        eng = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="eng",
                cli="claude",
                owner="eng",
                rooms=["lab"],
            ),
            audience="open",
        )
        # Interleave appends so created-order spans both sessions.
        await append_user_message(integ_store, sci, idx=0, text="a")
        await append_user_message(integ_store, eng, idx=0, text="b")
        await append_user_message(integ_store, sci, idx=1, text="c")

        feed = await integ_store.read_feed(scope=EVERY_SESSION)
        # All three turns, oldest first, each carrying its session context.
        assert len(feed) == 3
        assert [f.actor for f in feed] == ["scientist", "eng", "scientist"]
        assert feed[0].rooms == ["sear"]
        assert feed[0].cli == "codex"
        assert feed[1].cli == "claude"
        # Time-ordered by created across sessions.
        assert [f.created for f in feed] == sorted(f.created for f in feed)

        # Room filter narrows to one session.
        sear_only = await integ_store.read_feed(
            scope=replace(EVERY_SESSION, rooms=("sear",)),
        )
        assert {f.actor for f in sear_only} == {"scientist"}

        # Actor filter likewise.
        eng_only = await integ_store.read_feed(
            scope=replace(EVERY_SESSION, actors=("eng",)),
        )
        assert {f.actor for f in eng_only} == {"eng"}

        # The composite keyset cursor resumes strictly past the given event.
        after_first = await integ_store.read_feed(
            scope=EVERY_SESSION,
            after=(
                feed[0].created,
                feed[0].session_id,
                feed[0].part,
                feed[0].seq,
            ),
        )
        assert [f.actor for f in after_first] == ["eng", "scientist"]

        # ``tail`` returns the newest page (still oldest-first), so a backlog
        # does not force a replay from the beginning.
        tail2 = await integ_store.read_feed(
            scope=EVERY_SESSION,
            limit=2,
            tail=True,
        )
        assert [f.actor for f in tail2] == ["eng", "scientist"]

    async def test_read_feed_composite_cursor_spans_created_ties(
        self,
        integ_store: Store,
    ) -> None:
        """A page boundary inside a same-``created`` group skips no rows.

        The order key is ``(created, session_id, seq)``; a bare-``created``
        cursor would resume at ``> created`` and drop every tied row after the
        boundary. Force two rows in different sessions to share one ``created``
        instant, page with ``limit=1``, and assert the second is reached via
        the composite cursor.
        """
        a = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="a",
                cli="codex",
                owner="a-actor",
            ),
            audience="open",
        )
        b = await integ_store.submit_agentsession(
            SubmitAgentSession(
                account="tester@example.com",
                title="b",
                cli="codex",
                owner="b-actor",
            ),
            audience="open",
        )
        # Force an exact ``created`` tie across the two sessions (the column
        # defaults to clock_timestamp(); set it explicitly here).
        tie = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        async with integ_store.engine.acquire() as conn:
            for sid in (a, b):
                await conn.execute(
                    "INSERT INTO session_records "
                    "(session_id, part, idx, kind, created, payload) "
                    "VALUES ($1, 0, 0, 'UserMessage', $2, '{}'::json)",
                    sid,
                    tie,
                )
                # read_feed bounds by the live manifest prefix (idx < records), so
                # each seeded row needs a manifest covering it -- production writes
                # both together.
                await conn.execute(
                    "INSERT INTO session_manifests "
                    "(session_id, part, name, metadata, ir_id, format, records) "
                    "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), "
                    "'claude', 1)",
                    sid,
                )

        page1 = await integ_store.read_feed(scope=EVERY_SESSION, limit=1)
        assert len(page1) == 1
        first = page1[0]
        assert first.created == tie
        # Resume past the first via the composite cursor; the tied second row
        # (same created, different session) must still be returned.
        page2 = await integ_store.read_feed(
            scope=EVERY_SESSION,
            after=(first.created, first.session_id, first.part, first.seq),
        )
        assert [f.session_id for f in page2] == [
            sid for sid in (a, b) if sid != first.session_id
        ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
