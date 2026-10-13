"""The assistant relays a line to a session it cannot read only for an asker who may write.

The configured assistant reads ``open`` only, so a ``staff`` session is beyond it. It
may still be sent a line the assistant relays for a signed-in user, on that user's
rights and none of its own: the user is the session's account, or a writer who holds
its audience. Each test sends through the real routes, by the direct enqueue and by
name, and reads what reached the session's queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID, uuid4

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import install_identity, make_test_identity
from trackinizer.server.config import Assistant, Config


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.auth import Role
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

type _Route = Literal["inbound", "messages"]
_ROUTES: Final[tuple[_Route, ...]] = ("inbound", "messages")

_ASSISTANT: Final = "scout@example.com"
_STAFF: Final = "lee@staff.example"
_STAFF_VIEWER: Final = "vs@staff.example"
_STAFF_ADMIN: Final = "ada@staff.example"
_OPEN_WRITER: Final = "wes@open.example"
_OPEN_VIEWER: Final = "vi@open.example"
_DISABLED: Final = "dis@staff.example"
_CONFIG: Final = Config(
    assistant=Assistant(actor="scout", email=_ASSISTANT),
    staff=(_STAFF, _STAFF_VIEWER, _STAFF_ADMIN, _DISABLED),
)
_USERS: Final = (
    (_STAFF, "writer", "active"),
    (_STAFF_VIEWER, "viewer", "active"),
    (_STAFF_ADMIN, "admin", "active"),
    (_OPEN_WRITER, "writer", "active"),
    (_OPEN_VIEWER, "viewer", "active"),
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
    others: _Live
    wes_own: _Live
    vi_own: _Live
    public: _Live

    @classmethod
    def act_as(cls, email: str, role: Role = "writer") -> None:
        """Answer every following request as the user with this account."""
        install_identity(
            make_test_identity(
                user_id=uuid4(),
                api_key_id=None,
                email=email,
                role=role,
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
    monkeypatch.setattr(app.state, "config", _CONFIG, raising=False)
    async with store.engine.acquire() as conn:
        for email, role, status in _USERS:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, "
                "visual_workspace_enabled) VALUES ($1, $2, 'n', $3, $4, TRUE)",
                uuid4(),
                email,
                role,
                status,
            )
    yield _Served(
        http=client,
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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
