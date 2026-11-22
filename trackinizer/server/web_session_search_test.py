"""The ``/api/web/search_sessions`` handler against real Postgres.

Boots a real store and calls the handler directly with a duck-typed request (the
session-scoped ``integ_engine`` lives on the session loop, so a starlette
``TestClient`` -- which spins its own loop -- cannot drive it; web_test.py calls
handlers the same way). The vector arm is the app's ``vector_search`` slot, here a
fake: an addon fills it in a real deployment. Proves the keyword arm, the vector
arm's place in the answer, and -- the load-bearing case -- that a site without the
arm searches by keyword and says so, never failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from treekle import from_plain

import pytest

from trackinizer.conftest import make_store
from trackinizer.server import web
from trackinizer.server.audience import SessionReader
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Assistant, Config
from trackinizer.server.store.search import VectorHit
from trackinizer.server.store.session_feed import FeedScope


if TYPE_CHECKING:
    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.store.core import Store
    from trackinizer.server.store.search import (
        Kept,
        SessionSearchHit,
        VectorSearch,
    )
    from trackinizer.wire.wire_sessions import FeedWhose, SearchMode


_VIEWER = AuthIdentity(
    user_id=UUID("55555555-5555-5555-5555-555555555555"),
    api_key_id=None,
    email="viewer@example.com",
    role="viewer",
)


class _State:
    """The app state the route reads: the store, its config and the vector arm."""

    def __init__(self, *, store: Store, vector_search: VectorSearch | None) -> None:
        self.store = store
        self.engine = store.engine
        self.config = Config()
        self.vector_search = vector_search


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


class _Request:
    def __init__(self, app: _App) -> None:
        self.app = app
        self.state = SimpleNamespace()


def _request(store: Store, *, vector_search: VectorSearch | None = None) -> Request:
    return cast(
        Request,
        _Request(_App(_State(store=store, vector_search=vector_search))),
    )


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped."""
    return integ_store


