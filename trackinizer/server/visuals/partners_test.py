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
    assistant_holds_chat,
    assistant_key_may_use,
    attach_partner,
    is_assistant_session,
    is_chat_session,
    live_chat_session,
    opener_email,
    resolve_partner,
)
from trackinizer.server.visuals.workspaces import WorkspaceState
from trackinizer.wire.wire_science_chat import (
    CHAT_HELPER_CLI,
    SCIENCE_CHAT_LABEL,
    chat_session_id,
    poster_label,
)


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
    cli_session_id: str | None = None,
    labels: list[str] | None = None,
    account: str = "user@example.com",
) -> uuid.UUID:
    session_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
        "agentsession_opened_by_api_key_id, agentsession_cli, agentsession_ended, "
        "agentsession_cli_session_id, labels) "
        "VALUES ($1, 'AgentSession', $2, $3, $7, 'S', $4, $5, "
        "$8, CASE WHEN $6 THEN clock_timestamp() END, $9, $10)",
        session_id,
        seq,
        "complete" if ended else "active",
        owner,
        key,
        ended,
        account,
        cli,
        cli_session_id,
        labels,
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
            partner=partner,
            api_key_id=_USER_KEY,
        )
        _ = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=2,
            cli=_HELPER,
            cli_session_id=chat_session_id(uuid.uuid4()),
            labels=[SCIENCE_CHAT_LABEL],
        )
        assert await assistant_key_may_use(
            conn,
            owner_id=_USER,
            partner=partner,
            api_key_id=_USER_KEY,
        )
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            partner=partner,
            api_key_id=_OTHER_KEY,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_key_needs_a_live_science_chat_the_owner_is_in(
    pglite_engine: PGliteEngine,
) -> None:
    """Talking to scout gives it the canvas of the people in a chat it has open."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        service = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        partner = await resolve_partner(
            conn,
            inbound=_polled(service),
            assistant=_KB,
            choice="shared",
            owner_id=_USER,
        )

        async def may(owner: uuid.UUID, *, key: uuid.UUID = _KB_KEY) -> bool:
            return await assistant_key_may_use(
                conn,
                owner_id=owner,
                partner=partner,
                api_key_id=key,
            )

        assert not await may(_USER)
        chat = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=2,
            cli_session_id=chat_session_id(uuid.uuid4()),
            labels=[SCIENCE_CHAT_LABEL],
        )
        # The starter is the chat's account; nobody else is in it yet.
        assert await may(_USER)
        assert not await may(_OTHER)
        await conn.execute(
            "UPDATE inquiries SET labels = labels || $2::text[] WHERE id = $1",
            chat,
            [poster_label("other@example.com")],
        )
        assert await may(_OTHER)
        assert not await may(_USER, key=_OTHER_KEY)
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            partner=None,
            api_key_id=_KB_KEY,
        )
        await conn.execute(
            "UPDATE inquiries SET status = 'complete', agentsession_ended = now() "
            "WHERE id = $1",
            chat,
        )
        assert not await may(_USER)
        assert not await may(_OTHER)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_that_is_not_science_gives_the_assistant_no_canvas(
    pglite_engine: PGliteEngine,
) -> None:
    """The label, not the session id, makes a session a science chat."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        service = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        _ = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=2,
            cli_session_id=chat_session_id(uuid.uuid4()),
        )
        partner = await resolve_partner(
            conn,
            inbound=_polled(service),
            assistant=_KB,
            choice="shared",
            owner_id=_USER,
        )
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            partner=partner,
            api_key_id=_KB_KEY,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_conversations_live_session_is_the_assistants_polled_one(
    pglite_engine: PGliteEngine,
) -> None:
    """A squatter's session under the same id, or one nobody drains, is not it."""
    await _seed(pglite_engine)
    conversation = uuid.uuid4()
    cli_session_id = chat_session_id(conversation)
    async with pglite_engine.acquire() as conn:
        silent = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=1,
            cli_session_id=cli_session_id,
        )
        squatter = await _session(
            conn,
            key=_OTHER_KEY,
            owner="chat-2",
            seq=2,
            cli_session_id=cli_session_id,
        )
        ended = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-3",
            seq=3,
            cli_session_id=cli_session_id,
            ended=True,
        )

        async def found(inbound: InboundQueue) -> uuid.UUID | None:
            return await live_chat_session(
                conn,
                inbound=inbound,
                opener=_KB.email,
                conversation_id=conversation,
            )

        assert await found(_polled(squatter, ended)) is None
        assert await found(_polled()) is None
        assert await found(_polled(silent)) == silent
        assert (
            await live_chat_session(
                conn,
                inbound=_polled(silent),
                opener="other@example.com",
                conversation_id=conversation,
            )
            is None
        )
        assert (
            await live_chat_session(
                conn,
                inbound=_polled(silent),
                opener=_KB.email,
                conversation_id=uuid.uuid4(),
            )
            is None
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistant_holds_a_conversation_it_opened_live_or_closed(
    pglite_engine: PGliteEngine,
) -> None:
    await _seed(pglite_engine)
    held, elsewhere = uuid.uuid4(), uuid.uuid4()
    async with pglite_engine.acquire() as conn:
        _ = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=1,
            cli_session_id=chat_session_id(held),
            ended=True,
        )
        _ = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-2",
            seq=2,
            cli_session_id=chat_session_id(elsewhere),
        )

        assert await assistant_holds_chat(conn, assistant=_KB, conversation_id=held)
        # A helper's own session under the id is not the assistant's.
        assert not await assistant_holds_chat(
            conn,
            assistant=_KB,
            conversation_id=elsewhere,
        )
        assert not await assistant_holds_chat(
            conn,
            assistant=_KB,
            conversation_id=uuid.uuid4(),
        )
        assert not await assistant_holds_chat(
            conn,
            assistant=None,
            conversation_id=held,
        )


@pytest.mark.parametrize(
    ("cli_session_id", "email", "expected"),
    [
        ("chat:3d0e", "kb@example.com", True),
        ("chat:3d0e", "other@example.com", False),
        ("slack:C1:123", "kb@example.com", False),
        (None, "kb@example.com", False),
    ],
)
def test_a_chat_session_is_a_chat_id_opened_by_the_assistants_account(
    cli_session_id: str | None,
    email: str,
    expected: bool,
) -> None:
    assert is_chat_session(_KB, cli_session_id=cli_session_id, email=email) is expected
    assert not is_chat_session(None, cli_session_id="chat:3d0e", email=email)


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
