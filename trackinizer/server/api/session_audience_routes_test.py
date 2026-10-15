"""Hiding a session, and showing it again, over the real app and database.

Ann owns a session; Bob is a writer of the same server, which is one organisation, and
Ada its admin. Only an owner moves a session between ``open`` and ``private``: its
account, the person whose key opened it, or for a chat its starter. A ``staff``
session moves for no one, and every move is audited.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from treekle import from_plain, json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import install_identity, make_test_identity
from trackinizer.server.config import Assistant, Config
from trackinizer.server.store.core import Store
from trackinizer.wire.wire_science_chat import chat_session_id


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.auth import Role


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_ANN: Final = "ann@lab.example"
_BOB: Final = "bob@lab.example"
_ADA: Final = "ada@lab.example"
_SCOUT: Final = Assistant(actor="scout", email="scout@service.invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Served:
    """The app over a database, and its people's keys."""

    http: httpx2.AsyncClient
    store: Store
    keys: dict[str, UUID]

    @classmethod
    def act_as(cls, email: str, role: Role = "writer") -> None:
        """Answer every following request as ``email``, from a browser."""
        install_identity(
            replace(
                make_test_identity(user_id=uuid4(), api_key_id=None, email=email),
                role=role,
                read_audiences=frozenset({"open", "private"}),
            ),
        )

    async def session(
        self,
        *,
        account: str = _ANN,
        audience: str = "open",
        opener: str | None = None,
        cli_session_id: str | None = None,
    ) -> UUID:
        """Insert a session of ``account``, opened by ``opener``'s key when named."""
        session_id = uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
                "agentsession_audience, agentsession_cli_session_id, "
                "agentsession_opened_by_api_key_id) VALUES ($1, 'AgentSession', "
                "nextval('seq_agentsession'), 'active', $2, 'a run', $3, $4, $5, $6)",
                session_id,
                account,
                f"run-{session_id.hex[:8]}",
                audience,
                cli_session_id,
                None if opener is None else self.keys[opener],
            )
        return session_id

    async def move(self, session_id: UUID, audience: str) -> httpx2.Response:
        """Ask to move ``session_id`` into ``audience``."""
        return await self.http.post(
            f"/api/sessions/{session_id}/audience",
            json={"audience": audience},
        )

    async def audience_of(self, session_id: UUID) -> str:
        """Return the audience the database holds for ``session_id``."""
        async with self.store.engine.acquire() as conn:
            return from_plain(
                await conn.fetchval(
                    "SELECT agentsession_audience FROM inquiries WHERE id = $1",
                    session_id,
                ),
                str,
            )

    async def audits(self, session_id: UUID) -> list[tuple[str, str, str]]:
        """Return each audience move audited for ``session_id``: actor, old, new."""
        async with self.store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor, old_agentsession_audience AS old, "
                "new_agentsession_audience AS new FROM change_log "
                "WHERE subject_id = $1 AND kind = 'agentsession_audience' "
                "ORDER BY created, id",
                session_id,
            )
        return [(str(row["actor"]), str(row["old"]), str(row["new"])) for row in rows]


@pytest_asyncio.fixture(loop_scope="session")
async def served(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Served]:
    """Serve one organisation, with the assistant, over Ann's, Bob's and Scout's keys."""
    client, store = pglite_route_client
    monkeypatch.setattr(
        app.state,
        "config",
        Config(assistant=_SCOUT, chat_orgs="single"),
        raising=False,
    )
    keys: dict[str, UUID] = {}
    async with store.engine.acquire() as conn:
        for email in (_ANN, _BOB, _ADA, _SCOUT.email):
            user = await conn.fetchval(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'n', 'writer', 'active') RETURNING id",
                uuid4(),
                email,
            )
            keys[email] = from_plain(
                await conn.fetchval(
                    "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, "
                    "role) VALUES ($1, $2, 'k', 'test-hash', 'trax_test', 'writer') "
                    "RETURNING id",
                    uuid4(),
                    user,
                ),
                UUID,
            )
    yield _Served(http=client, store=store, keys=keys)


