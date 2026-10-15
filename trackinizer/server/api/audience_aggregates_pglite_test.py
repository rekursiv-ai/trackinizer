"""The routes that return aggregates read for the caller who asked.

The store tests prove each reader leaves out a session its viewer may not read. These
prove each route names the right viewer: an open caller and a staff caller ask the real
routes about the same two sessions, and only the staff caller sees the one that holds
the canary. A route that forgot to pass the caller's audiences answers a staff caller as
it answers an open one. A science chat sits beside them: its starter reads it through
every route, and a stranger who reads every audience, the assistant included, through
none. None of them reads for an asker: the configured assistant that names a staff
user is answered as the caller it is. The feed's ``whose`` keeps the caller's own
sessions, or those of the campaigns they launched, of what they read.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from fastapi import FastAPI
from treekle import from_plain, json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server import web
from trackinizer.server.api import export_routes, query
from trackinizer.server.api.conftest import TEST_USER_EMAIL, make_test_identity
from trackinizer.server.auth import AuthIdentity, current_user
from trackinizer.server.config import Assistant, ChatOrgs, Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.audience_aggregates_pglite_test import (
    ASSISTANT_EMAIL,
    CANARY,
    CHAT_CANARY,
    STARTER_EMAIL,
    Pair,
    index,
    seed,
    seed_chat,
)
from trackinizer.server.store.core import Store
from trackinizer.wire.audience import AUDIENCES
from trackinizer.wire.wire_export import EXPORT_API_PATH
from trackinizer.wire.wire_sessions import campaign_asker_label


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from trackinizer.lib.postgres import PGliteEngine


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_OPEN: Final = frozenset({"open", "private"})
_BOTH: Final = AUDIENCES
_STRANGER: Final = "sid@else.example"
_STAFF_USER: Final = "lee@staff.example"
_STAFF_TEXT: Final = f"{CANARY} the staff transcript"
_RECORD_FILTER: Final = '{"field":"user_message","op":"re","value":"transcript"}'


@dataclass(frozen=True, slots=True, kw_only=True)
class _Served:
    """The routes under test, the sessions behind them, and who asks."""

    http: httpx2.AsyncClient
    app: FastAPI
    store: Store
    pair: Pair
    chat: UUID
    chat_at: datetime

    def as_reader(
        self,
        audiences: frozenset[str],
        *,
        email: str = TEST_USER_EMAIL,
    ) -> None:
        """Answer every following request as ``email``, reading ``audiences``.

        Google names each a member of their email domain's organisation.
        """
        caller = replace(
            make_test_identity(api_key_id=None, email=email),
            read_audiences=audiences,
            hosted_domain=email.rpartition("@")[2],
        )

        async def identity() -> AuthIdentity:
            return caller

        self.app.dependency_overrides[current_user] = identity

    def serve_chats_as(self, orgs: ChatOrgs) -> None:
        """Group chats by ``orgs`` from the next request on."""
        self.app.state.config = _config(orgs)

    async def get(self, path: str, params: list[tuple[str, str]]) -> httpx2.Response:
        """Read ``path`` and require that it answered."""
        response = await self.http.get(path, params=params)
        assert response.status_code == 200, response.text
        return response


@pytest_asyncio.fixture(loop_scope="session")
async def served(pglite_engine: PGliteEngine) -> AsyncIterator[_Served]:
    """Serve the feed, search, record-filter and export routes over three sessions."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    # The histogram counts a week back at most, so the sessions write within it. The
    # chat writes first, so a grid that counted it would start at its record.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    pair = await seed(store, origin=today - timedelta(days=3))
    chat_at = pair.origin
    chat = await seed_chat(store, at=chat_at)
    await index(store)
    app = FastAPI()
    app.state.store = store
    app.state.engine = store.engine
    app.state.config = _config("domain")
    web.attach(app)
    app.include_router(query.router)
    app.include_router(export_routes.router)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as http:
        yield _Served(
            http=http,
            app=app,
            store=store,
            pair=pair,
            chat=chat,
            chat_at=chat_at,
        )


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

