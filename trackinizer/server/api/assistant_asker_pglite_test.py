"""The assistant reaches a session it cannot read only for an asker who may.

The configured assistant reads ``open`` only, so a ``staff`` session is beyond it. It
may still be sent a line the assistant relays for a signed-in user, on that user's
rights and none of its own: the user is the session's account, or a writer who reads
it. Each send test goes through the real routes, by the direct enqueue and by name,
and reads what reached the session's queue. The assistant reads a session's
transcript for a user into the chat it answers them in, through each route that reads
one by id, exactly when the user reads both; a ``staff`` session only once the chat
is ``staff`` too, so no member who lacks staff reads what it read, which only the
chat's starter makes it; and any other only when everyone who reads the chat reads
it. The server is one organisation, as alpha is, unless a test says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID, uuid4

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.server.store.core import Store
from trackinizer.wire.audience import AUDIENCES
from trackinizer.wire.wire_science_chat import chat_session_id


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.auth import Role
    from trackinizer.server.inbound import InboundQueue


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

type _Route = Literal["inbound", "messages"]
_ROUTES: Final[tuple[_Route, ...]] = ("inbound", "messages")
_READS: Final = ("parts", "records", "turns")

_ASSISTANT: Final = "scout@example.com"
_STAFF: Final = "lee@staff.example"
_STAFF_VIEWER: Final = "vs@staff.example"
_STAFF_ADMIN: Final = "ada@staff.example"
_OPEN_WRITER: Final = "wes@open.example"
_OPEN_VIEWER: Final = "vi@open.example"
_OPEN_ADMIN: Final = "root@open.example"
_DISABLED: Final = "dis@staff.example"
_GHOST: Final = "ghost@staff.example"
"""Named as staff, with no account: the name alone must lend nothing."""
_EVE: Final = "eve@example.com"
"""A colleague of the account the open sessions are held under."""

_CONFIG: Final = Config(
    assistant=Assistant(actor="scout", email=_ASSISTANT),
    staff=(_STAFF, _STAFF_VIEWER, _STAFF_ADMIN, _DISABLED, _GHOST),
    chat_orgs="single",
)
_USERS: Final = (
    (_ASSISTANT, "writer", "active"),
    (_STAFF, "writer", "active"),
    (_STAFF_VIEWER, "viewer", "active"),
    (_STAFF_ADMIN, "admin", "active"),
    (_OPEN_WRITER, "writer", "active"),
    (_OPEN_VIEWER, "viewer", "active"),
    (_OPEN_ADMIN, "admin", "active"),
    (_DISABLED, "writer", "disabled"),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Live:
    """A live session, by the name a send addresses it with."""

    id: UUID
    actor: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Served:
    """The real routes over four live sessions, and who may send to them."""

    http: httpx2.AsyncClient
    store: Store
    others: _Live
    wes_own: _Live
    vi_own: _Live
    public: _Live

    @classmethod
    def act_as(cls, email: str, *, role: Role = "writer", staff: bool = False) -> None:
        """Answer every following request as the user with this account."""
        install_identity(
            replace(
                make_test_identity(
                    user_id=uuid4(),
                    api_key_id=None,
                    email=email,
                    role=role,
                ),
                read_audiences=AUDIENCES if staff else frozenset({"open", "private"}),
            ),
        )

    async def send(
        self,
        route: _Route,
        to: _Live,
        *,
        asker: str | None = None,
    ) -> httpx2.Response:
        """Send ``hi`` to ``to`` by ``route``, naming ``asker`` when there is one."""
        named = {} if asker is None else {"asker": asker}
        if route == "inbound":
            return await self.http.post(
                f"/api/sessions/{to.id}/inbound",
                json={"text": "hi", **named},
            )
        return await self.http.post(
            "/api/messages",
            json={"actor": to.actor, "room": "ops", "text": "hi", **named},
        )

    @classmethod
    def queued(cls, to: _Live) -> int:
        """Return how many lines wait for ``to``."""
        return cast("InboundQueue", app.state.inbound).pending(to.id)

    async def reads(
        self,
        of: _Live,
        *,
        asker: str | None = None,
        chat: UUID | None = None,
    ) -> list[int]:
        """Read ``of`` by each transcript route; return the status of each."""
        named = {
            name: str(value)
            for name, value in (("asker", asker), ("chat", chat))
            if value is not None
        }
        return [
            (
                await self.http.get(f"/api/sessions/{of.id}/{route}", params=named)
            ).status_code
            for route in _READS
        ]

    async def reads_into(self, of: _Live, *, asker: str, chat: UUID) -> int:
        """Return the status of reading ``of`` into ``chat`` for ``asker``, by one route.

        The read routes share one access gate, so a single route settles which chat a
        session is read into; :meth:`reads` reads all three where the route matters.
        """
        response = await self.http.get(
            f"/api/sessions/{of.id}/records",
            params={"asker": asker, "chat": str(chat)},
        )
        return response.status_code

    async def chat(self, *, starter: str, audience: str = "open") -> _Chat:
        """Begin a chat as the assistant does, for ``starter``, in ``audience``."""
        conversation, session = uuid4(), uuid4()
        async with self.store.engine.acquire() as conn:
            key = await conn.fetchval(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "SELECT $1, id, 'scout', 'test-hash', 'trax_test', 'writer' "
                "FROM users WHERE email = $2 RETURNING id",
                uuid4(),
                _ASSISTANT,
            )
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
                "agentsession_cli, agentsession_audience, agentsession_cli_session_id, "
                "agentsession_opened_by_api_key_id) VALUES ($1, 'AgentSession', "
                "nextval('seq_agentsession'), 'active', $2, 'Science chat', $3, "
                "'assistant', $4, $5, $6)",
                session,
                starter,
                f"chat-{conversation.hex[:12]}",
                audience,
                chat_session_id(conversation),
                key,
            )
        return _Chat(conversation=conversation, session=_Live(id=session, actor=""))

    async def share(self, chat: _Chat, *, email: str) -> None:
        """Share ``chat`` with ``email``, as its starter does."""
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
                "VALUES ($1, $2, 'starter')",
                chat.conversation,
                email,
            )

    async def chat_audiences(self, chat: _Chat) -> list[str]:
        """Return the audience of every session under the chat's id."""
        async with self.store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT agentsession_audience FROM inquiries "
                "WHERE agentsession_cli_session_id = $1",
                chat_session_id(chat.conversation),
            )
        return [str(row["agentsession_audience"]) for row in rows]

    async def moves(self, chat: _Chat) -> list[tuple[str, str, str, str]]:
        """Return each audience move the change log audited for the chat's session."""
        async with self.store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor, reason, old_agentsession_audience, "
                "new_agentsession_audience FROM change_log "
                "WHERE subject_id = $1 AND kind = 'agentsession_audience'",
                chat.session.id,
            )
        return [
            (
                str(row["actor"]),
                str(row["reason"]),
                str(row["old_agentsession_audience"]),
                str(row["new_agentsession_audience"]),
            )
            for row in rows
        ]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Chat:
    """A science chat the assistant began, and its one session."""

    conversation: UUID
    session: _Live


