"""A chat's answer in writing: the assistant puts it, the chat's readers read it.

The first tests run each route over a faked store, which holds three sessions: the
chat the assistant's key opened, a session that is no chat, and a chat that ended;
only the people named read the chat. The PGlite tests run the whole app over a real
database: the assistant opened a science chat for the browser user, its starter, and
the second user reads it on a ``single`` server and not on an ``individual`` one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import asyncio
import math

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server.api import chat_partial_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_EMAIL,
    KB_KEY_ID,
    KB_USER_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_other_agent,
    browser,
    seed_accounts,
    seed_science_chat,
    seed_session,
)
from trackinizer.server.api.conftest import (
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.session_access import hidden_session
from trackinizer.server.chat_partials import ChatPartials
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.wire_science_chat import (
    ChatPartial,
    ChatPartialState,
    chat_session_id,
)


if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.api.session_access import SessionAccess
    from trackinizer.server.auth import AuthIdentity


_CANARY: Final = "partial7e2a"
_CHAT: Final = UUID(int=1)
_RUN: Final = UUID(int=2)
_ENDED: Final = UUID(int=3)
_READER: Final = "reader@example.com"
_HELD: Final = {"seq": 1, "partial": {"turn": "t1", "text": "One", "done": False}}
_NONE: Final = {"seq": 0, "partial": None}


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> ChatPartials:
    """Serve with the assistant configured, on one organisation, holding no partial.

    A chat's first partial is put under ``seq`` 1, so a test reads back what it put.
    """
    partials = ChatPartials(first_seq=lambda: 1)
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    patch_state(monkeypatch, "partials", partials)
    return partials


@pytest.fixture
def readers() -> set[str]:
    """Return who reads the faked chat; a test unshares it by taking one out."""
    return {_READER}


@pytest.fixture
def faked(
    route_client: tuple[TestClient, Store, FakeEngine],
    readers: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Serve the routes over a store that holds the three sessions, read as named."""
    client, store, _engine = route_client
    chat = AgentSession(
        cli="sagent",
        cli_session_id=chat_session_id(uuid4()),
        audience="open",
        opened_by_api_key_id=KB_KEY_ID,
    )
    sessions = {
        _CHAT: chat,
        _RUN: replace(chat, cli_session_id="run"),
        _ENDED: replace(chat, status="complete", ended=datetime.now(UTC)),
    }

    async def get_inquiry(inquiry_id: UUID) -> AgentSession | None:
        return sessions.get(inquiry_id)

    async def readers_only(
        request: Request,
        identity: AuthIdentity,
        *,
        session: AgentSession,
        mode: SessionAccess,
    ) -> None:
        del request
        if mode != "read" or identity.email not in readers:
            raise hidden_session(session.audience)

    monkeypatch.setattr(store, "get_inquiry", get_inquiry)
    monkeypatch.setattr(chat_partial_routes, "require_session_access", readers_only)
    return client


def _as_assistant(key: UUID | None = KB_KEY_ID) -> None:
    """Act as the assistant's account, through ``key``."""
    install_identity(
        make_test_identity(user_id=KB_USER_ID, api_key_id=key, email=KB_EMAIL),
    )


def _as_reader() -> None:
    """Act as a person who reads the chat, from a browser."""
    install_identity(make_test_identity(api_key_id=None, email=_READER))


def _put(client: TestClient, session: UUID, *, turn: str = "t1") -> tuple[int, object]:
    """Put a partial; return the answer's status and body."""
    response = client.put(
        f"/api/sessions/{session}/partial",
        json={"turn": turn, "text": "One"},
    )
    return response.status_code, response.json() if response.content else None


def _read(client: TestClient, session: UUID = _CHAT) -> tuple[int, object]:
    """Read a partial now; return the answer's status and body."""
    response = client.get(f"/api/sessions/{session}/partial")
    return response.status_code, response.json()


def _delete(client: TestClient, session: UUID = _CHAT, *, turn: str = "t1") -> int:
    """Delete a turn's partial; return the answer's status."""
    return client.delete(f"/api/sessions/{session}/partial/{turn}").status_code


def test_the_key_that_opened_a_chat_puts_and_a_reader_reads_it(
    faked: TestClient,
) -> None:
    _as_assistant()
    put = _put(faked, _CHAT)
    _as_reader()

    assert put == (204, None)
    assert _read(faked) == (200, _HELD)


