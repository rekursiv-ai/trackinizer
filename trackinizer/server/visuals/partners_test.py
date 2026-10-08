"""A canvas's partner: the assistant's session by account and name, else the pairing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import uuid

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.partners import (
    assistant_key_may_use,
    attach_partner,
    is_assistant_session,
    opener_email,
    resolve_partner,
)
from trackinizer.server.visuals.workspaces import WorkspaceState
from trackinizer.wire.wire_chats import CHAT_HELPER_CLI


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, PGliteEngine


_KB: Final = Assistant(actor="scout", email="kb@example.com")
_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_KB_USER: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_USER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444441")
_OTHER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444442")
_KB_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444443")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_HELPER: Final = CHAT_HELPER_CLI


@pytest.mark.parametrize(
    ("actor", "email", "expected"),
    [
        ("scout", "kb@example.com", True),
        ("scout#2", "kb@example.com", True),
        ("scout#17", "kb@example.com", True),
        ("scout", "other@example.com", False),
        ("scout#2", "other@example.com", False),
        ("scout#x", "kb@example.com", False),
        ("scout#", "kb@example.com", False),
        ("scout2", "kb@example.com", False),
        ("scribe", "kb@example.com", False),
    ],
)
def test_assistant_is_matched_by_account_and_name_with_its_restart_suffix(
    actor: str,
    email: str,
    expected: bool,
) -> None:
    """The server suffixes a reused handle, so `scout#2` is still scout."""
    assert is_assistant_session(_KB, actor=actor, email=email) is expected


def test_nothing_matches_without_an_assistant() -> None:
    """No configured assistant means no shared session."""
    assert not is_assistant_session(None, actor="scout", email="kb@example.com")


async def _seed(engine: PGliteEngine) -> Store:
    await reset_schema(engine)
    store = Store(engine, embed=StubEmbedder())
    await store.bootstrap()
    async with engine.acquire() as conn:
        for user, email, key in (
            (_USER, "user@example.com", _USER_KEY),
            (_OTHER, "other@example.com", _OTHER_KEY),
            (_KB_USER, "kb@example.com", _KB_KEY),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'T', 'writer', 'active')",
                user,
                email,
            )
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "VALUES ($1, $2, 'k', 'h', $3, 'writer')",
                key,
                user,
                f"trax_{key.hex[-4:]}",
            )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state) VALUES ($1, $2, $3)",
            _WORKSPACE,
            _USER,
            {"visuals": []},
        )
    return store


async def _session(
    conn: Conn,
    *,
    key: uuid.UUID,
    owner: str,
    seq: int,
    ended: bool = False,
    cli: str = "codex",
) -> uuid.UUID:
    session_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
        "agentsession_opened_by_api_key_id, agentsession_cli, agentsession_ended) "
        "VALUES ($1, 'AgentSession', $2, $3, 'user@example.com', 'S', $4, $5, "
        "$7, CASE WHEN $6 THEN clock_timestamp() END)",
        session_id,
        seq,
        "complete" if ended else "active",
        owner,
        key,
        ended,
        cli,
    )
    return session_id


def _polled(*session_ids: uuid.UUID) -> InboundQueue:
    inbound = InboundQueue()
    for session_id in session_ids:
        inbound.mark_poller(session_id)
    return inbound


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistants_newest_live_session_is_the_default_partner(
    pglite_engine: PGliteEngine,
) -> None:
    """A canvas talks to the newest polled session, named as configured."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        older = await _session(conn, key=_KB_KEY, owner="scout", seq=1, ended=True)
        newest = await _session(conn, key=_KB_KEY, owner="scout#2", seq=2)
        squatter = await _session(conn, key=_OTHER_KEY, owner="scout#3", seq=3)
        partner = await resolve_partner(
            conn,
            inbound=_polled(older, newest, squatter),
            assistant=_KB,
            choice="shared",
            owner_id=_USER,
        )
    assert partner is not None
    assert partner.model_dump() == {
        "session_id": newest,
        "actor": "scout",
        "cli": "codex",
        "status": "live",
        "kind": "shared",
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_with_no_live_session_is_still_named_as_unavailable(
    pglite_engine: PGliteEngine,
) -> None:
    """The browser can say scout is down instead of showing no partner."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        silent = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        partner = await resolve_partner(
            conn,
            inbound=_polled(),
            assistant=_KB,
            choice="shared",
            owner_id=_USER,
        )
        none = await resolve_partner(
            conn,
            inbound=_polled(silent),
            assistant=None,
            choice="shared",
            owner_id=_USER,
        )
    assert partner is not None
    assert partner.model_dump() == {
        "session_id": None,
        "actor": "scout",
        "cli": None,
        "status": "unavailable",
        "kind": "shared",
    }
    assert none is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_attach_names_the_assistant_and_the_partner(
    pglite_engine: PGliteEngine,
) -> None:
    """A canvas state carries both, computed, for every reader."""
    await _seed(pglite_engine)
    state = WorkspaceState(id=_WORKSPACE, revision=0, visuals=[])
    async with pglite_engine.acquire() as conn:
        with_assistant = await attach_partner(
            conn,
            state=state,
            inbound=_polled(),
            assistant=_KB,
            owner_id=_USER,
        )
        without = await attach_partner(
            conn,
            state=state,
            inbound=_polled(),
            assistant=None,
            owner_id=_USER,
        )
    assert with_assistant.assistant == "scout"
    assert with_assistant.partner is not None
    assert without.assistant is None
    assert without.partner is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_local_partner_is_the_owners_newest_live_polled_helper(
    pglite_engine: PGliteEngine,
) -> None:
    """Only a helper the owner's own live key opened and is polling can answer."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        older = await _session(conn, key=_USER_KEY, owner="mine", seq=1, cli=_HELPER)
        newest = await _session(conn, key=_USER_KEY, owner="mine#2", seq=2, cli=_HELPER)
        not_a_helper = await _session(conn, key=_USER_KEY, owner="run", seq=3)
        foreign = await _session(
            conn,
            key=_OTHER_KEY,
            owner="theirs",
            seq=4,
            cli=_HELPER,
        )
        ended = await _session(
            conn,
            key=_USER_KEY,
            owner="gone",
            seq=5,
            cli=_HELPER,
            ended=True,
        )
        await _session(conn, key=_USER_KEY, owner="mute", seq=6, cli=_HELPER)
        inbound = _polled(older, newest, not_a_helper, foreign, ended)
        partner = await resolve_partner(
            conn,
            inbound=inbound,
            assistant=None,
            choice="local",
            owner_id=_USER,
        )
        elsewhere = await resolve_partner(
            conn,
            inbound=_polled(foreign, not_a_helper, ended),
            assistant=_KB,
            choice="local",
            owner_id=_USER,
        )
        state = WorkspaceState(
            id=_WORKSPACE,
            revision=0,
            visuals=[],
            partner_choice="local",
        )
        attached = await attach_partner(
            conn,
            state=state,
            inbound=inbound,
            assistant=None,
            owner_id=_USER,
        )
    assert partner is not None
    assert partner.model_dump() == {
        "session_id": newest,
        "actor": "mine#2",
        "cli": _HELPER,
        "status": "live",
        "kind": "local",
    }
    assert elsewhere is not None
    assert elsewhere.model_dump() == {
        "session_id": None,
        "actor": None,
        "cli": None,
        "status": "unavailable",
        "kind": "local",
    }
    assert attached.partner == partner
    assert attached.assistant is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_revoking_the_owners_key_takes_its_helper_out_of_chat(
    pglite_engine: PGliteEngine,
) -> None:
    """Revoking the owner's key takes its helper out of Chat."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        helper = await _session(conn, key=_USER_KEY, owner="mine", seq=1, cli=_HELPER)
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            _USER_KEY,
        )
        partner = await resolve_partner(
            conn,
            inbound=_polled(helper),
            assistant=None,
            choice="local",
            owner_id=_USER,
        )
    assert partner is not None
    assert partner.status == "unavailable"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_owners_own_key_may_use_a_canvas_with_its_local_helper(
    pglite_engine: PGliteEngine,
) -> None:
    """With a local partner the owner's key passes the rule once they have talked."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        helper = await _session(conn, key=_USER_KEY, owner="mine", seq=1, cli=_HELPER)
        partner = await resolve_partner(
            conn,
            inbound=_polled(helper),
            assistant=None,
            choice="local",
            owner_id=_USER,
        )
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            workspace_id=_WORKSPACE,
            partner=partner,
            api_key_id=_USER_KEY,
        )
        await conn.execute(
            "INSERT INTO chat_conversations "
            "(id, user_id, workspace_id, title, partner_session_id) "
            "VALUES ($1, $2, $3, 't', $4)",
            uuid.uuid4(),
            _USER,
            _WORKSPACE,
            helper,
        )
        assert await assistant_key_may_use(
            conn,
            owner_id=_USER,
            workspace_id=_WORKSPACE,
            partner=partner,
            api_key_id=_USER_KEY,
        )
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            workspace_id=_WORKSPACE,
            partner=partner,
            api_key_id=_OTHER_KEY,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_key_needs_a_conversation_on_that_canvas(
    pglite_engine: PGliteEngine,
) -> None:
    """Talking to scout elsewhere gives it nothing here."""
    await _seed(pglite_engine)
    other_workspace = uuid.uuid4()
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, is_default, state) "
            "VALUES ($1, $2, FALSE, $3)",
            other_workspace,
            _USER,
            {"visuals": []},
        )
        session = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        partner = await resolve_partner(
            conn,
            inbound=_polled(session),
            assistant=_KB,
            choice="shared",
            owner_id=_USER,
        )

        async def may(workspace: uuid.UUID, *, key: uuid.UUID) -> bool:
            return await assistant_key_may_use(
                conn,
                owner_id=_USER,
                workspace_id=workspace,
                partner=partner,
                api_key_id=key,
            )

        assert not await may(_WORKSPACE, key=_KB_KEY)
        await conn.execute(
            "INSERT INTO chat_conversations "
            "(id, user_id, workspace_id, title, partner_session_id) "
            "VALUES ($1, $2, $3, 't', $4)",
            uuid.uuid4(),
            _USER,
            _WORKSPACE,
            session,
        )
        assert await may(_WORKSPACE, key=_KB_KEY)
        assert not await may(other_workspace, key=_KB_KEY)
        assert not await may(_WORKSPACE, key=_OTHER_KEY)
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            workspace_id=_WORKSPACE,
            partner=None,
            api_key_id=_KB_KEY,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opener_email_names_the_account_of_a_key(
    pglite_engine: PGliteEngine,
) -> None:
    """The opening key says whose session it is."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        assert await opener_email(conn, api_key_id=_KB_KEY) == "kb@example.com"
        assert await opener_email(conn, api_key_id=uuid.uuid4()) is None
        assert await opener_email(conn, api_key_id=None) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
