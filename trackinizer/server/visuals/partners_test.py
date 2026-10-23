"""A canvas's partner: the assistant's session by account and name, or the CLI's run."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import json
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.asks_test_support import open_ask, resolve_ask
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue, PollerRun
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.partners import (
    assistant_key_may_use,
    attach_partner,
    chat_run_states,
    is_assistant_session,
    is_chat_session,
    live_chat_session,
    live_run_states,
    opener_email,
    provider_availability,
    resolve_partner,
)
from trackinizer.server.visuals.workspaces import NO_MACHINE_REASON, WorkspaceState
from trackinizer.wire.wire_science_chat import (
    SCIENCE_CHAT_LABEL,
    chat_session_id,
    poster_label,
)


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, PGliteEngine
    from trackinizer.wire.wire_science_chat import ChatProvider


_KB: Final = Assistant(actor="scout", email="kb@example.com")
_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_KB_USER: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_USER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444441")
_OTHER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444442")
_KB_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444443")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_CHAT: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")
_OTHER_CHAT: Final = uuid.UUID("66666666-6666-6666-6666-666666666667")


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
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            'VALUES ($1, $2, \'{"name": "unnamed-workstream#1"}\', $3)',
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
        "agentsession_cli_session_id, labels, agentsession_audience) "
        "VALUES ($1, 'AgentSession', $2, $3, $7, 'S', $4, $5, "
        "$8, CASE WHEN $6 THEN clock_timestamp() END, $9, $10, 'open')",
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


def _polled(*session_ids: uuid.UUID, cli: str | None = None) -> InboundQueue:
    inbound = InboundQueue()
    for session_id in session_ids:
        inbound.mark_poller(session_id, cli=cli)
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
            provider="assistant",
            conversation_id=None,
        )
    assert partner is not None
    assert partner.model_dump() == {
        "session_id": newest,
        "actor": "scout",
        "cli": "codex",
        "status": "live",
        "provider": "assistant",
        "state": "idle",
        "machine": None,
        "run_name": None,
        "cwd": None,
        "label": "scout",
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
            provider="assistant",
            conversation_id=None,
        )
        none = await resolve_partner(
            conn,
            inbound=_polled(silent),
            assistant=None,
            provider="assistant",
            conversation_id=None,
        )
    assert partner is not None
    assert partner.model_dump() == {
        "session_id": None,
        "actor": "scout",
        "cli": None,
        "status": "unavailable",
        "provider": "assistant",
        "state": "idle",
        "machine": None,
        "run_name": None,
        "cwd": None,
        "label": "scout",
    }
    assert none is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_revoking_the_assistants_key_takes_its_session_out_of_chat(
    pglite_engine: PGliteEngine,
) -> None:
    """A session a revoked key opened answers no Chat, polled or not."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        service = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            _KB_KEY,
        )
        partner = await resolve_partner(
            conn,
            inbound=_polled(service),
            assistant=_KB,
            provider="assistant",
            conversation_id=None,
        )
    assert partner is not None
    assert partner.status == "unavailable"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_attach_names_the_assistant_and_the_partner(
    pglite_engine: PGliteEngine,
) -> None:
    """A canvas state carries both, computed, for every reader."""
    await _seed(pglite_engine)
    state = WorkspaceState(
        id=_WORKSPACE,
        revision=0,
        name="unnamed-workstream#1",
        conversation_id=None,
        page=None,
        visuals=[],
    )
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
            provider="assistant",
            conversation_id=None,
        )

        async def may(owner: uuid.UUID, *, key: uuid.UUID = _KB_KEY) -> bool:
            return await assistant_key_may_use(
                conn,
                owner_id=owner,
                partner=partner,
                conversation_id=_CHAT,
                api_key_id=key,
            )

        assert not await may(_USER)
        chat = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=2,
            cli_session_id=chat_session_id(_CHAT),
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
            conversation_id=_CHAT,
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
async def test_a_station_takes_the_key_only_of_the_chat_it_holds(
    pglite_engine: PGliteEngine,
) -> None:
    """No chat, or another conversation's chat, gives a key nothing of the station."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        service = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        _ = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=2,
            cli_session_id=chat_session_id(_CHAT),
            labels=[SCIENCE_CHAT_LABEL],
        )
        partner = await resolve_partner(
            conn,
            inbound=_polled(service),
            assistant=_KB,
            provider="assistant",
            conversation_id=None,
        )

        async def may(conversation_id: uuid.UUID | None) -> bool:
            return await assistant_key_may_use(
                conn,
                owner_id=_USER,
                partner=partner,
                conversation_id=conversation_id,
                api_key_id=_KB_KEY,
            )

        assert await may(_CHAT)
        assert not await may(None)
        assert not await may(_OTHER_CHAT)


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
            cli_session_id=chat_session_id(_CHAT),
        )
        partner = await resolve_partner(
            conn,
            inbound=_polled(service),
            assistant=_KB,
            provider="assistant",
            conversation_id=None,
        )
        assert not await assistant_key_may_use(
            conn,
            owner_id=_USER,
            partner=partner,
            conversation_id=_CHAT,
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


async def _part(conn: Conn, *, session_id: uuid.UUID, part: int, cli: str) -> None:
    """Record that a run of ``cli`` wrote part ``part`` of a session."""
    await conn.execute(
        "INSERT INTO session_manifests "
        "(session_id, part, name, metadata, ir_id, format, records) "
        "VALUES ($1, $2, $3, '{}', $4, $5, 0)",
        session_id,
        part,
        f"{cli}-{part}.jsonl",
        uuid.uuid4(),
        cli,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cli_partner_is_the_chats_session_a_run_of_that_cli_drains(
    pglite_engine: PGliteEngine,
) -> None:
    """Live while a run of that CLI drains it; offline still names the session.

    Which CLI drains it is what the drain named, never what the newest part says: a
    run writes no part of its own, and ``/compact`` leaves a part with no format.
    """
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        chat = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=1,
            cli="claude",
            cli_session_id=chat_session_id(_CHAT),
        )
        squatter = await _session(
            conn,
            key=_OTHER_KEY,
            owner="chat-2",
            seq=2,
            cli_session_id=chat_session_id(_CHAT),
            account="other@example.com",
        )

        async def partner(
            *polled: uuid.UUID,
            provider: ChatProvider = "claude",
            drained_by: str | None = "claude",
            conversation_id: uuid.UUID | None = _CHAT,
        ) -> tuple[uuid.UUID | None, str, str]:
            found = await resolve_partner(
                conn,
                inbound=_polled(*polled, cli=drained_by),
                assistant=_KB,
                provider=provider,
                conversation_id=conversation_id,
            )
            assert found is not None
            assert (found.provider, found.cli, found.machine) == (
                provider,
                provider,
                None,
            )
            return found.session_id, found.status, found.label

        assert await partner() == (chat, "offline", "claude: no run")
        assert await partner(chat) == (chat, "live", "claude")
        assert await partner(squatter) == (chat, "offline", "claude: no run")
        assert await partner(chat, conversation_id=None) == (
            None,
            "offline",
            "claude: no run",
        )
        assert await partner(chat, conversation_id=_OTHER_CHAT) == (
            None,
            "offline",
            "claude: no run",
        )
        # The run that drains the chat is the one its drain names, whatever parts the
        # chat holds: the codex part and the compact part (no format) are both newest.
        await _part(conn, session_id=chat, part=0, cli="sagent")
        assert await partner(chat) == (chat, "live", "claude")
        await _part(conn, session_id=chat, part=1, cli="codex")
        assert await partner(chat) == (chat, "live", "claude")
        assert await partner(chat, provider="codex") == (
            chat,
            "offline",
            "codex: no run",
        )
        await _part(conn, session_id=chat, part=2, cli="")
        assert await partner(chat) == (chat, "live", "claude")
        assert await partner(chat, drained_by="codex") == (
            chat,
            "offline",
            "claude: no run",
        )
        assert await partner(chat, provider="codex", drained_by="codex") == (
            chat,
            "live",
            "codex",
        )
        # A drain that names no CLI is an older trax's, which no provider claims.
        assert await partner(chat, drained_by=None) == (
            chat,
            "offline",
            "claude: no run",
        )
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            _USER_KEY,
        )
        assert await partner(chat) == (chat, "offline", "claude: no run")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_live_runs_partner_says_where_it_runs_and_how_to_attach(
    pglite_engine: PGliteEngine,
) -> None:
    """What the run's drain said of itself shows while its lease holds, and no longer."""
    await _seed(pglite_engine)
    inbound = InboundQueue()
    async with pglite_engine.acquire() as conn:
        chat = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=1,
            cli="claude",
            cli_session_id=chat_session_id(_CHAT),
        )

        async def partner(
            where: PollerRun | None,
        ) -> tuple[str, str | None, str | None, str | None, str]:
            if where is None:
                inbound.forget_poller(chat)
            else:
                inbound.mark_poller(
                    chat,
                    run=where.run,
                    machine=where.machine,
                    cwd=where.cwd,
                    cli="claude",
                )
            found = await resolve_partner(
                conn,
                inbound=inbound,
                assistant=_KB,
                provider="claude",
                conversation_id=_CHAT,
            )
            assert found is not None
            return (found.status, found.machine, found.run_name, found.cwd, found.label)

        detached = PollerRun(run="chat-1a2b3c4d", machine="gx10", cwd="~/work")
        assert await partner(detached) == (
            "live",
            "gx10",
            "chat-1a2b3c4d",
            "~/work",
            "claude · gx10",
        )
        assert await partner(PollerRun(machine="gx10", cwd="/srv")) == (
            "live",
            "gx10",
            None,
            "/srv",
            "claude · gx10",
        )
        # A run from a trax that names nothing is shown as runs always were.
        assert await partner(PollerRun()) == ("live", None, None, None, "claude")
        assert await partner(None) == ("offline", None, None, None, "claude: no run")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_live_runs_partner_is_the_one_chats_info_reads(
    pglite_engine: PGliteEngine,
) -> None:
    """The partner of a detached run, as ``web/test/seams/livePartner.json`` holds it.

    Chat's Info dialog renders that file into the command that attaches to the run, so a
    change of shape on either side fails both.
    """
    await _seed(pglite_engine)
    inbound = InboundQueue()
    async with pglite_engine.acquire() as conn:
        chat = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=1,
            cli="claude",
            cli_session_id=chat_session_id(_CHAT),
        )
        inbound.mark_poller(
            chat,
            run="chat-1a2b3c4d",
            machine="gx10",
            cwd="~/project",
            cli="claude",
        )
        found = await resolve_partner(
            conn,
            inbound=inbound,
            assistant=_KB,
            provider="claude",
            conversation_id=_CHAT,
        )

    assert found is not None
    shown = found.model_copy(
        update={"session_id": uuid.UUID("00000000-0000-4000-8000-000000000001")},
    )
    seam = _CWD.parents[1] / "web" / "test" / "seams" / "livePartner.json"
    assert json_loads(json.dumps(shown.model_dump(mode="json"))) == json_loads(
        seam.read_text(encoding="utf-8"),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_live_runs_partner_says_what_its_run_is_doing(
    pglite_engine: PGliteEngine,
) -> None:
    """The state a run reported shows while its lease holds; a run gone shows idle."""
    await _seed(pglite_engine)
    inbound = InboundQueue()
    run = "a" * 32
    async with pglite_engine.acquire() as conn:
        chat = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=1,
            cli="claude",
            cli_session_id=chat_session_id(_CHAT),
        )

        async def state() -> str | None:
            found = await resolve_partner(
                conn,
                inbound=inbound,
                assistant=_KB,
                provider="claude",
                conversation_id=_CHAT,
            )
            assert found is not None
            return found.state

        assert await state() == "idle"
        inbound.mark_poller(chat, cli="claude", instance=run)
        assert await state() == "idle"
        for reported in ("running", "requires_action", "taken_over", "idle"):
            assert inbound.report_state(chat, instance=run, state=reported)
            assert await state() == reported
        inbound.forget_poller(chat)
        assert await state() == "idle"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ask_open_in_the_chat_requires_action_whatever_the_run_says(
    pglite_engine: PGliteEngine,
) -> None:
    """A run that reports nothing is still waiting on an open ask; a takeover is not."""
    store = await _seed(pglite_engine)
    inbound = InboundQueue()
    run = "a" * 32
    async with pglite_engine.acquire() as conn:
        chat = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=1,
            cli="claude",
            cli_session_id=chat_session_id(_CHAT),
        )
    inbound.mark_poller(chat, cli="claude", instance=run)

    async def state() -> str | None:
        async with pglite_engine.acquire() as conn:
            found = await resolve_partner(
                conn,
                inbound=inbound,
                assistant=_KB,
                provider="claude",
                conversation_id=_CHAT,
            )
        assert found is not None
        return found.state

    await open_ask(store, chat, ask_id="a1")
    assert await state() == "requires_action"
    assert inbound.report_state(chat, instance=run, state="running")
    assert await state() == "requires_action"
    assert inbound.report_state(chat, instance=run, state="taken_over")
    assert await state() == "taken_over"
    assert inbound.report_state(chat, instance=run, state="idle")
    await resolve_ask(store, chat, ask_id="a1")
    assert await state() == "idle"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ask_open_in_the_assistants_chat_requires_action(
    pglite_engine: PGliteEngine,
) -> None:
    """The assistant reports no state, so its chat's open ask is the whole of it."""
    store = await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        chat = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-1",
            seq=1,
            cli_session_id=chat_session_id(_CHAT),
            account="user@example.com",
        )
        service = await _session(conn, key=_KB_KEY, owner="scout", seq=2)
    inbound = _polled(chat, service)

    async def seen(conversation: uuid.UUID | None) -> tuple[str, str | None]:
        async with pglite_engine.acquire() as conn:
            found = await resolve_partner(
                conn,
                inbound=inbound,
                assistant=_KB,
                provider="assistant",
                conversation_id=conversation,
            )
        assert found is not None
        return found.status, found.state

    assert await seen(_CHAT) == ("live", "idle")
    await open_ask(store, chat, ask_id="q1")
    assert await seen(_CHAT) == ("live", "requires_action")
    assert await seen(_OTHER_CHAT) == ("live", "idle")
    assert await seen(None) == ("live", "idle")
    await resolve_ask(store, chat, ask_id="q1")
    assert await seen(_CHAT) == ("live", "idle")
    await open_ask(store, chat, ask_id="q2")
    inbound.forget_poller(chat)
    assert await seen(_CHAT) == ("live", "idle")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_states_of_some_live_runs_are_read_together(
    pglite_engine: PGliteEngine,
) -> None:
    """A session no run polls has no state; the rest say what they do, or what they wait on."""
    store = await _seed(pglite_engine)
    inbound = InboundQueue()
    run = "a" * 32
    async with pglite_engine.acquire() as conn:
        running, taken, waiting, gone = [
            await _session(
                conn,
                key=_USER_KEY,
                owner=f"run-{seq}",
                seq=seq,
                cli="claude",
            )
            for seq in range(1, 5)
        ]
    for session in (running, taken, waiting):
        inbound.mark_poller(session, cli="claude", instance=run)
    assert inbound.report_state(running, instance=run, state="running")
    assert inbound.report_state(taken, instance=run, state="taken_over")
    for session in (taken, waiting, gone):
        await open_ask(store, session, ask_id="a1")

    async with pglite_engine.acquire() as conn:
        states = await live_run_states(
            conn,
            inbound=inbound,
            session_ids=[running, taken, waiting, gone],
        )
        nothing = await live_run_states(conn, inbound=inbound, session_ids=[])

    assert states == {
        running: "running",
        taken: "taken_over",
        waiting: "requires_action",
    }
    assert nothing == {}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_state_of_each_chat_is_the_one_its_live_runs_agree_on(
    pglite_engine: PGliteEngine,
) -> None:
    """A chat's runs are the sessions its station owner or the assistant opened."""
    store = await _seed(pglite_engine)
    inbound = InboundQueue()
    run = "a" * 32
    mine, asked, stranger_chat, idle_chat = (uuid.uuid4() for _ in range(4))
    async with pglite_engine.acquire() as conn:
        cli = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-1",
            seq=1,
            cli="claude",
            cli_session_id=chat_session_id(mine),
        )
        card = await _session(
            conn,
            key=_KB_KEY,
            owner="chat-2",
            seq=2,
            cli_session_id=chat_session_id(asked),
        )
        squatter = await _session(
            conn,
            key=_OTHER_KEY,
            owner="chat-3",
            seq=3,
            cli_session_id=chat_session_id(stranger_chat),
        )
        quiet = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-4",
            seq=4,
            cli="claude",
            cli_session_id=chat_session_id(idle_chat),
        )
        silent = await _session(
            conn,
            key=_USER_KEY,
            owner="chat-5",
            seq=5,
            cli="claude",
            cli_session_id=chat_session_id(uuid.uuid4()),
        )
    for session in (cli, card, squatter, quiet):
        inbound.mark_poller(session, cli="claude", instance=run)
    assert inbound.report_state(cli, instance=run, state="taken_over")
    await open_ask(store, card, ask_id="q1")
    await open_ask(store, squatter, ask_id="q1")
    await open_ask(store, silent, ask_id="q1")

    async with pglite_engine.acquire() as conn:
        states = await chat_run_states(
            conn,
            inbound=inbound,
            openers=["USER@example.com", _KB.email],
            conversation_ids=[mine, asked, stranger_chat, idle_chat, uuid.uuid4()],
        )

    assert states == {mine: "taken_over", asked: "requires_action", idle_chat: "idle"}