async def _session(store: Store, *, title: str = "route search") -> UUID:
    """Insert an open session of the viewer's, which a search finds by default."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$3, $2, 'open')",
            session_id,
            title,
            _VIEWER.email,
        )
    return session_id


async def _record(store: Store, session_id: UUID, *, idx: int, text: str) -> None:
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_records "
            "(session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, $2, 'UserMessage', '{}'::json, $3)",
            session_id,
            idx,
            text,
        )
        # Search bounds by the live manifest prefix (idx < records); grow the
        # part's bound to cover this record -- production writes both together.
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', $2) "
            "ON CONFLICT (session_id, part) DO UPDATE SET "
            "records = GREATEST(session_manifests.records, $2)",
            session_id,
            idx + 1,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fts_arm_returns_hits_with_position_and_title(store: Store) -> None:
    """A keyword search returns hits carrying (session_id, part, idx) + title."""
    session_id = await _session(store, title="deploy log")
    await _record(store, session_id, idx=0, text="advisory lock acquired cleanly")

    body = from_plain(
        await web.web_search_sessions(
            _request(store),
            q="advisory lock",
            identity=_VIEWER,
            mode="keyword",
        ),
        dict[str, object],
    )
    hits = from_plain(body["hits"], list[dict[str, object]])
    assert len(hits) == 1
    hit = from_plain(hits[0], dict[str, object])
    assert hit["session_id"] == str(session_id)
    assert (hit["part"], hit["idx"]) == (0, 0)
    assert hit["title"] == "deploy log"
    assert hit["source"] == "fts"
    assert body["semantic"] is False


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("unloaded", [False, True], ids=["no addon", "not loaded"])
async def test_a_site_without_the_addon_searches_by_keyword(
    store: Store,
    *,
    unloaded: bool,
) -> None:
    """No vector arm, or one whose model is not loaded: keyword hits, degraded.

    The load-bearing contract: a search never fails or loads a model for want of
    vectors. It falls back to keywords and says so.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="postgres deadlock trace")

    body = from_plain(
        await web.web_search_sessions(
            _request(store, vector_search=_Fixed(hits=None) if unloaded else None),
            q="deadlock",
            identity=_VIEWER,
            mode="fused",
        ),
        dict[str, object],
    )

    assert (body["degraded"], body["semantic"]) == (True, False)
    hits = from_plain(body["hits"], list[dict[str, object]])
    assert [from_plain(hit, dict[str, object])["source"] for hit in hits] == ["fts"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_vector_arm_runs_when_the_app_has_one(store: Store) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="deploy the release to production")
    await _record(store, session_id, idx=1, text="unrelated chatter about lunch")
    arm = _Fixed(
        hits=[VectorHit(owner_id=session_id, part=0, idx=0, field="content", chunk=0)],
    )

    body = from_plain(
        await web.web_search_sessions(
            _request(store, vector_search=arm),
            q="release",
            identity=_VIEWER,
            mode="fused",
        ),
        dict[str, object],
    )

    assert (body["degraded"], body["semantic"]) == (False, True)
    hits = from_plain(body["hits"], list[dict[str, object]])
    top = from_plain(hits[0], dict[str, object])
    assert (top["session_id"], top["idx"], top["source"]) == (
        str(session_id),
        0,
        "both",
    )
    assert arm.asked == ["release"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_keyword_mode_skips_the_vector_arm_entirely(store: Store) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="advisory lock token here")
    arm = _Fixed(hits=[])

    body = from_plain(
        await web.web_search_sessions(
            _request(store, vector_search=arm),
            q="advisory lock",
            identity=_VIEWER,
            mode="keyword",
        ),
        dict[str, object],
    )

    assert (body["semantic"], body["degraded"]) == (False, False)
    assert arm.asked == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_bad_limit_and_empty_query_are_400(store: Store) -> None:
    """Route validation mirrors the sibling search route: limit + non-empty q."""
    request = _request(store)
    for bad in (0, 5000):
        with pytest.raises(HTTPException) as caught:
            await web.web_search_sessions(request, q="x", identity=_VIEWER, limit=bad)
        assert caught.value.status_code == 400
    with pytest.raises(HTTPException) as caught:
        await web.web_search_sessions(request, q="   ", identity=_VIEWER)
    assert caught.value.status_code == 400


def test_a_search_names_no_model_of_its_own() -> None:
    """The server's vector arm answers every search; a request cannot pick another."""
    app = FastAPI()
    app.include_router(web.router, prefix="/api/web")
    paths = from_plain(
        cast(dict[str, object], app.openapi())["paths"],
        dict[str, dict[str, dict[str, object]]],
    )
    parameters = from_plain(
        paths["/api/web/search_sessions"]["get"]["parameters"],
        list[dict[str, object]],
    )
    assert [each["name"] for each in parameters] == [
        "q",
        "near",
        "spawned",
        "mode",
        "limit",
        "whose",
        "hidden",
    ]


@pytest.mark.asyncio
async def test_a_search_near_a_session_hands_the_search_the_session_and_no_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _SearchSpy(searched=True)
    monkeypatch.setattr(web, "search_sessions", search)
    near = uuid4()

    body = await web.web_search_sessions(
        _request(make_store()[0]),
        identity=_VIEWER,
        near=near,
    )

    assert (search.queries, search.spawned) == ([near], [""])
    assert (body["semantic"], body["degraded"]) == (True, False)


@pytest.mark.asyncio
async def test_a_search_near_what_a_session_spawned_hands_the_search_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _SearchSpy(searched=True)
    monkeypatch.setattr(web, "search_sessions", search)
    near = uuid4()

    _ = await web.web_search_sessions(
        _request(make_store()[0]),
        identity=_VIEWER,
        near=near,
        spawned="a/b",
    )

    assert (search.queries, search.spawned) == ([near], ["a/b"])