def test_only_the_assistant_through_the_opening_key_writes_a_live_chats_partial(
    faked: TestClient,
) -> None:
    install_identity(make_test_identity(api_key_id=KB_KEY_ID))
    stranger = _put(faked, _CHAT)
    _as_assistant(key=uuid4())
    other_key = _put(faked, _CHAT)
    _as_assistant(key=None)
    browser_put = _put(faked, _CHAT)
    _as_assistant()
    refused = {
        "unknown": _put(faked, uuid4())[0],
        "no chat": _put(faked, _RUN),
        "ended": _put(faked, _ENDED),
    }
    _as_reader()

    assert stranger == (403, {"detail": {"code": "not_the_assistant"}})
    not_the_opener = (
        403,
        {"detail": "A science chat is written by the key that opened it"},
    )
    assert (other_key, browser_put) == (not_the_opener, not_the_opener)
    assert refused == {
        "unknown": 404,
        "no chat": (404, {"detail": {"code": "not_a_chat"}}),
        "ended": (409, {"detail": {"code": "session_ended"}}),
    }
    assert _read(faked) == (200, _NONE)


def test_a_delete_drops_only_its_own_turns_partial_and_needs_the_opening_key(
    faked: TestClient,
) -> None:
    _as_assistant()
    _ = _put(faked, _CHAT, turn="t1")
    _as_assistant(key=uuid4())
    stranger = _delete(faked)
    _as_assistant()
    late = _delete(faked, turn="t0")
    _as_reader()
    kept = _read(faked)
    _as_assistant()
    dropped = _delete(faked)
    ended = _delete(faked, _ENDED)
    _as_reader()

    assert (stranger, late, kept) == (403, 204, (200, _HELD))
    assert (dropped, ended) == (204, 204)
    assert _read(faked) == (200, _NONE)


def test_a_read_is_refused_to_whoever_does_not_read_the_chat(
    faked: TestClient,
) -> None:
    _as_assistant()
    _ = _put(faked, _CHAT)
    install_identity(make_test_identity(api_key_id=None))
    refused = _read(faked)
    _as_reader()
    unknown = _read(faked, uuid4())[0]

    assert refused == (
        403,
        {"detail": {"code": "session_audience", "audience": "open"}},
    )
    assert unknown == 404