async def _live(store: Store, *, actor: str, account: str, audience: str) -> _Live:
    """Insert a live session of ``account`` in ``audience`` that is being polled."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "agentsession_cli, agentsession_rooms, agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, $3, $3, 'codex', ARRAY['ops'], $4)",
            session_id,
            account,
            actor,
            audience,
        )
    cast("InboundQueue", app.state.inbound).mark_poller(session_id)
    return _Live(id=session_id, actor=actor)


@pytest_asyncio.fixture(loop_scope="session")
async def served(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Served]:
    """Serve with the assistant and the staff configured, over four live sessions."""
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", _CONFIG)
    async with store.engine.acquire() as conn:
        for email, role, status in _USERS:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, "
                "visual_canvas_enabled) VALUES ($1, $2, 'n', $3, $4, TRUE)",
                uuid4(),
                email,
                role,
                status,
            )
    yield _Served(
        http=client,
        store=store,
        others=await _live(
            store,
            actor="lead",
            account="tester@example.com",
            audience="staff",
        ),
        wes_own=await _live(
            store,
            actor="wes-run",
            account=_OPEN_WRITER,
            audience="staff",
        ),
        vi_own=await _live(
            store,
            actor="vi-run",
            account=_OPEN_VIEWER,
            audience="staff",
        ),
        public=await _live(
            store,
            actor="pub",
            account="tester@example.com",
            audience="open",
        ),
    )


def _refusal(response: httpx2.Response) -> object:
    """Return the body of a 403 and require that it names the audience."""
    assert response.status_code == 403, response.text
    return response.json()


@pytest.mark.parametrize("route", _ROUTES)
async def test_it_is_refused_a_staff_session_when_it_names_no_asker(
    served: _Served,
    route: _Route,
) -> None:
    served.act_as(_ASSISTANT)

    refused = await served.send(route, served.others)

    assert _refusal(refused) == {
        "detail": {"code": "session_audience", "audience": "staff"},
    }
    assert served.queued(served.others) == 0


@pytest.mark.parametrize("route", _ROUTES)
async def test_it_relays_for_an_asker_who_holds_the_audience_and_may_write(
    served: _Served,
    route: _Route,
) -> None:
    served.act_as(_ASSISTANT)

    sent = await served.send(route, served.others, asker=_STAFF)

    assert sent.status_code == 200, sent.text
    assert served.queued(served.others) == 1


@pytest.mark.parametrize("route", _ROUTES)
async def test_it_relays_for_an_admin_who_holds_the_audience(
    served: _Served,
    route: _Route,
) -> None:
    served.act_as(_ASSISTANT)

    sent = await served.send(route, served.others, asker=_STAFF_ADMIN)

    assert sent.status_code == 200, sent.text


@pytest.mark.parametrize("route", _ROUTES)
async def test_it_relays_for_the_user_whose_session_it_is(
    served: _Served,
    route: _Route,
) -> None:
    """An open writer steers the staff session attributed to them, and no one else's."""
    served.act_as(_ASSISTANT)

    own = await served.send(route, served.wes_own, asker=_OPEN_WRITER)
    others = await served.send(route, served.others, asker=_OPEN_WRITER)

    assert own.status_code == 200, own.text
    assert served.queued(served.wes_own) == 1
    assert _refusal(others) == {
        "detail": {"code": "session_audience", "audience": "staff"},
    }
    assert served.queued(served.others) == 0


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(
    ("asker", "session"),
    [
        pytest.param("gone@staff.example", "others", id="an account the server lacks"),
        pytest.param(_DISABLED, "others", id="a disabled account"),
        pytest.param(_STAFF_VIEWER, "others", id="a viewer who holds the audience"),
        pytest.param(_OPEN_VIEWER, "vi_own", id="a viewer whose session it is"),
    ],
)
async def test_it_is_refused_for_an_asker_who_may_not_write_there(
    served: _Served,
    route: _Route,
    asker: str,
    session: str,
) -> None:
    served.act_as(_ASSISTANT)
    target = {"others": served.others, "vi_own": served.vi_own}[session]

    refused = await served.send(route, target, asker=asker)

    assert _refusal(refused) == {
        "detail": {"code": "session_audience", "audience": "staff"},
    }
    assert served.queued(target) == 0


