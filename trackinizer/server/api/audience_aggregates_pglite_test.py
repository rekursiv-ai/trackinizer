"""The routes that return aggregates read for the audiences of the caller who asked.

The store tests prove each reader leaves out a session its viewer may not read. These
prove each route names the right viewer: an open caller and a staff caller ask the real
routes about the same two sessions, and only the staff caller sees the one that holds
the canary. A route that forgot to pass the caller's audiences answers a staff caller as
it answers an open one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from fastapi import FastAPI
from treekle import from_plain, json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server import web
from trackinizer.server.api import export_routes, query
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.config import Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.audience_aggregates_pglite_test import (
    CANARY,
    Pair,
    index,
    seed,
)
from trackinizer.server.store.core import Store
from trackinizer.wire.wire_export import EXPORT_API_PATH


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from trackinizer.lib.postgres import PGliteEngine


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_OPEN: Final = frozenset({"open"})
_BOTH: Final = frozenset({"open", "staff"})
_STAFF_TEXT: Final = f"{CANARY} the staff transcript"
_RECORD_FILTER: Final = '{"field":"user_message","op":"re","value":"transcript"}'


@dataclass(frozen=True, slots=True, kw_only=True)
class _Served:
    """The routes under test, the sessions behind them, and who asks."""

    http: httpx2.AsyncClient
    app: FastAPI
    pair: Pair

    def as_reader(self, audiences: frozenset[str]) -> None:
        """Answer every following request as a caller that reads ``audiences``."""
        base = make_test_identity(api_key_id=None)

        async def identity() -> AuthIdentity:
            return replace(base, read_audiences=audiences)

        self.app.dependency_overrides[current_user] = identity

    async def get(self, path: str, params: list[tuple[str, str]]) -> httpx2.Response:
        """Read ``path`` and require that it answered."""
        response = await self.http.get(path, params=params)
        assert response.status_code == 200, response.text
        return response


@pytest_asyncio.fixture(loop_scope="session")
async def served(pglite_engine: PGliteEngine) -> AsyncIterator[_Served]:
    """Serve the feed, search, record-filter and export routes over two sessions."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    # The histogram counts a week back at most, so the sessions write within it.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    pair = await seed(store, origin=today - timedelta(days=3))
    await index(store)
    app = FastAPI()
    app.state.store = store
    app.state.engine = store.engine
    app.state.config = Config(session_embedder="stub-1024")
    web.attach(app)
    app.include_router(query.router)
    app.include_router(export_routes.router)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as http:
        yield _Served(http=http, app=app, pair=pair)


def _whole(response: httpx2.Response) -> str:
    return response.text


def _transcript(response: httpx2.Response) -> str:
    """Return the lines of an export that belong to the session tables."""
    session_tables = {"session_manifests", "session_records", "session_slash_commands"}
    return "\n".join(
        line
        for line in response.text.splitlines()
        if json_loads(line, dict[str, object]).get("table") in session_tables
    )


type _Route = tuple[str, list[tuple[str, str]], Callable[[httpx2.Response], str]]

_ROUTES: Final[dict[str, _Route]] = {
    "feed": ("/api/web/feed", [], _whole),
    "facets": ("/api/web/feed/facets", [], _whole),
    "full text": (
        "/api/web/search_sessions",
        [("q", "transcript"), ("semantic", "false")],
        _whole,
    ),
    "semantic": (
        "/api/web/search_sessions",
        [("q", _STAFF_TEXT), ("semantic", "true")],
        _whole,
    ),
    "record filter": (
        "/api/inquiries",
        [("kind", "AgentSession"), ("filter", _RECORD_FILTER)],
        _whole,
    ),
    "export": (EXPORT_API_PATH, [], _transcript),
}


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_route_answers_for_the_audiences_of_its_caller(
    served: _Served,
    route: str,
) -> None:
    path, params, view = _ROUTES[route]
    pair = served.pair

    served.as_reader(_OPEN)
    opened = view(await served.get(path, params))
    served.as_reader(_BOTH)
    staffed = view(await served.get(path, params))

    assert CANARY not in opened
    assert str(pair.staff_id) not in opened
    assert str(pair.open_id) in opened
    assert CANARY in staffed
    assert str(pair.staff_id) in staffed
    assert str(pair.open_id) in staffed


async def test_the_export_header_names_the_audiences_of_its_caller(
    served: _Served,
) -> None:
    """The first line of the file says which audiences' transcripts it holds."""

    async def header() -> dict[str, object]:
        first = (await served.get(EXPORT_API_PATH, [])).text.splitlines()[0]
        return json_loads(first, dict[str, object])

    served.as_reader(_OPEN)
    opened = await header()
    served.as_reader(_BOTH)
    staffed = await header()

    assert opened["audiences"] == ["open"]
    assert staffed["audiences"] == ["open", "staff"]


async def test_the_histogram_counts_for_the_audiences_of_its_caller(
    served: _Served,
) -> None:
    """Its grid starts at the first record the caller may count, and counts no more."""
    pair = served.pair

    async def read() -> tuple[datetime, int]:
        body = from_plain(
            (await served.get("/api/web/feed/histogram", [])).json(),
            dict[str, object],
        )
        counts = from_plain(body["counts"], list[dict[str, object]])
        return (
            datetime.fromisoformat(from_plain(body["start"], str)),
            sum(from_plain(bucket["count"], int) for bucket in counts),
        )

    served.as_reader(_OPEN)
    opened = await read()
    served.as_reader(_BOTH)
    staffed = await read()

    assert opened == (pair.open_at, 1)
    assert staffed == (pair.staff_at, 3)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