def test_every_route_is_refused_when_streaming_is_off(
    faked: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_state(monkeypatch, "config", replace(ASSISTANT_CONFIG, chat_streaming=False))
    install_identity(make_test_identity(api_key_id=None, email="anyone@example.com"))
    off = [_put(faked, _CHAT), _read(faked)]
    deleted = faked.delete(f"/api/sessions/{_CHAT}/partial/t1")

    assert off == [(404, {"detail": {"code": "chat_partials_off"}})] * 2
    assert (deleted.status_code, deleted.json()) == off[0]


def test_a_read_holds_for_the_wait_asked_cut_to_25_s_and_never_for_nan(
    faked: TestClient,
    served: ChatPartials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[int, float]] = []

    async def wait(
        session_id: UUID,
        *,
        after: int,
        wait_sec: float,
    ) -> ChatPartialState:
        del session_id
        asked.append((after, wait_sec))
        return ChatPartialState(seq=0, partial=None)

    monkeypatch.setattr(served, "wait", wait)
    _as_reader()
    for after, wait_sec in ((0, "3"), (4, "-1"), (5, "nan"), (6, "90")):
        _ = faked.get(
            f"/api/sessions/{_CHAT}/partial",
            params={"after": after, "wait_sec": wait_sec},
        )
    _ = faked.get(f"/api/sessions/{_CHAT}/partial")

    assert asked == [(0, 3.0), (4, 0.0), (5, 0.0), (6, 25.0), (0, 0.0)]


def test_a_read_held_while_the_chat_is_unshared_from_its_reader_shows_none_of_it(
    faked: TestClient,
    served: ChatPartials,
    readers: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_assistant()
    _ = _put(faked, _CHAT)
    held = served.wait

    async def unshared_then_put(
        session_id: UUID,
        *,
        after: int,
        wait_sec: float,
    ) -> ChatPartialState:
        readers.discard(_READER)
        _ = served.put(session_id, ChatPartial(turn="t1", text=_CANARY))
        return await held(session_id, after=after, wait_sec=wait_sec)

    monkeypatch.setattr(served, "wait", unshared_then_put)
    _as_reader()
    response = faked.get(
        f"/api/sessions/{_CHAT}/partial",
        params={"after": 1, "wait_sec": 25},
    )

    assert (response.status_code, response.json()) == (
        403,
        {"detail": {"code": "session_audience", "audience": "open"}},
    )


# -- The whole app over PGlite.


@dataclass(frozen=True, slots=True, kw_only=True)
class _Chat:
    """The app, its store, and the chat's session."""

    client: httpx2.AsyncClient
    store: Store
    session: UUID

    @property
    def path(self) -> str:
        """Return the chat's partial path."""
        return f"/api/sessions/{self.session}/partial"

    async def put(self, text: str, *, turn: str = "t1") -> httpx2.Response:
        """Put ``text`` as the installed identity."""
        return await self.client.put(self.path, json={"turn": turn, "text": text})

    async def read(self, **params: str | float) -> httpx2.Response:
        """Read the partial as the installed identity."""
        return await self.client.get(self.path, params=params)


@pytest_asyncio.fixture(loop_scope="session")
async def chat(pglite_route_client: tuple[httpx2.AsyncClient, Store]) -> _Chat:
    """Seed the three accounts and a chat the assistant opened for the browser user."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_science_chat(store, conversation_id=uuid4())
    return _Chat(client=client, store=store, session=session)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistants_opening_key_puts_and_the_chats_starter_reads_it(
    chat: _Chat,
) -> None:
    act_as_assistant("writer")
    put = await chat.put("One")
    browser()
    read = await chat.read()

    assert put.status_code == 204
    assert (read.status_code, read.json()) == (200, _HELD)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_neither_another_writer_nor_another_key_of_the_assistant_writes(
    chat: _Chat,
) -> None:
    act_as_other_agent("writer")
    stranger = await chat.put(_CANARY)
    install_identity(
        make_test_identity(
            user_id=KB_USER_ID,
            api_key_id=uuid4(),
            email=KB_EMAIL,
            role="writer",
        ),
    )
    other_key = await chat.put(_CANARY)
    other_delete = await chat.client.delete(f"{chat.path}/t1")
    browser()
    read = await chat.read()

    assert (stranger.status_code, stranger.json()) == (
        403,
        {"detail": {"code": "not_the_assistant"}},
    )
    assert (other_key.status_code, other_delete.status_code) == (403, 403)
    assert read.json() == _NONE


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_person_the_chat_is_not_shared_with_reads_none_of_it(
    chat: _Chat,
) -> None:
    app.state.config = replace(ASSISTANT_CONFIG, chat_orgs="individual")
    act_as_assistant("writer")
    _ = await chat.put(f"The answer is {_CANARY}")
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    refused = await chat.read()
    browser()
    starter = await chat.read()

    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": {"code": "session_audience", "audience": "open"}},
    )
    assert _CANARY not in refused.text
    assert (starter.status_code, _CANARY in starter.text) == (200, True)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_partial_is_refused_for_a_session_that_is_no_chat_or_has_ended(
    chat: _Chat,
) -> None:
    run = await seed_session(chat.store, ASSISTANT, actor="kb-run")
    act_as_assistant("writer")
    no_chat = await chat.client.put(
        f"/api/sessions/{run}/partial",
        json={"turn": "t1", "text": "x"},
    )
    async with chat.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET status = 'complete', agentsession_ended = now() "
            "WHERE id = $1",
            chat.session,
        )
    after_end = await chat.put("x")

    assert (no_chat.status_code, no_chat.json()) == (
        404,
        {"detail": {"code": "not_a_chat"}},
    )
    assert (after_end.status_code, after_end.json()) == (
        409,
        {"detail": {"code": "session_ended"}},
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_every_partial_route_is_refused_on_a_server_that_streams_none(
    chat: _Chat,
) -> None:
    app.state.config = replace(ASSISTANT_CONFIG, chat_streaming=False)
    act_as_assistant("writer")
    off = [
        await chat.put("x"),
        await chat.client.delete(f"{chat.path}/t1"),
        await chat.read(),
    ]

    assert [(each.status_code, each.json()) for each in off] == [
        (404, {"detail": {"code": "chat_partials_off"}}),
    ] * 3


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_partial_writes_no_row_and_only_its_own_turns_delete_drops_it(
    chat: _Chat,
) -> None:
    before = await _rows(chat.store)
    act_as_assistant("writer")
    _ = await chat.put("One", turn="t2")
    late = await chat.client.delete(f"{chat.path}/t1")
    kept = await chat.read()
    dropped = await chat.client.delete(f"{chat.path}/t2")
    gone = await chat.read()

    assert (late.status_code, kept.json()) == (
        204,
        {"seq": 1, "partial": {"turn": "t2", "text": "One", "done": False}},
    )
    assert (dropped.status_code, gone.json()) == (204, _NONE)
    assert await _rows(chat.store) == before


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_held_read_answers_the_next_put(chat: _Chat) -> None:
    """The key that opened the chat reads it too, so one identity does both."""
    act_as_assistant("writer")
    _ = await chat.put("One")
    held = asyncio.create_task(chat.read(after=1, wait_sec=25))
    unheld = await asyncio.wait_for(chat.read(after=1, wait_sec=math.nan), 5)
    _ = await chat.put("One two")
    answered = await asyncio.wait_for(held, 5)

    assert unheld.json() == _HELD
    assert answered.json() == {
        "seq": 2,
        "partial": {"turn": "t1", "text": "One two", "done": False},
    }


async def _rows(store: Store) -> tuple[object, object, object]:
    """Count the change log, the records and the rows."""
    async with store.engine.acquire() as conn:
        return (
            await conn.fetchval("SELECT count(*) FROM change_log"),
            await conn.fetchval("SELECT count(*) FROM session_records"),
            await conn.fetchval("SELECT count(*) FROM inquiries"),
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