@pytest.mark.parametrize("route", _ROUTES)
async def test_it_finds_the_asker_whatever_the_case_it_writes_the_account_in(
    served: _Served,
    route: _Route,
) -> None:
    served.act_as(_ASSISTANT)

    sent = await served.send(route, served.others, asker=_STAFF.upper())

    assert sent.status_code == 200, sent.text


@pytest.mark.parametrize("route", _ROUTES)
async def test_naming_an_asker_gives_another_sender_nothing(
    served: _Served,
    route: _Route,
) -> None:
    served.act_as(_OPEN_WRITER)

    refused = await served.send(route, served.others, asker=_STAFF)

    assert _refusal(refused) == {
        "detail": {"code": "session_audience", "audience": "staff"},
    }
    assert served.queued(served.others) == 0


@pytest.mark.parametrize("route", _ROUTES)
async def test_it_needs_no_asker_for_a_session_it_reads_itself(
    served: _Served,
    route: _Route,
) -> None:
    """An open session is any writer's to message, the assistant's included."""
    served.act_as(_ASSISTANT)

    sent = await served.send(route, served.public)

    assert sent.status_code == 200, sent.text
    assert served.queued(served.public) == 1


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_blank_asker_is_not_a_request(
    served: _Served,
    route: _Route,
) -> None:
    served.act_as(_ASSISTANT)

    refused = await served.send(route, served.others, asker="  ")

    assert refused.status_code == 422, refused.text
    assert served.queued(served.others) == 0