@pytest.mark.asyncio
async def test_a_search_by_words_that_names_a_spawned_session_is_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _SearchSpy()
    monkeypatch.setattr(web, "search_sessions", search)

    with pytest.raises(HTTPException) as caught:
        await web.web_search_sessions(
            _request(make_store()[0]),
            q="words",
            identity=_VIEWER,
            spawned="a/b",
        )

    assert (caught.value.status_code, caught.value.detail) == (
        400,
        "spawned names what near spawned; give near",
    )
    assert search.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("q", "near", "mode", "detail"),
    [
        ("", None, "fused", "q must be non-empty"),
        ("words", UUID(int=7), "fused", "give q or near, not both"),
        ("", UUID(int=7), "keyword", "near is searched by vector, not by keyword"),
    ],
    ids=["neither", "both", "near-by-keyword"],
)
async def test_a_search_with_no_query_two_or_a_session_by_keyword_is_400(
    monkeypatch: pytest.MonkeyPatch,
    q: str,
    near: UUID | None,
    mode: SearchMode,
    detail: str,
) -> None:
    search = _SearchSpy()
    monkeypatch.setattr(web, "search_sessions", search)

    with pytest.raises(HTTPException) as caught:
        await web.web_search_sessions(
            _request(make_store()[0]),
            q=q,
            identity=_VIEWER,
            near=near,
            mode=mode,
        )

    assert (caught.value.status_code, caught.value.detail) == (400, detail)
    assert search.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "searched", "degraded"),
    [
        ("fused", False, True),
        ("fused", True, False),
        ("vector", False, True),
        ("keyword", False, False),
    ],
)
async def test_degraded_says_a_vector_arm_asked_for_did_not_run(
    monkeypatch: pytest.MonkeyPatch,
    mode: SearchMode,
    *,
    searched: bool,
    degraded: bool,
) -> None:
    search = _SearchSpy(searched=searched)
    monkeypatch.setattr(web, "search_sessions", search)

    body = await web.web_search_sessions(
        _request(make_store()[0]),
        q="x",
        identity=_VIEWER,
        mode=mode,
    )

    assert (body["semantic"], body["degraded"]) == (searched, degraded)
    assert search.modes == [mode]


@pytest.mark.asyncio
async def test_a_search_naming_no_mode_runs_both_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _SearchSpy()
    monkeypatch.setattr(web, "search_sessions", search)

    _ = await web.web_search_sessions(
        _request(make_store()[0]),
        q="x",
        identity=_VIEWER,
    )

    assert search.modes == ["fused"]


@pytest.mark.asyncio
async def test_feed_rejects_reversed_window_before_reading_store() -> None:
    request = Request({"type": "http", "app": FastAPI()})
    since = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(HTTPException) as caught:
        await web.web_feed(
            request,
            _VIEWER,
            since=since,
            until=since - timedelta(days=1),
        )
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_the_search_is_read_for_the_reader_its_caller_is_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _SearchSpy()
    monkeypatch.setattr(web, "search_sessions", search)
    request = _request(make_store()[0])

    held = (frozenset({"open", "private"}), frozenset({"open", "private", "staff"}))
    for audiences in held:
        _ = await web.web_search_sessions(
            request,
            q="x",
            identity=replace(_VIEWER, read_audiences=audiences),
            mode="keyword",
        )

    assert search.asked == [
        FeedScope(
            account=_VIEWER.email,
            reader=SessionReader(
                audiences=audiences,
                email=_VIEWER.email,
                oversight="staff" in audiences,
                hidden=False,
            ),
        )
        for audiences in held
    ]