async def test_the_owner_hides_a_session_and_shows_it_again(served: _Served) -> None:
    """Hidden, Bob reads its row and not its transcript; shown, he reads both again."""
    session = await served.session()
    served.act_as(_ANN)

    hidden = await served.move(session, "private")
    served.act_as(_BOB)
    while_hidden = await served.http.get(f"/api/sessions/{session}/records")
    row = await served.http.get(f"/api/inquiries/{session}")
    served.act_as(_ANN)
    own = await served.http.get(f"/api/sessions/{session}/records")
    shown = await served.move(session, "open")
    served.act_as(_BOB)
    after = await served.http.get(f"/api/sessions/{session}/records")

    assert (hidden.status_code, json_loads(hidden.content, dict[str, object])) == (
        200,
        {"session_id": str(session), "audience": "private", "sessions": [str(session)]},
    )
    assert (while_hidden.status_code, row.status_code, own.status_code) == (
        403,
        200,
        200,
    )
    assert (shown.status_code, after.status_code) == (200, 200)
    assert await served.audience_of(session) == "open"


async def test_the_user_whose_key_opened_it_is_an_owner_too(served: _Served) -> None:
    """Bob's key opened a run held under Ann's account: either of them hides it."""
    session = await served.session(opener=_BOB)
    served.act_as(_BOB)

    assert (await served.move(session, "private")).status_code == 200
    assert await served.audience_of(session) == "private"


@pytest.mark.parametrize(
    ("caller", "role"),
    [(_BOB, "writer"), (_ADA, "admin")],
    ids=["another writer", "an admin"],
)
async def test_only_the_owner_hides(served: _Served, caller: str, role: Role) -> None:
    session = await served.session()
    served.act_as(caller, role)

    refused = await served.move(session, "private")

    assert (refused.status_code, json_loads(refused.content, dict[str, object])) == (
        403,
        {"detail": {"code": "session_not_owner"}},
    )
    assert await served.audience_of(session) == "open"


async def test_an_admin_cannot_take_a_hidden_session_to_show_it(
    served: _Served,
) -> None:
    """Its account is an owner, so moving it to Ada would let her undo Ann's hide."""
    hidden, shown = await served.session(), await served.session()
    served.act_as(_ANN)
    _ = await served.move(hidden, "private")
    served.act_as(_ADA, "admin")

    taken = await served.http.put(
        f"/api/inquiries/{hidden}/account",
        json={"value": _ADA},
    )
    unhidden = await served.move(hidden, "open")
    moved = await served.http.put(
        f"/api/inquiries/{shown}/account",
        json={"value": _ADA},
    )

    assert (taken.status_code, unhidden.status_code, moved.status_code) == (
        403,
        403,
        200,
    )
    assert await served.audience_of(hidden) == "private"


async def test_a_staff_session_is_never_hidden(served: _Served) -> None:
    session = await served.session(audience="staff")
    served.act_as(_ANN)

    refused = await served.move(session, "private")

    assert (refused.status_code, json_loads(refused.content, dict[str, object])) == (
        409,
        {"detail": {"code": "session_staff"}},
    )
    assert await served.audience_of(session) == "staff"


async def test_hiding_a_chat_hides_every_session_under_its_id(served: _Served) -> None:
    """Ann began the chat with the assistant; Bob's key opened a session under it later."""
    conversation = uuid4()
    first = await served.session(
        opener=_SCOUT.email,
        cli_session_id=chat_session_id(conversation),
    )
    later = await served.session(
        account=_BOB,
        opener=_BOB,
        cli_session_id=chat_session_id(conversation),
    )
    served.act_as(_BOB)
    refused = await served.move(first, "private")
    served.act_as(_ANN)

    hidden = await served.move(later, "private")

    assert refused.status_code == 403
    assert hidden.status_code == 200, hidden.text
    assert sorted(
        from_plain(
            json_loads(hidden.content, dict[str, object])["sessions"],
            list[str],
        ),
    ) == sorted([str(first), str(later)])
    assert [await served.audience_of(each) for each in (first, later)] == [
        "private",
        "private",
    ]


async def test_each_change_is_audited(served: _Served) -> None:
    """Asking for the audience a session has moves nothing and audits nothing."""
    session = await served.session()
    served.act_as(_ANN)

    answers = [
        await served.move(session, audience)
        for audience in ("private", "private", "open")
    ]

    assert [
        json_loads(each.content, dict[str, object])["sessions"] for each in answers
    ] == [[str(session)], [], [str(session)]]
    assert await served.audits(session) == [
        (_ANN, "open", "private"),
        (_ANN, "private", "open"),
    ]


async def test_an_audience_that_is_neither_open_nor_private_is_refused(
    served: _Served,
) -> None:
    session = await served.session()
    served.act_as(_ANN)

    assert (await served.move(session, "staff")).status_code == 422
    assert (await served.move(uuid4(), "private")).status_code == 404


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
