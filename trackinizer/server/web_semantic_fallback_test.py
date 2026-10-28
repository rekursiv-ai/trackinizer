"""``/api/web/search``'s semantic fallback.

The fallback fires only where the literal search found nothing, so the
properties worth pinning are the ones that keep it from making the search box
worse: a literal hit must never reach it, an unconfigured or failing embedder
must degrade to the empty result the caller already got, and a query unrelated
to anything in the graph must stay empty rather than return the nearest rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import uuid

from fastapi import Request

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server import web
from trackinizer.server.auth import AuthIdentity
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Belief, Inquiry


_IDENTITY = AuthIdentity(
    user_id=uuid.uuid4(),
    email="tester@example.com",
    role="viewer",
    api_key_id=None,
)


@dataclass
class _Request:
    app: object


@dataclass
class _AppState:
    store: object
    engine: object


@dataclass
class _App:
    state: _AppState


@dataclass
class _SemanticStore:
    """A store whose ``find_similar`` is scripted by the test."""

    engine: FakeEngine
    matches: list[tuple[Inquiry, float]] = field(default_factory=list)
    raises: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def find_similar(
        self,
        text: str,
        *,
        kind: Inquiry.InquiryKind | None = None,
        limit: int = 50,
        model: str | None = None,
        conn: object = None,
    ) -> list[tuple[Inquiry, float]]:
        del kind, limit, model, conn
        self.calls.append(text)
        if self.raises is not None:
            raise self.raises
        return self.matches


def _request(store: object, engine: object) -> _Request:
    return _Request(app=_App(state=_AppState(store=store, engine=engine)))


def _belief(title: str) -> Belief:
    now = datetime(2026, 5, 18, tzinfo=UTC)
    return Belief(
        id=uuid.uuid4(),
        seq=1,
        account="alice@example.com",
        title=title,
        created=now,
        modified=now,
    )


def _row_for(inquiry: Inquiry) -> dict[str, object]:
    """Build an ``inquiries`` record matching ``inquiry``, for the re-read step."""
    now = datetime(2026, 5, 18, tzinfo=UTC)
    return {
        "id": inquiry.id,
        "kind": "Belief",
        "seq": inquiry.seq,
        "owner": None,
        "account": inquiry.account,
        "status": "active",
        "title": inquiry.title,
        "description": "",
        "labels": None,
        "subscribers": None,
        "marginal_cost_agent_usd": 0.0,
        "marginal_cost_resource_usd": 0.0,
        "created": now,
        "modified": now,
    }


@pytest.mark.asyncio
async def test_a_literal_hit_never_reaches_the_fallback() -> None:
    """Exact matching stays primary: a hit must not trigger an embed call."""
    engine = FakeEngine()
    store = _SemanticStore(engine=engine, matches=[(_belief("nearby"), 0.1)])
    engine.conn.fetch = AsyncMock(
        return_value=[_row_for(_belief("literal match"))],
    )

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="literal",
        identity=_IDENTITY,
    )

    assert rows[0]["title"] == "literal match"
    assert store.calls == [], "the fallback ran despite a literal hit"


@pytest.mark.asyncio
async def test_an_unconfigured_embedder_degrades_to_empty() -> None:
    """The default stub raises ValueError; the box must not 500."""
    engine = FakeEngine()
    store = _SemanticStore(
        engine=engine,
        raises=ValueError("embedder 'stub' does not support semantic search"),
    )
    engine.conn.fetch = AsyncMock(return_value=[])

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="anything at all",
        identity=_IDENTITY,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_a_failing_endpoint_degrades_to_empty() -> None:
    """A dead embedding endpoint is not the caller's problem to see as a 500."""
    engine = FakeEngine()
    store = _SemanticStore(
        engine=engine,
        raises=ConflictError("embedding request failed: connection refused"),
    )
    engine.conn.fetch = AsyncMock(return_value=[])

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="anything at all",
        identity=_IDENTITY,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_a_related_match_is_returned_when_literal_search_is_empty() -> None:
    """The point of the feature: results where there were none."""
    engine = FakeEngine()
    near = _belief("x10 overfits past step 12.5k")
    store = _SemanticStore(engine=engine, matches=[(near, 0.42)])
    # First fetch: the literal search (empty). Second: the re-read by id.
    engine.conn.fetch = AsyncMock(side_effect=[[], [_row_for(near)]])

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="what happened when we trained too long",
        identity=_IDENTITY,
    )

    assert [r["title"] for r in rows] == ["x10 overfits past step 12.5k"]
    assert store.calls == ["what happened when we trained too long"]


@pytest.mark.asyncio
async def test_an_unrelated_query_stays_empty() -> None:
    """Nearest-neighbour always returns something; the cutoff must drop it.

    Without the distance ceiling, an off-topic question would come back with
    whatever the graph happens to hold -- worse than the empty result the
    caller gets today.
    """
    engine = FakeEngine()
    far = _belief("x10 overfits past step 12.5k")
    store = _SemanticStore(engine=engine, matches=[(far, 0.97)])
    engine.conn.fetch = AsyncMock(return_value=[])

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="how do I bake sourdough bread",
        identity=_IDENTITY,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_the_cutoff_keeps_near_rows_and_drops_far_ones() -> None:
    """A mixed result set is filtered per row, not all-or-nothing."""
    engine = FakeEngine()
    near = _belief("near enough")
    far = _belief("much too far")
    store = _SemanticStore(
        engine=engine,
        matches=[(near, 0.50), (far, 1.02)],
    )
    engine.conn.fetch = AsyncMock(side_effect=[[], [_row_for(near)]])

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="a question",
        identity=_IDENTITY,
    )

    assert [r["title"] for r in rows] == ["near enough"]


@pytest.mark.asyncio
async def test_similarity_order_survives_the_re_read() -> None:
    """``id = ANY(...)`` does not preserve argument order; ranking must."""
    engine = FakeEngine()
    first = _belief("closest")
    second = _belief("further")
    store = _SemanticStore(
        engine=engine,
        matches=[(first, 0.20), (second, 0.60)],
    )
    # Postgres hands the rows back in the opposite order on purpose.
    engine.conn.fetch = AsyncMock(
        side_effect=[[], [_row_for(second), _row_for(first)]],
    )

    rows = await web.web_search(
        cast(Request, _request(store, engine)),
        q="a question",
        identity=_IDENTITY,
    )

    assert [r["title"] for r in rows] == ["closest", "further"]