@pytest.mark.parametrize("asker", [_STAFF, _STAFF_VIEWER])
async def test_it_reads_a_staff_transcript_for_an_asker_who_holds_staff(
    served: _Served,
    asker: str,
) -> None:
    """A viewer reads what they hold as any writer does, into their chat."""
    chat = await served.chat(starter=asker)
    served.act_as(_ASSISTANT)

    assert await served.reads(served.others) == [403] * 3
    assert (
        await served.reads(
            served.others,
            asker=asker,
            chat=chat.conversation,
        )
        == [200] * 3
    )


async def test_it_reads_for_an_asker_no_more_than_they_read_themselves(
    served: _Served,
) -> None:
    """Their own session in an audience they lack is no more theirs to read through it."""
    chat = await served.chat(starter=_OPEN_WRITER)
    served.act_as(_OPEN_WRITER)
    themselves = await served.reads(served.wes_own)
    served.act_as(_ASSISTANT)

    assert themselves == [403] * 3
    assert (
        await served.reads(
            served.wes_own,
            asker=_OPEN_WRITER,
            chat=chat.conversation,
        )
        == [403] * 3
    )


@pytest.mark.parametrize("asker", [_GHOST, _DISABLED, "", "  "])
async def test_naming_an_asker_no_active_user_holds_reads_nothing(
    served: _Served,
    asker: str,
) -> None:
    """Not even what the assistant reads as itself: the read is the asker's alone."""
    chat = await served.chat(starter=_STAFF)
    served.act_as(_ASSISTANT)

    assert await served.reads(served.public) == [200] * 3
    for of in (served.public, served.others):
        assert await served.reads(of, asker=asker, chat=chat.conversation) == [403] * 3


async def test_naming_an_asker_reads_nothing_for_another_caller(
    served: _Served,
) -> None:
    chat = await served.chat(starter=_STAFF)
    served.act_as(_OPEN_WRITER)

    assert (
        await served.reads(
            served.others,
            asker=_STAFF,
            chat=chat.conversation,
        )
        == [403] * 3
    )


async def test_a_staff_read_for_an_asker_makes_their_chat_staff(
    served: _Served,
) -> None:
    """Lee's chat that he reads a staff lead into is staff's alone after."""
    chat = await served.chat(starter=_STAFF)
    served.act_as(_OPEN_WRITER)
    before = await served.reads(chat.session)
    served.act_as(_ASSISTANT)

    read = await served.reads(served.others, asker=_STAFF, chat=chat.conversation)
    served.act_as(_OPEN_WRITER)
    after = await served.reads(chat.session)
    served.act_as(_STAFF, staff=True)
    staffed = await served.reads(chat.session)

    assert (before, read, after, staffed) == (
        [200] * 3,
        [200] * 3,
        [403] * 3,
        [200] * 3,
    )
    assert await served.chat_audiences(chat) == ["staff"]
    assert await served.moves(chat) == [
        (_ASSISTANT, f"read {served.others.id} for {_STAFF}", "open", "staff"),
    ]


async def test_a_staff_read_takes_no_one_elses_chat_from_them(
    served: _Served,
) -> None:
    """Made staff, Wes's chat would be read by staff alone, and no longer by Wes.

    A chat that is staff already loses no one by another staff read.
    """
    wes = await served.chat(starter=_OPEN_WRITER)
    staffed = await served.chat(starter=_STAFF_ADMIN, audience="staff")
    served.act_as(_ASSISTANT)

    refused = await served.http.get(
        f"/api/sessions/{served.others.id}/records",
        params={"asker": _STAFF, "chat": str(wes.conversation)},
    )
    again = await served.reads(
        served.others,
        asker=_STAFF,
        chat=staffed.conversation,
    )
    served.act_as(_OPEN_WRITER)
    kept = await served.reads(wes.session)

    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": {"code": "chat_not_starter"}},
    )
    assert (again, kept) == ([200] * 3, [200] * 3)
    assert await served.chat_audiences(wes) == ["open"]