# Session search reads a person's own sessions unless asked for every one they read.
_ROUTES: Final[dict[str, _Route]] = {
    "feed": ("/api/web/feed", [], _whole),
    "facets": ("/api/web/feed/facets", [], _whole),
    "full text": (
        "/api/web/search_sessions",
        [("q", "transcript"), ("semantic", "false"), ("whose", "everyone")],
        _whole,
    ),
    "semantic": (
        "/api/web/search_sessions",
        [("q", _STAFF_TEXT), ("semantic", "true"), ("whose", "everyone")],
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


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_route_answers_a_stranger_without_another_persons_chat(
    served: _Served,
    route: str,
) -> None:
    path, params, view = _ROUTES[route]
    pair = served.pair

    served.as_reader(_BOTH, email=_STRANGER)
    stranger = view(await served.get(path, params))
    served.as_reader(_OPEN, email=STARTER_EMAIL)
    starter = view(await served.get(path, params))

    assert (str(pair.open_id) in stranger, str(pair.staff_id) in stranger) == (
        True,
        True,
    )
    assert CANARY in stranger
    assert (str(served.chat) in stranger, CHAT_CANARY in stranger) == (False, False)
    assert str(served.chat) in starter


async def test_the_export_header_names_the_audiences_of_its_caller(
    served: _Served,
) -> None:
    """The first line of the file says which audiences' transcripts it holds."""
    served.as_reader(_OPEN)
    opened = await _export_header(served)
    served.as_reader(_BOTH)
    staffed = await _export_header(served)

    assert opened["audiences"] == ["open", "private"]
    assert staffed["audiences"] == ["open", "private", "staff"]


async def test_the_histogram_counts_for_the_audiences_of_its_caller(
    served: _Served,
) -> None:
    """Its grid starts at the first record the caller may count, and counts no more."""
    pair = served.pair

    served.as_reader(_OPEN)
    opened = await _histogram(served)
    served.as_reader(_BOTH)
    staffed = await _histogram(served)

    assert opened == (pair.open_at, 1)
    assert staffed == (pair.staff_at, 3)


async def test_the_histogram_counts_no_record_of_another_persons_chat(
    served: _Served,
) -> None:
    """The chat wrote first and answered inside the span, so a leak shows in either."""
    served.as_reader(_BOTH, email=_STRANGER)
    stranger = await _histogram(served)
    served.as_reader(_OPEN, email=STARTER_EMAIL)
    starter = await _histogram(served)

    # The starter is of another organisation than the open session's account.
    assert stranger == (served.pair.staff_at, 3)
    assert starter == (served.chat_at, 2)


async def test_the_assistant_finds_no_chat_by_searching(served: _Served) -> None:
    """It searches on a user's behalf, so a chat it could find would be any user's."""
    params = [("q", "transcript"), ("semantic", "false")]
    served.as_reader(_BOTH, email=ASSISTANT_EMAIL)

    by_domain = (await served.get("/api/web/search_sessions", params)).text
    served.serve_chats_as("single")
    by_one_organisation = (await served.get("/api/web/search_sessions", params)).text

    assert str(served.pair.staff_id) in by_domain
    assert str(served.chat) not in by_domain
    assert str(served.chat) in by_one_organisation


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_route_reads_nothing_for_an_asker_the_assistant_names(
    served: _Served,
    route: str,
) -> None:
    """Only a session read by id is answered for an asker; these answer the caller."""
    path, params, view = _ROUTES[route]
    pair = served.pair
    served.serve_chats_as("single")
    served.as_reader(_OPEN, email=ASSISTANT_EMAIL)

    shown = view(await served.get(path, [*params, ("asker", _STAFF_USER)]))

    assert CANARY not in shown
    assert str(pair.staff_id) not in shown
    assert str(pair.open_id) in shown


async def test_the_histogram_counts_nothing_for_an_asker_the_assistant_names(
    served: _Served,
) -> None:
    """The assistant counts the open session and the chat as itself, and no staff."""
    served.serve_chats_as("single")
    served.as_reader(_OPEN, email=ASSISTANT_EMAIL)

    counted = await _histogram(served, [("asker", _STAFF_USER)])

    assert counted == (served.chat_at, 3)


async def test_whose_mine_reads_the_callers_own_sessions(served: _Served) -> None:
    """Of what the caller reads, its own account's sessions: the chat is Ann's."""
    pair = served.pair
    served.serve_chats_as("single")
    served.as_reader(_BOTH, email="Tester@Example.com")

    mine = (await served.get("/api/web/feed", [("whose", "mine")])).text
    everyone = (await served.get("/api/web/feed", [])).text

    assert (str(pair.open_id) in mine, str(pair.staff_id) in mine) == (True, True)
    assert (str(served.chat) in mine, str(served.chat) in everyone) == (False, True)


async def test_whose_campaigns_reads_the_rooms_of_briefs_labelled_with_the_caller(
    served: _Served,
) -> None:
    """A brief labelled with the caller names a slug: its room's sessions are kept."""
    pair = served.pair
    async with served.store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, labels) "
            "VALUES (gen_random_uuid(), 'Issue', nextval('seq_issue'), 'active', "
            "'campaign@service.invalid', 'brief', $1)",
            ["lab", campaign_asker_label("lab", "ann@lab.example"), "launch:lab"],
        )
    served.as_reader(_BOTH, email="Ann@Lab.example")

    mine = (await served.get("/api/web/feed", [("whose", "campaigns")])).text
    served.as_reader(_BOTH, email=_STRANGER)
    theirs = (await served.get("/api/web/feed", [("whose", "campaigns")])).text

    # The open session is in the room the slug names; the staff one is not.
    assert (str(pair.open_id) in mine, str(pair.staff_id) in mine) == (True, False)
    assert str(pair.open_id) not in theirs


