"""The ``/api/web/search_sessions`` handler against real Postgres (pglite).

Boots a real store, seeds the session index through the production sweep with a
1024-dim stub, and calls the handler directly with a duck-typed request (the
session-scoped ``integ_engine`` lives on the session loop, so a starlette
``TestClient`` -- which spins its own loop -- cannot drive it; web_test.py calls
handlers the same way). Proves the semantic and FTS arms, RRF surfacing, and --
the load-bearing case -- graceful degradation to FTS-only when no session
embedder is configured (a keyword search must never load an 8 GB model).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

import pytest
import pytest_asyncio

from trackinizer.lib.custom_json import DictCodec, ListCodec
from trackinizer.server import web
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Config
from trackinizer.server.embedders import registry
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_embed import sweep_session_embeddings


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PostgresEngine


_VIEWER = AuthIdentity(
    user_id=UUID("55555555-5555-5555-5555-555555555555"),
    api_key_id=None,
    email="viewer@example.com",
    role="viewer",
)


class _State:
    """Mutable app state: ``_session_embedder`` assigns its cache onto it."""

    def __init__(self, *, store: Store, session_embedder: str) -> None:
        self.store = store
        self.engine = store.engine
        # A REAL Config, not a duck-type: ``_session_embedder`` narrows with
        # ``isinstance(config, Config)`` so a renamed field breaks type-checking
        # instead of silently disabling the semantic arm -- a fake here would
        # read as no-config and the route would degrade in every test.
        self.config = Config(session_embedder=session_embedder)


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


class _Request:
    def __init__(self, app: _App) -> None:
        self.app = app


def _request(store: Store, *, session_embedder: str) -> Request:
    return cast(
        Request,
        _Request(_App(_State(store=store, session_embedder=session_embedder))),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Bootstrapped store with the session tables emptied (search is global)."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with built.engine.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_embeddings, session_records, session_manifests, "
            "inquiries CASCADE",
        )
    yield built


async def _session(store: Store, *, title: str = "route search") -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', $2)",
            session_id,
            title,
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


async def _seed(store: Store) -> None:
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fts_arm_returns_hits_with_position_and_title(store: Store) -> None:
    """A keyword search returns hits carrying (session_id, part, idx) + title."""
    session_id = await _session(store, title="deploy log")
    await _record(store, session_id, idx=0, text="advisory lock acquired cleanly")
    await _seed(store)

    body = DictCodec.coerce(
        await web.web_search_sessions(
            _request(store, session_embedder=""),
            q="advisory lock",
            identity=_VIEWER,
            semantic=False,
        ),
    )
    hits = ListCodec.mappings(body["hits"])
    assert len(hits) == 1
    hit = DictCodec.coerce(hits[0])
    assert hit["session_id"] == str(session_id)
    assert (hit["part"], hit["idx"]) == (0, 0)
    assert hit["title"] == "deploy log"
    assert hit["source"] == "fts"
    assert body["semantic"] is False


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_semantic_requested_without_model_degrades_to_fts(store: Store) -> None:
    """semantic=true but no embedder configured -> FTS-only, degraded flagged.

    The load-bearing contract: a keyword search never 500s or loads a model
    when the session embedder is unset. It falls back to full text and says so.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="postgres deadlock trace")
    await _seed(store)

    body = DictCodec.coerce(
        await web.web_search_sessions(
            _request(store, session_embedder=""),
            q="deadlock",
            identity=_VIEWER,
            semantic=True,
        ),
    )
    assert body["degraded"] is True
    assert body["semantic"] is False
    hits = ListCodec.mappings(body["hits"])
    assert len(hits) == 1
    assert DictCodec.coerce(hits[0])["source"] == "fts"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_semantic_arm_runs_with_a_configured_embedder(store: Store) -> None:
    """With stub-1024 configured, the semantic arm runs and is not degraded."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="deploy the release to production")
    await _record(store, session_id, idx=1, text="unrelated chatter about lunch")
    await _seed(store)

    body = DictCodec.coerce(
        await web.web_search_sessions(
            _request(store, session_embedder="stub-1024"),
            q="deploy the release to production",
            identity=_VIEWER,
            semantic=True,
        ),
    )
    assert body["degraded"] is False
    assert body["semantic"] is True
    hits = ListCodec.mappings(body["hits"])
    assert hits
    top = DictCodec.coerce(hits[0])
    assert (top["session_id"], top["idx"]) == (str(session_id), 0)
    assert top["source"] in ("semantic", "both")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_semantic_false_skips_the_model_entirely(store: Store) -> None:
    """semantic=false never touches the embedder, even when one is configured."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="advisory lock token here")
    await _seed(store)

    body = DictCodec.coerce(
        await web.web_search_sessions(
            _request(store, session_embedder="stub-1024"),
            q="advisory lock",
            identity=_VIEWER,
            semantic=False,
        ),
    )
    assert body["semantic"] is False
    assert body["degraded"] is False  # Not degraded: the caller opted out.
    assert DictCodec.coerce(ListCodec.mappings(body["hits"])[0])["source"] == "fts"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_model_override_reuses_one_instance_across_requests(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``?model=`` queries on one app build the override embedder ONCE.

    A real model loads its weights on first use, so rebuilding per request would
    reload them every A/B query. The override is cached per name on app.state;
    this counts ``registry.build_session_embedder`` calls to prove reuse.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="deploy the release to production")
    await _seed(store)

    builds: list[tuple[str, int | None]] = []
    real_build = registry.build_session_embedder

    def counting_build(name: str, *, dim: int | None = None) -> object:
        builds.append((name, dim))
        return real_build(name, dim=dim)

    monkeypatch.setattr(registry, "build_session_embedder", counting_build)
    request = _request(store, session_embedder="")  # No default; override drives it.
    for _ in range(2):
        body = DictCodec.coerce(
            await web.web_search_sessions(
                request,
                q="deploy the release to production",
                identity=_VIEWER,
                semantic=True,
                model="stub-1024",
            ),
        )
        assert body["semantic"] is True
        assert body["degraded"] is False
    assert builds == [("stub-1024", None)]  # Built once, reused on the second.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_model_override_caches_two_dims_as_distinct_entries(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?dim=`` distinguishes cache entries: two dims of one model build twice.

    A Matryoshka model at 512 and at 256 is two different stored identities and
    two different truncated vectors, so the per-request override cache must key on
    ``(name, dim)`` -- not collapse them to one entry keyed by name alone.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="deploy the release to production")
    await _seed(store)

    builds: list[tuple[str, int | None]] = []
    real_build = registry.build_session_embedder

    def counting_build(name: str, *, dim: int | None = None) -> object:
        builds.append((name, dim))
        return real_build(name, dim=dim)

    monkeypatch.setattr(registry, "build_session_embedder", counting_build)
    request = _request(store, session_embedder="")
    for override_dim in (512, 256, 512):  # 512 repeats -> its second call is cached.
        _ = DictCodec.coerce(
            await web.web_search_sessions(
                request,
                q="deploy the release to production",
                identity=_VIEWER,
                semantic=True,
                model="stub",
                dim=override_dim,
            ),
        )
    # 512 built once (reused on repeat), 256 built once -> two distinct entries.
    assert builds == [("stub", 512), ("stub", 256)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_model_override_unknown_is_400(store: Store) -> None:
    """An unknown ``?model=`` is a client error, with a did-you-mean hint."""
    request = _request(store, session_embedder="")
    with pytest.raises(HTTPException) as caught:
        await web.web_search_sessions(
            request,
            q="anything",
            identity=_VIEWER,
            semantic=True,
            model="qwen3-embeddings-4b",  # Typo'd slug -- no such model.
        )
    assert caught.value.status_code == 400
    assert "did you mean" in str(caught.value.detail)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_model_override_unsupported_dim_is_400(store: Store) -> None:
    """A ``?dim=`` outside the model's range is a client error naming the range."""
    request = _request(store, session_embedder="")
    with pytest.raises(HTTPException) as caught:
        await web.web_search_sessions(
            request,
            q="anything",
            identity=_VIEWER,
            semantic=True,
            model="qwen3-embedding-4b",
            dim=99_999,  # Above the 2560 Matryoshka ceiling.
        )
    assert caught.value.status_code == 400
    assert "2560" in str(caught.value.detail)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_bad_limit_and_empty_query_are_400(store: Store) -> None:
    """Route validation mirrors the sibling search route: limit + non-empty q."""
    request = _request(store, session_embedder="")
    for bad in (0, 5000):
        with pytest.raises(HTTPException) as caught:
            await web.web_search_sessions(request, q="x", identity=_VIEWER, limit=bad)
        assert caught.value.status_code == 400
    with pytest.raises(HTTPException) as caught:
        await web.web_search_sessions(request, q="   ", identity=_VIEWER)
    assert caught.value.status_code == 400


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