async def _machine(
    conn: Conn,
    *,
    name: str,
    owner: str = "user@example.com",
    clis: list[str] | str = "",
    confirmed: bool = True,
    online: bool = True,
) -> uuid.UUID:
    """Register a machine as the machine host leaves it."""
    return from_plain(
        await conn.fetchval(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ($1, $2, $2, "
            "CASE WHEN $3 THEN now() END, gen_random_uuid(), "
            "now() - make_interval(secs => CASE WHEN $4 THEN 0 ELSE 3600 END), "
            "jsonb_build_object('clis', $5::jsonb)) RETURNING id",
            name,
            owner,
            confirmed,
            online,
            clis,
        ),
        uuid.UUID,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cli_is_available_on_a_confirmed_online_machine_that_lists_it(
    pglite_engine: PGliteEngine,
) -> None:
    """The machines the terminal picker offers; the assistant is always available."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:

        async def available() -> tuple[bool, bool, bool, str, str, str]:
            flags, reasons = await provider_availability(conn, owner_id=_USER)
            return (
                flags.assistant,
                flags.claude,
                flags.codex,
                reasons.assistant,
                reasons.claude,
                reasons.codex,
            )

        none = (
            True,
            False,
            False,
            "",
            NO_MACHINE_REASON.format(cli="claude"),
            NO_MACHINE_REASON.format(cli="codex"),
        )
        assert await available() == none
        _ = await _machine(conn, name="cold", clis=["claude"], online=False)
        _ = await _machine(conn, name="new", clis=["claude"], confirmed=False)
        _ = await _machine(conn, name="theirs", clis=["claude"], owner="other@x.com")
        _ = await _machine(conn, name="odd", clis="claude")
        assert await available() == none
        laptop = await _machine(conn, name="laptop", clis=["claude", "git"])
        assert await available() == (
            True,
            True,
            False,
            "",
            "",
            NO_MACHINE_REASON.format(cli="codex"),
        )
        await conn.execute(
            "INSERT INTO machine_credentials (machine_id, secret_sha256, revoked_at) "
            "VALUES ($1, sha256('x'::bytea), now())",
            laptop,
        )
        assert await available() == none
        await conn.execute(
            "INSERT INTO machine_credentials (machine_id, secret_sha256) "
            "VALUES ($1, sha256('y'::bytea))",
            laptop,
        )
        assert (await available())[1]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_attach_names_the_stations_cli_partner_and_what_its_owner_may_choose(
    pglite_engine: PGliteEngine,
) -> None:
    """A claude station without a run is offline, whatever the assistant is doing."""
    await _seed(pglite_engine)
    state = WorkspaceState(
        id=_WORKSPACE,
        revision=0,
        name="unnamed-workstream#1",
        conversation_id=_CHAT,
        page=None,
        visuals=[],
        provider="claude",
    )
    async with pglite_engine.acquire() as conn:
        service = await _session(conn, key=_KB_KEY, owner="scout", seq=1)
        _ = await _machine(conn, name="laptop", clis=["codex"])
        attached = await attach_partner(
            conn,
            state=state,
            inbound=_polled(service),
            assistant=_KB,
            owner_id=_USER,
        )
    assert attached.partner is not None
    assert (attached.partner.status, attached.partner.label) == (
        "offline",
        "claude: no run",
    )
    assert (attached.available.claude, attached.available.codex) == (False, True)
    assert attached.reasons.claude == NO_MACHINE_REASON.format(cli="claude")


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
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