async def test_whose_everyone_reads_as_no_whose_and_another_is_refused(
    served: _Served,
) -> None:
    served.as_reader(_BOTH)

    named = (await served.get("/api/web/feed", [("whose", "everyone")])).json()
    default = (await served.get("/api/web/feed", [])).json()
    refused = await served.http.get("/api/web/feed", params=[("whose", "theirs")])

    assert named == default
    assert refused.status_code == 422


async def test_facets_carry_account_audience_and_hideable(served: _Served) -> None:
    """Its owner may hide a session it reads, not a staff one, and no one a chat."""
    pair = served.pair
    served.serve_chats_as("single")
    served.as_reader(_BOTH, email="Tester@Example.com")

    body = from_plain(
        (await served.get("/api/web/feed/facets", [])).json(),
        dict[str, object],
    )
    actors = {
        from_plain(each["session_id"], str): (
            each["account"],
            each["audience"],
            each["hideable"],
        )
        for each in from_plain(body["actors"], list[dict[str, object]])
    }

    assert actors == {
        str(pair.open_id): ("tester@example.com", "open", True),
        str(pair.staff_id): ("tester@example.com", "staff", False),
        str(served.chat): (STARTER_EMAIL, "open", False),
    }


async def test_whose_campaigns_reads_a_lead_labelled_with_the_slug_in_no_room_of_it(
    served: _Served,
) -> None:
    """A lead carries its campaign's slug as a label, whatever rooms it joined."""
    lead = uuid4()
    async with served.store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, labels) "
            "VALUES (gen_random_uuid(), 'Issue', nextval('seq_issue'), 'active', "
            "'campaign@service.invalid', 'brief', $1)",
            ["tiles", campaign_asker_label("tiles", "ann@lab.example")],
        )
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "labels, agentsession_cli, agentsession_audience) VALUES ($1, "
            "'AgentSession', nextval('seq_agentsession'), 'active', "
            "'campaign@service.invalid', 'lead', 'lead', $2, 'claude', 'open')",
            lead,
            ["tiles"],
        )
        await conn.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, 0, 'UserMessage', '{\"content\": \"go\"}'::json, 'go')",
            lead,
        )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 'lead.jsonl', '{}'::json, gen_random_uuid(), 'claude', 1)",
            lead,
        )
    served.as_reader(_BOTH, email="Ann@Lab.example")

    mine = (await served.get("/api/web/feed", [("whose", "campaigns")])).text

    assert str(lead) in mine


async def test_facets_offer_no_hide_of_a_chat_even_to_its_starter(
    served: _Served,
) -> None:
    """A chat is hidden from Chat, which says who reads it, and not from the console."""
    served.serve_chats_as("single")
    served.as_reader(_OPEN, email=STARTER_EMAIL)

    body = from_plain(
        (await served.get("/api/web/feed/facets", [])).json(),
        dict[str, object],
    )
    hideable = {
        from_plain(each["session_id"], str): each["hideable"]
        for each in from_plain(body["actors"], list[dict[str, object]])
    }

    assert hideable[str(served.chat)] is False


def _config(orgs: ChatOrgs) -> Config:
    """Return the server's configuration, the assistant Scout's, chats as ``orgs``."""
    return Config(
        session_embedder="stub-1024",
        assistant=Assistant(actor="scout", email=ASSISTANT_EMAIL),
        chat_orgs=orgs,
    )


async def _export_header(served: _Served) -> dict[str, object]:
    """Read the export's first line as the installed caller."""
    first = (await served.get(EXPORT_API_PATH, [])).text.splitlines()[0]
    return json_loads(first, dict[str, object])


async def _histogram(
    served: _Served,
    params: list[tuple[str, str]] | None = None,
) -> tuple[datetime, int]:
    """Read the histogram's start and total count as the installed caller."""
    body = from_plain(
        (await served.get("/api/web/feed/histogram", params or [])).json(),
        dict[str, object],
    )
    counts = from_plain(body["counts"], list[dict[str, object]])
    return (
        datetime.fromisoformat(from_plain(body["start"], str)),
        sum(from_plain(bucket["count"], int) for bucket in counts),
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