async def test_a_session_is_read_only_into_a_chat_whose_every_reader_reads_it(
    served: _Served,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eve's colleague's run is her organisation's, and Wes is of another.

    It goes into her own chat, and not into one Wes reads: his own that he shared with
    her, or hers once she shares it with him.
    """
    patch_state(monkeypatch, "config", replace(_CONFIG, chat_orgs="domain"))
    async with served.store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, hosted_domain) "
            "VALUES ($1, $2, 'n', 'writer', 'active', 'example.com')",
            uuid4(),
            _EVE,
        )
    eve, wes = await served.chat(starter=_EVE), await served.chat(starter=_OPEN_WRITER)
    await served.share(wes, email=_EVE)
    served.act_as(_ASSISTANT)

    # Which chat a session may be read into is one decision, made in
    # ``require_session_access``; the three read routes share it, and the sibling
    # tests prove each wires it, so the org-mode rule is read through one route here.
    own = await served.reads_into(served.public, asker=_EVE, chat=eve.conversation)
    theirs = await served.reads_into(served.public, asker=_EVE, chat=wes.conversation)
    await served.share(eve, email=_OPEN_WRITER)
    shared = await served.reads_into(served.public, asker=_EVE, chat=eve.conversation)

    assert (own, theirs, shared) == (200, 409, 409)


async def test_a_staff_read_for_an_asker_names_the_chat(served: _Served) -> None:
    served.act_as(_ASSISTANT)

    response = await served.http.get(
        f"/api/sessions/{served.others.id}/records",
        params={"asker": _STAFF},
    )

    assert (response.status_code, response.json()) == (
        422,
        {"detail": {"code": "chat_required"}},
    )


async def test_a_staff_read_for_an_asker_outside_the_chat_is_refused(
    served: _Served,
) -> None:
    """A chat that is not theirs, or that no session carries, is no place to read into."""
    hidden = await served.chat(starter=_OPEN_WRITER, audience="private")
    served.act_as(_ASSISTANT)

    for chat in (hidden.conversation, uuid4()):
        response = await served.http.get(
            f"/api/sessions/{served.others.id}/records",
            params={"asker": _STAFF, "chat": str(chat)},
        )
        assert (response.status_code, response.json()) == (
            403,
            {"detail": {"code": "chat_not_member"}},
        )
    assert await served.chat_audiences(hidden) == ["private"]


async def test_a_hidden_chat_is_never_made_staff(served: _Served) -> None:
    """Its starter hid it; staff would open it to every member who holds staff."""
    chat = await served.chat(starter=_STAFF, audience="private")
    served.act_as(_ASSISTANT)

    response = await served.http.get(
        f"/api/sessions/{served.others.id}/records",
        params={"asker": _STAFF, "chat": str(chat.conversation)},
    )

    assert (response.status_code, response.json()) == (
        409,
        {"detail": {"code": "chat_private"}},
    )
    assert await served.chat_audiences(chat) == ["private"]


async def test_a_read_for_an_admin_asker_holds_no_oversight(
    served: _Served,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin reads every session, but nothing of it goes into a chat for them."""
    patch_state(monkeypatch, "config", replace(_CONFIG, chat_orgs="individual"))
    chat = await served.chat(starter=_OPEN_ADMIN)
    served.act_as(_OPEN_ADMIN, role="admin")
    themselves = await served.reads(served.public)
    served.act_as(_ASSISTANT)

    read = await served.reads(served.public, asker=_OPEN_ADMIN, chat=chat.conversation)

    assert (themselves, read) == ([200] * 3, [403] * 3)


async def test_an_open_read_leaves_the_chat_open(served: _Served) -> None:
    chat = await served.chat(starter=_OPEN_WRITER)
    served.act_as(_ASSISTANT)

    read = await served.reads(served.public, asker=_STAFF, chat=chat.conversation)

    assert read == [200] * 3
    assert await served.chat_audiences(chat) == ["open"]
    assert await served.moves(chat) == []


async def test_a_hidden_session_is_not_read_into_its_owners_chat(
    served: _Served,
) -> None:
    """Its owner reads it; no chat could keep it from the people the chat reaches."""
    hidden = await _live(
        served.store,
        actor="mine",
        account=_OPEN_WRITER,
        audience="private",
    )
    chat = await served.chat(starter=_OPEN_WRITER)
    served.act_as(_OPEN_WRITER)
    themselves = await served.reads(hidden)
    served.act_as(_ASSISTANT)

    read = await served.reads(hidden, asker=_OPEN_WRITER, chat=chat.conversation)

    assert (themselves, read) == ([200] * 3, [403] * 3)
    assert await served.chat_audiences(chat) == ["open"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
