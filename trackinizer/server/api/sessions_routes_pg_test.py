"""The session and message routes against a session-scoped Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

import asyncio
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from treekle import from_plain

import httpx2
import pytest

from trackinizer.server.api import edit, sessions_routes
from trackinizer.server.api.conftest import TEST_USER_EMAIL, make_test_identity
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.api.session_test_support import ServingRelay
from trackinizer.server.auth import current_user
from trackinizer.server.config import Config
from trackinizer.server.store.store_test_support import seed_active_user
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Each route served by an app holding the router, over a real Store."""

    async def test_session_start_route_renegotiates_actor(
        self,
        integ_store: Store,
    ) -> None:
        """``POST /api/sessions/start`` returns a live-unique granted actor."""
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        # Session-authed (api_key_id=None) so the created change_log row needs
        # no api_keys FK row.
        async def _identity() -> object:
            return make_test_identity(api_key_id=None)

        await seed_active_user(integ_store, TEST_USER_EMAIL)
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                first = await http.post(
                    "/api/sessions/start",
                    json={"cli": "claude", "actor": "scientist"},
                )
                assert first.status_code == 201, first.text
                first_body = from_plain(first.json(), dict[str, object])
                assert from_plain(first_body["actor"], str) == "scientist"
                # A second concurrent start with the same name is suffixed.
                second = await http.post(
                    "/api/sessions/start",
                    json={"cli": "claude", "actor": "scientist"},
                )
                assert second.status_code == 201, second.text
                second_body = from_plain(second.json(), dict[str, object])
                assert from_plain(second_body["actor"], str) == "scientist#2"

                # ``rooms`` round-trips: start with membership, read it back
                # off the stored AgentSession row.
                roomed = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "eng", "rooms": ["sear", "lab"]},
                )
                assert roomed.status_code == 201, roomed.text
                sid = UUID(
                    from_plain(from_plain(roomed.json(), dict[str, object])["id"], str),
                )
        finally:
            app.dependency_overrides.pop(current_user, None)
        row = await integ_store.get_inquiry(sid)
        assert isinstance(row, AgentSession)
        assert row.rooms is not None
        assert sorted(row.rooms) == ["lab", "sear"]

    async def test_rooms_field_edits_round_trip_through_route(
        self,
        integ_store: Store,
    ) -> None:
        """Rooms is editable via the generated field routes (PUT/PATCH/DELETE).

        Regression: ``AgentSession.rooms`` declares ``list_verb_stem`` so the
        wire table generates ``set_rooms`` / ``add_room`` / ``remove_room``
        routes. Before the backing Store methods existed every such edit 500'd
        (the SPA exposes a rooms editor). Proves the full chain -- route ->
        Store setter -> audited change -> stored row -- works for set, add, and
        remove, with each edit landing an auditable ``agentsession_rooms``
        change row.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.include_router(sessions_routes.router)
        app.include_router(edit.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="editor@test")

        await seed_active_user(integ_store, "editor@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "router-eng", "rooms": ["sear"]},
                )
                sid = from_plain(from_plain(start.json(), dict[str, object])["id"], str)

                # PUT overwrites the whole membership.
                put = await http.put(
                    f"/api/agentsession/{sid}/rooms",
                    json={"value": ["lab", "ops"], "actor": "editor@test"},
                )
                assert put.status_code == 200, put.text
                row = await integ_store.get_inquiry(uuid.UUID(sid))
                assert isinstance(row, AgentSession)
                assert sorted(row.rooms or ()) == ["lab", "ops"]

                # PATCH add joins one more room.
                added = await http.patch(
                    f"/api/agentsession/{sid}/rooms",
                    json={"op": "add", "value": "sear", "actor": "editor@test"},
                )
                assert added.status_code == 200, added.text
                row = await integ_store.get_inquiry(uuid.UUID(sid))
                assert isinstance(row, AgentSession)
                assert sorted(row.rooms or ()) == ["lab", "ops", "sear"]

                # PATCH remove leaves the rest.
                removed = await http.patch(
                    f"/api/agentsession/{sid}/rooms",
                    json={"op": "sub", "value": "lab", "actor": "editor@test"},
                )
                assert removed.status_code == 200, removed.text
                row = await integ_store.get_inquiry(uuid.UUID(sid))
                assert isinstance(row, AgentSession)
                assert sorted(row.rooms or ()) == ["ops", "sear"]
        finally:
            app.dependency_overrides.pop(current_user, None)

        # Each edit emitted an auditable ``agentsession_rooms`` change row that
        # PRESERVES the old/new value -- not just a change kind with a NULL
        # snapshot. Regression for the AgentSession audit-loss bug: the
        # change_log lacked ``old_/new_agentsession_rooms`` mirror columns, so
        # the value was silently dropped from the INSERT.
        async with integ_store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT old_agentsession_rooms, new_agentsession_rooms "
                "FROM change_log "
                "WHERE subject_id = $1 AND kind = 'agentsession_rooms' "
                "ORDER BY created",
                uuid.UUID(sid),
            )
        assert len(rows) == 3
        # First edit (PUT set) overwrote ["sear"] -> ["lab", "ops"].
        old_rooms = cast(list[str], rows[0]["old_agentsession_rooms"])
        new_rooms = cast(list[str], rows[0]["new_agentsession_rooms"])
        last_rooms = cast(list[str], rows[2]["new_agentsession_rooms"])
        assert sorted(old_rooms) == ["sear"]
        assert sorted(new_rooms) == ["lab", "ops"]
        # Last edit (remove "lab") landed ["ops", "sear"] as the new snapshot.
        assert sorted(last_rooms) == ["ops", "sear"]

    async def test_send_resolves_actor_room_to_live_session(
        self,
        integ_store: Store,
    ) -> None:
        """``POST /api/messages`` resolves ``@actor:room`` to live sessions.

        Proves the step-9 name-resolution front door: a send addressed to a
        routing name (optionally room-scoped) reaches the live session's agent
        process, and a non-matching room delivers nothing (drop-if-absent).
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.state.session_relay = relay = ServingRelay()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "scientist", "rooms": ["sear"]},
                )
                sid = from_plain(from_plain(start.json(), dict[str, object])["id"], str)

                # Room-scoped send to the right room reaches the session.
                hit = await http.post(
                    "/api/messages",
                    json={"actor": "scientist", "room": "sear", "text": "go"},
                )
                assert hit.status_code == 200, hit.text
                assert from_plain(
                    from_plain(hit.json(), dict[str, object])["delivered"],
                    list[str],
                ) == [
                    sid,
                ]

                # Wrong room matches nothing (undelivered).
                miss = await http.post(
                    "/api/messages",
                    json={"actor": "scientist", "room": "other", "text": "go"},
                )
                assert (
                    from_plain(
                        from_plain(miss.json(), dict[str, object])["delivered"],
                        list[object],
                    )
                    == []
                )

                # The reaching send went to the session's agent process, under the
                # attested sender.
                assert relay.sent == [
                    (
                        UUID(sid),
                        {"py/object": "UserMessage", "content": "go"},
                        "sender@test",
                    ),
                ]
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_bare_actor_rejects_multi_room_session(
        self,
        integ_store: Store,
    ) -> None:
        """A bare ``@actor`` send is rejected when the session spans >1 room.

        A single PTY interleaves every room's messages, so an unscoped send to
        a multi-room session has no room context to inject; the server returns
        409 telling the caller to name a room.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.state.session_relay = ServingRelay()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                started = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "multi", "rooms": ["a", "b"]},
                )
                assert started.status_code == 201, started.text
                # Bare send: ambiguous across rooms a and b -> 409.
                bare = await http.post(
                    "/api/messages",
                    json={"actor": "multi", "text": "go"},
                )
                assert bare.status_code == 409, bare.text
                assert "address one explicitly" in from_plain(
                    from_plain(bare.json(), dict[str, object])["detail"],
                    str,
                )
                # Naming a room resolves it.
                scoped = await http.post(
                    "/api/messages",
                    json={"actor": "multi", "room": "a", "text": "go"},
                )
                assert scoped.status_code == 200, scoped.text
                assert (
                    len(
                        from_plain(
                            from_plain(scoped.json(), dict[str, object])["delivered"],
                            list[object],
                        ),
                    )
                    == 1
                )
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_is_idempotent_on_key_replay(self, integ_store: Store) -> None:
        """A replayed ``Idempotency-Key`` sends once, not once per retry.

        The send path writes no ``change_log`` row, so it cannot dedup the way
        DB writes do; the route's replay receipt answers a replay with the
        first result. Without this a client retry (lost response) double-sends
        into the live session.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.state.session_relay = relay = ServingRelay()
        # The dedup key reaches the route via request.state, set by the
        # middleware from the Idempotency-Key header.
        app.add_middleware(ChangeIdMiddleware)
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "idem", "rooms": ["sear"]},
                )
                sid = from_plain(from_plain(start.json(), dict[str, object])["id"], str)
                key = str(uuid.uuid4())
                body = {"actor": "idem", "room": "sear", "text": "once"}
                first = await http.post(
                    "/api/messages",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                replay = await http.post(
                    "/api/messages",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert from_plain(
                    from_plain(first.json(), dict[str, object])["delivered"],
                    list[str],
                ) == [sid]
                # Replay returns the same receipt, but does not send again.
                assert from_plain(
                    from_plain(replay.json(), dict[str, object])["delivered"],
                    list[str],
                ) == [sid]
                assert [payload for _, payload, _ in relay.sent] == [
                    {"py/object": "UserMessage", "content": "once"},
                ]
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_concurrent_same_key_sends_once(
        self,
        integ_store: Store,
    ) -> None:
        """Two concurrent same-key sends send exactly one message.

        Two racing same-``Idempotency-Key`` sends must not both pass the dedup
        check and double-send: the replay receipt holds the second until the
        first is done, and answers it with the same result.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.state.session_relay = relay = ServingRelay()
        # The dedup key reaches the route via request.state, set by the
        # middleware from the Idempotency-Key header.
        app.add_middleware(ChangeIdMiddleware)
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "race", "rooms": ["sear"]},
                )
                sid = from_plain(from_plain(start.json(), dict[str, object])["id"], str)
                key = str(uuid.uuid4())
                body = {"actor": "race", "room": "sear", "text": "once"}
                first, second = await asyncio.gather(
                    http.post(
                        "/api/messages",
                        json=body,
                        headers={"Idempotency-Key": key},
                    ),
                    http.post(
                        "/api/messages",
                        json=body,
                        headers={"Idempotency-Key": key},
                    ),
                )
                assert from_plain(
                    from_plain(first.json(), dict[str, object])["delivered"],
                    list[str],
                ) == [sid]
                assert from_plain(
                    from_plain(second.json(), dict[str, object])["delivered"],
                    list[str],
                ) == [sid]
                # Exactly one copy despite two concurrent same-key sends.
                assert [payload for _, payload, _ in relay.sent] == [
                    {"py/object": "UserMessage", "content": "once"},
                ]
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_send_skips_session_ended_after_resolve(
        self,
        integ_store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A session ended between resolve and send is not sent to.

        ``resolve_live_sessions`` and the send are separate steps; a concurrent
        ``end`` in that window must not hand a message to a session that is over.
        The route re-checks ``ended`` immediately before it sends. Simulated by
        stubbing ``resolve`` to return the now-ended session so the route's
        pre-send liveness re-check fires.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.state.session_relay = relay = ServingRelay()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="sender@test")

        await seed_active_user(integ_store, "sender@test")
        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                start = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "ending", "rooms": ["sear"]},
                )
                sid = UUID(
                    from_plain(from_plain(start.json(), dict[str, object])["id"], str),
                )
                # Close via ``end_session`` (ended + status=complete together)
                # so the AgentSession lifecycle CHECK holds.
                await integ_store.end_session(
                    sid,
                    ended=datetime.now(UTC),
                    actor="ending",
                )

                async def _stale(
                    actor: str,
                    *,
                    room: str | None = None,
                ) -> list[tuple[uuid.UUID, tuple[str, ...]]]:
                    del actor, room
                    return [(sid, ("sear",))]

                monkeypatch.setattr(integ_store, "resolve_live_sessions", _stale)
                resp = await http.post(
                    "/api/messages",
                    json={"actor": "ending", "room": "sear", "text": "late"},
                )
                assert from_plain(resp.json(), dict[str, object])["delivered"] == []
                assert relay.sent == []
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_session_start_rejects_blank_rooms(self, integ_store: Store) -> None:
        """Blank/whitespace room names are rejected at the API boundary (422).

        Mirrors the subscribers validator: a blank entry is almost certainly a
        client bug and silently dropping it (storage canonicalization) hides
        it. ``rooms=["  "]`` is a 422, not a 201 with empty membership.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config()
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None)

        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                resp = await http.post(
                    "/api/sessions/start",
                    json={"cli": "codex", "actor": "eng", "rooms": ["  "]},
                )
                assert resp.status_code == 422, resp.text
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_an_end_with_no_time_stamps_a_real_one(
        self,
        integ_store: Store,
    ) -> None:
        """REV-31: an end whose body names no ``ended`` still stamps a real time."""
        sid = await integ_store.submit_agentsession(
            SubmitAgentSession(account="router@test", title="inbox", cli="claude"),
            audience="open",
        )
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.state.config = Config(chat_orgs="single")
        app.include_router(sessions_routes.router)

        async def _identity() -> object:
            return make_test_identity(api_key_id=None, email="router@test")

        app.dependency_overrides[current_user] = _identity
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                end = await http.post(f"/api/sessions/{sid}/end", json={})
                assert end.status_code == 200, end.text
                assert from_plain(end.json(), dict[str, object])["ended"] is not None
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_session_end_retry_same_key_replays_not_409(
        self,
        integ_store: Store,
    ) -> None:
        """A retried POST /end with the same Idempotency-Key replays 200.

        The original close raised three audit emits but is anchored on the
        ``agentsession_ended`` change_log row keyed by the request's
        idempotency UUID; a same-key retry replays the original success
        instead of 409-ing on the already-ended guard. A *different* key
        ending the already-closed session is a genuine second close -> 409.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.add_middleware(ChangeIdMiddleware)

        # The already-ended-different-key path raises ConflictError; translate
        # it to 409 (as app.py's conflict_handler does) so the test sees the
        # HTTP status, not a re-raised exception.
        async def _conflict_to_409(request: Request, exc: Exception) -> JSONResponse:
            del request
            return JSONResponse(status_code=409, content={"detail": str(exc)})

        app.add_exception_handler(ConflictError, _conflict_to_409)
        app.include_router(sessions_routes.router)
        identity = make_test_identity(api_key_id=None)

        await seed_active_user(integ_store, TEST_USER_EMAIL)

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                r = await http.post("/api/sessions/start", json={"cli": "codex"})
                assert r.status_code == 201, r.text
                session_id = from_plain(
                    from_plain(r.json(), dict[str, object])["id"],
                    str,
                )

                key = str(uuid.uuid4())
                # Empty body: the route stamps a fresh ``now()``. The replay
                # must echo the COMMITTED ended, so two same-key calls return
                # the IDENTICAL timestamp -- not two distinct ``now()`` values.
                body: dict[str, str] = {}
                first = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert first.status_code == 200, first.text

                # Same key -> replay the original receipt (same committed
                # ended), not 409 and not a fresh now().
                retry = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert retry.status_code == 200, retry.text
                assert retry.json() == first.json()
                assert (
                    from_plain(retry.json(), dict[str, object])["ended"]
                    == from_plain(first.json(), dict[str, object])["ended"]
                )

                # A different key on the already-ended session -> 409.
                other = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                assert other.status_code == 409, other.text
        finally:
            app.dependency_overrides.pop(current_user, None)

    async def test_session_end_with_cli_backfill_retry_replays_not_409(
        self,
        integ_store: Store,
    ) -> None:
        """End-with-cli-backfill retry replays 200 (the K-on-cli-row case).

        When the close ALSO backfills ``cli_session_id``, that emit fires first
        and consumes the client key K, so K lands on the
        ``agentsession_cli_session_id`` change_log row, not the
        ``agentsession_ended`` one. The replay probe (now keyed on id+subject,
        not kind) must still match, so a same-key retry replays 200 instead of
        falsely 409-ing.
        """
        app = FastAPI()
        app.state.engine = integ_store.engine
        app.state.store = integ_store
        app.add_middleware(ChangeIdMiddleware)

        async def _conflict_to_409(request: Request, exc: Exception) -> JSONResponse:
            del request
            return JSONResponse(status_code=409, content={"detail": str(exc)})

        app.add_exception_handler(ConflictError, _conflict_to_409)
        app.include_router(sessions_routes.router)
        identity = make_test_identity(api_key_id=None)

        await seed_active_user(integ_store, TEST_USER_EMAIL)

        async def _identity_override() -> object:
            return identity

        app.dependency_overrides[current_user] = _identity_override
        transport = httpx2.ASGITransport(app=app)
        try:
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http:
                # Start WITHOUT a cli_session_id so the end-time backfill is a
                # real change (the cli emit then consumes K).
                r = await http.post("/api/sessions/start", json={"cli": "codex"})
                assert r.status_code == 201, r.text
                session_id = from_plain(
                    from_plain(r.json(), dict[str, object])["id"],
                    str,
                )

                key = str(uuid.uuid4())
                body = {"ended": "2026-05-31T16:00:00Z", "cli_session_id": "vendor-9"}
                first = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert first.status_code == 200, first.text

                # Same key + same body: the backfill put K on the cli row, but
                # the retry still replays 200 (probe by id+subject).
                retry = await http.post(
                    f"/api/sessions/{session_id}/end",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                assert retry.status_code == 200, retry.text
                assert retry.json() == first.json()
        finally:
            app.dependency_overrides.pop(current_user, None)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