@pytest.mark.asyncio
async def test_search_sessions_takes_whose_and_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search widens to every session the caller reads, or narrows to their campaigns."""
    search = _SearchSpy()
    monkeypatch.setattr(web, "search_sessions", search)
    request = _request(make_store()[0])
    caller = replace(_VIEWER, read_audiences=frozenset({"open", "private"}))

    cases: tuple[tuple[FeedWhose, bool], ...] = (
        ("everyone", True),
        ("campaigns", False),
    )
    for whose, hidden in cases:
        _ = await web.web_search_sessions(
            request,
            q="x",
            identity=caller,
            mode="keyword",
            whose=whose,
            hidden=hidden,
        )

    assert [
        (each.account, each.asker, each.reader.hidden) for each in search.asked
    ] == [
        ("", "", True),
        ("", _VIEWER.email, False),
    ]


@pytest.mark.asyncio
async def test_the_assistant_searches_every_session_it_reads_unless_it_names_whose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It holds none of the sessions it is asked about, where a person searches theirs."""
    search = _SearchSpy()
    monkeypatch.setattr(web, "search_sessions", search)
    state = _State(store=make_store()[0], vector_search=None)
    state.config = Config(assistant=Assistant(actor="scout", email=_VIEWER.email))
    request = cast(Request, _Request(_App(state)))

    for whose in (None, "mine"):
        _ = await web.web_search_sessions(
            request,
            q="x",
            identity=_VIEWER,
            mode="keyword",
            whose=whose,
        )

    assert [each.account for each in search.asked] == ["", _VIEWER.email]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_inquiry_search_answers_both_arms_by_inquiry_and_field(
    store: Store,
) -> None:
    async with store.engine.acquire() as conn:
        issue = from_plain(
            await conn.fetchval(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, "
                "description) VALUES (gen_random_uuid(), 'Issue', 7, 'active', $1, "
                "'lock bug', 'advisory lock held') RETURNING id",
                _VIEWER.email,
            ),
            UUID,
        )
    arm = _Fixed(hits=[VectorHit(owner_id=issue, part=0, idx=0, field="text", chunk=0)])

    body = await web.web_search_inquiries(
        _request(store, vector_search=arm),
        q="advisory",
        identity=_VIEWER,
        kind="Issue",
    )

    [hit] = from_plain(body.pop("hits"), list[dict[str, object]])
    assert body == {"semantic": True, "degraded": False}
    assert from_plain(hit.pop("score"), float) == pytest.approx(2 / 61)
    assert "<b>advisory</b>" in from_plain(hit.pop("snippet"), str)
    assert hit == {
        "inquiry_id": str(issue),
        "kind": "Issue",
        "seq": 7,
        "title": "lock bug",
        "field": "text",
        "source": "both",
    }
    assert arm.asked == ["advisory"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_malformed_inquiry_search_is_400(store: Store) -> None:
    request = _request(store)
    for q, limit in (("x", 0), ("x", 201), ("  ", 20)):
        with pytest.raises(HTTPException) as caught:
            await web.web_search_inquiries(request, q=q, identity=_VIEWER, limit=limit)
        assert caught.value.status_code == 400


@dataclass(frozen=True, slots=True, kw_only=True)
class _Fixed:
    """A vector arm that answers the same units for every query, recording each."""

    hits: list[VectorHit] | None
    asked: list[str | UUID] = field(default_factory=list[str | UUID])

    async def nearest(
        self,
        engine: DatabaseEngine,
        *,
        query: str | UUID,
        kept: Kept,
        filtered: bool,
        limit: int,
    ) -> list[VectorHit] | None:
        """Record ``query``; answer :attr:`hits`."""
        del engine, kept, filtered, limit
        self.asked.append(query)
        return self.hits


@dataclass(frozen=True, slots=True, kw_only=True)
class _SearchSpy:
    """Stand in for the session search, keeping each scope and mode it is asked for."""

    searched: bool = False
    asked: list[FeedScope] = field(default_factory=list[FeedScope])
    modes: list[str] = field(default_factory=list[str])
    queries: list[str | UUID] = field(default_factory=list[str | UUID])
    spawned: list[str] = field(default_factory=list[str])

    async def __call__(
        self,
        engine: object,
        *,
        query: str | UUID,
        spawned: str,
        scope: FeedScope,
        mode: str,
        **rest: object,
    ) -> tuple[list[SessionSearchHit], bool]:
        """Keep the query, ``scope`` and ``mode``; find nothing, the vector arm run or not."""
        del engine, rest
        self.queries.append(query)
        self.spawned.append(spawned)
        self.asked.append(scope)
        self.modes.append(mode)
        return [], self.searched


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
