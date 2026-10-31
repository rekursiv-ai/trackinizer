"""The transcript test keeps and drops the right rows on a real database.

The unit tests pin the binding of each fragment. This runs them against ``inquiries``
to show that the parameter numbering, the array cast, the anti-join and the rules of
each organisation mode do what ``TABLE`` says, for a cast of readers that each take
one clause of the rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.audience import (
    NOBODY,
    SessionReader,
    caller_reader,
    hidden_exists_sql,
    reads_session,
    visible_sql,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_feed import FeedScope
from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from trackinizer.lib.postgres import PGliteEngine
    from trackinizer.server.config import ChatOrgs


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_ASSISTANT: Final = "scout@service.invalid"
_T0: Final = datetime(2026, 10, 1, tzinfo=UTC)
_HELD: Final = frozenset({"open", "private"})

# Who reads, as each signs in, with Google's hd claim: Ann starts the chats and owns
# the sessions; Olu is her colleague, and Lee her colleague who holds staff; Con signs
# in with a consumer account on her domain's address, which Google names no member of
# it; Sam, on a consumer domain, has the chats shared with him; Sid is a stranger, and
# Ada an admin of another domain; the assistant; and a service account the operator
# made staff.
_CAST: Final[Mapping[str, tuple[str, frozenset[str], bool, str | None]]] = {
    "ann": ("ann@lab.example", _HELD, False, "lab.example"),
    "olu": ("olu@lab.example", _HELD, False, "lab.example"),
    "lee": ("lee@lab.example", AUDIENCES, False, "lab.example"),
    "con": ("con@lab.example", _HELD, False, None),
    "sam": ("sam@gmail.com", _HELD, False, None),
    "sid": ("sid@else.example", _HELD, False, "else.example"),
    "ada": ("ada@else.example", _HELD, True, "else.example"),
    "scout": (_ASSISTANT, _HELD, False, None),
    "lead": ("lead@service.invalid", AUDIENCES, False, None),
}

_EVERYONE: Final = frozenset(_CAST)

# Who reads each session, by organisation mode. The ``s_`` sessions are Ann's but
# ``s_svc``, the service account's; the ``c_`` sessions are chats Ann began with the
# assistant and shared with Sam.
TABLE: Final[Mapping[str, Mapping[str, frozenset[str]]]] = {
    "single": {
        "s_open": _EVERYONE,
        "s_private": frozenset({"ann", "lee", "ada"}),
        "s_staff": frozenset({"lee", "lead"}),
        "s_svc": _EVERYONE,
        "c_open": _EVERYONE - {"lead"},
        "c_private": frozenset({"ann", "sam"}),
        "c_staff": frozenset({"lee"}),
    },
    "domain": {
        "s_open": frozenset({"ann", "olu", "lee", "ada"}),
        "s_private": frozenset({"ann", "lee", "ada"}),
        "s_staff": frozenset({"lee", "lead"}),
        "s_svc": frozenset({"lead", "lee", "ada"}),
        "c_open": frozenset({"ann", "olu", "lee", "sam"}),
        "c_private": frozenset({"ann", "sam"}),
        "c_staff": frozenset({"lee"}),
    },
    "individual": {
        "s_open": frozenset({"ann", "lee", "ada"}),
        "s_private": frozenset({"ann", "lee", "ada"}),
        "s_staff": frozenset({"lee", "lead"}),
        "s_svc": frozenset({"lead", "lee", "ada"}),
        "c_open": frozenset({"ann", "sam"}),
        "c_private": frozenset({"ann", "sam"}),
        "c_staff": frozenset[str](),
    },
}


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


# The by-id read tests one session at a time, so a run over all nine readers makes
# ~60 round trips and no longer fits a unit test's budget. The readers split into
# groups that each carry a share, keeping every reader's row of the table checked by
# every path; the groups partition ``_CAST`` exactly.
_READER_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    ("ann", "olu", "lee"),
    ("con", "sam", "sid"),
    ("ada", "scout", "lead"),
)
assert {name for group in _READER_GROUPS for name in group} == _EVERYONE


@pytest.mark.parametrize("orgs", ["single", "domain", "individual"])
@pytest.mark.parametrize(
    "who",
    _READER_GROUPS,
    ids=["-".join(group) for group in _READER_GROUPS],
)
async def test_the_visibility_table_holds(
    store: Store,
    orgs: ChatOrgs,
    who: tuple[str, ...],
) -> None:
    """Every reader reads exactly its row of the table, many rows at once or by id."""
    sessions = await _seed_cast(store)
    ids = list(sessions.values())
    readers = {name: _reader(name, orgs=orgs) for name in who}
    named = {session: name for name, session in sessions.items()}

    listed = {
        name: {named[each] for each in await _visible(store, reader, ids)}
        for name, reader in readers.items()
    }
    unhidden = {
        name: {named[each] for each in await _not_hidden(store, reader, ids)}
        for name, reader in readers.items()
    }
    async with store.engine.acquire() as conn:
        by_id = {
            name: {
                session_name
                for session_name, session in sessions.items()
                if await reads_session(conn, reader=reader, session_id=session)
            }
            for name, reader in readers.items()
        }

    expected = {
        name: {
            session for session, who_reads in TABLE[orgs].items() if name in who_reads
        }
        for name in who
    }
    assert listed == expected
    assert unhidden == expected
    assert by_id == expected


async def test_a_store_read_that_names_no_reader_reads_nothing(store: Store) -> None:
    sessions = await _seed_cast(store)
    await _record(store, session=sessions["s_open"])
    ids = list(sessions.values())
    every = SessionReader(
        audiences=AUDIENCES,
        every_session=True,
        every_chat=True,
        oversight=True,
    )

    assert await _visible(store, NOBODY, ids) == set()
    assert await _not_hidden(store, NOBODY, ids) == set()
    assert await store.read_feed() == []
    assert len(await store.read_feed(scope=FeedScope(reader=every))) == 1


async def test_hidden_false_drops_private_from_listings_not_reads(
    store: Store,
) -> None:
    """A listing asks for no private session, chat or not; a read by id keeps them."""
    sessions = await _seed_cast(store)
    ann = _reader("ann", orgs="single")
    listing = _reader("ann", orgs="single", hidden=False)
    ids = list(sessions.values())

    async with store.engine.acquire() as conn:
        read = await reads_session(conn, reader=ann, session_id=sessions["s_private"])
    kept = await _visible(store, ann, ids)
    listed = await _visible(store, listing, ids)

    assert read is True
    assert kept - listed == {sessions["s_private"], sessions["c_private"]}


async def test_visible_keeps_the_sessions_of_an_audience_read(store: Store) -> None:
    open_id = await _session(store, audience="open")
    staff_id = await _session(store, audience="staff")
    partner_id = await _session(store, audience="partner")
    ids = [open_id, staff_id, partner_id]

    assert await _visible(store, _reads("open"), ids) == {open_id}
    assert await _visible(store, _reads("open", "staff"), ids) == {open_id, staff_id}
    assert await _visible(store, _reads(), ids) == set()


async def test_hidden_drops_the_rows_of_a_session_not_read_and_keeps_the_rest(
    store: Store,
) -> None:
    open_id = await _session(store, audience="open")
    staff_id = await _session(store, audience="staff")
    no_session = uuid4()
    ids = [open_id, staff_id, no_session]

    assert await _not_hidden(store, _reads("open"), ids) == {open_id, no_session}
    assert await _not_hidden(store, _reads("open", "staff"), ids) == set(ids)
    assert await _not_hidden(store, _reads(), ids) == {no_session}


async def test_hidden_drops_a_session_that_lost_its_audience(store: Store) -> None:
    """A NULL audience is hidden, as ``require_session_access`` hides it.

    The column's CHECK makes a NULL unreachable, so the test stands up a bare
    ``inquiries`` in a scratch schema ahead of ``public`` on the search path, which
    the helper's unqualified name finds first, the chat rule's included. It is not a
    temp table: dropping the session's temp schema leaves later temp tables beyond
    ``reset_schema``'s reach.
    """
    lost = uuid4()
    params: list[object] = [lost]
    clause = hidden_exists_sql("$1::uuid", params=params, reader=_reads("open"))
    async with store.engine.acquire() as conn:
        await conn.execute("CREATE SCHEMA audience_probe")
        await conn.execute(
            "CREATE TABLE audience_probe.inquiries (id uuid, kind text, "
            "account text, created timestamptz, agentsession_audience text, "
            "agentsession_cli_session_id text, "
            "agentsession_opened_by_api_key_id uuid)",
        )
        await conn.execute("SET search_path TO audience_probe, public")
        try:
            await conn.execute(
                "INSERT INTO inquiries (id, kind) VALUES ($1, 'AgentSession')",
                lost,
            )
            kept = await conn.fetchval(vetted_sql("SELECT ", clause), *params)
        finally:
            await conn.execute("RESET search_path")
            await conn.execute("DROP SCHEMA audience_probe CASCADE")

    assert kept is False


async def test_a_chat_admits_its_first_sessions_account_not_a_later_ones(
    store: Store,
) -> None:
    """A rotated assistant key opens the chat's next session in its first poster's name."""
    chat = await _seed_chat(store)
    later = await _session(
        store,
        cli=chat.cli,
        account="pat@else.example",
        key=chat.keys["rotated"],
        created=_T0 + timedelta(hours=1),
    )
    ids = [chat.session, later]

    assert await _visible(store, _named("pat@else.example"), ids) == set()
    assert await _visible(store, _named("ann@lab.example"), ids) == {
        chat.session,
        later,
    }


async def test_a_squat_under_a_chats_id_makes_no_one_its_starter(store: Store) -> None:
    chat = await _seed_chat(store)
    # Older than the chat and under its id, by Sid's key in Jan's name: no beginning.
    decoy = await _session(
        store,
        cli=chat.cli,
        account="jan@else.example",
        key=chat.keys["sid"],
        created=_T0 - timedelta(hours=1),
        session_id=UUID(int=1),
    )
    # Sid's own, in Sid's name: a beginning, but after the chat's first, though first
    # by id.
    squat = await _session(
        store,
        cli=chat.cli,
        account="sid@else.example",
        key=chat.keys["sid"],
        created=_T0 + timedelta(hours=1),
        session_id=UUID(int=2),
    )
    ids = [chat.session, decoy, squat]

    assert await _visible(store, _named("sid@else.example"), ids) == set()
    assert await _visible(store, _named("jan@else.example"), ids) == set()
    assert await _visible(store, _named("ann@lab.example"), ids) == set(ids)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Chat:
    """A science chat Ann began with the assistant, shared with Sam."""

    conversation: UUID
    session: UUID
    keys: Mapping[str, UUID]

    @property
    def cli(self) -> str:
        """The chat's ``cli_session_id``."""
        return f"chat:{self.conversation}"


async def _seed_cast(store: Store) -> dict[str, UUID]:
    """Seed every session of ``TABLE``, by name."""
    sessions = {
        name: await _session(store, audience=audience, account="Ann@Lab.example")
        for name, audience in (
            ("s_open", "open"),
            ("s_private", "private"),
            ("s_staff", "staff"),
        )
    }
    sessions["s_svc"] = await _session(store, account="lead@service.invalid")
    for name, audience in (
        ("c_open", "open"),
        ("c_private", "private"),
        ("c_staff", "staff"),
    ):
        sessions[name] = (await _seed_chat(store, audience=audience)).session
    return sessions


async def _seed_chat(store: Store, *, audience: str = "open") -> _Chat:
    """Seed the keys, the chat's first session, last of its id's by id, and a share."""
    keys = {
        "scout": await _key(store, _ASSISTANT),
        "rotated": await _key(store, _ASSISTANT),
        "sid": await _key(store, "sid@else.example"),
    }
    conversation = uuid4()
    session = await _session(
        store,
        audience=audience,
        cli=f"chat:{conversation}",
        account="Ann@Lab.Example",
        key=keys["scout"],
        created=_T0,
        session_id=UUID(int=2**128 - 1 - conversation.int % 2**32),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
            "VALUES ($1, 'sam@gmail.com', 'ann@lab.example')",
            conversation,
        )
    return _Chat(conversation=conversation, session=session, keys=keys)


async def _key(store: Store, email: str) -> UUID:
    """Mint a key for ``email``, making the user on its first key."""
    key = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Test', 'writer', 'active') ON CONFLICT (email) DO NOTHING",
            uuid4(),
            email,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "SELECT $1, id, 'agent', 'test-hash', 'trax_test', 'writer' "
            "FROM users WHERE email = $2",
            key,
            email,
        )
    return key


async def _session(
    store: Store,
    *,
    audience: str = "open",
    cli: str | None = None,
    account: str = "tester@example.com",
    key: UUID | None = None,
    created: datetime = _T0,
    session_id: UUID | None = None,
) -> UUID:
    """Insert a session and return its id."""
    session_id = session_id or uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience, agentsession_cli_session_id, "
            "agentsession_opened_by_api_key_id, created) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, 'a session', $3, $4, $5, $6)",
            session_id,
            account,
            audience,
            cli,
            key,
            created,
        )
    return session_id


async def _record(store: Store, *, session: UUID) -> None:
    """Give ``session`` one record of one part, which a feed read returns."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 'one.jsonl', '{}'::json, $2, 'claude', 1)",
            session,
            uuid4(),
        )
        await conn.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, 0, 'UserMessage', '{\"content\": \"hi\"}'::json, 'hi')",
            session,
        )


def _reader(name: str, *, orgs: ChatOrgs, hidden: bool = True) -> SessionReader:
    """Return the reader ``name`` of the cast signs in as, on a server in ``orgs``."""
    email, audiences, admin, hosted_domain = _CAST[name]
    return caller_reader(
        email,
        audiences=audiences,
        admin=admin,
        orgs=orgs,
        assistant=_ASSISTANT,
        hosted_domain=hosted_domain,
        hidden=hidden,
    )


async def _visible(
    store: Store,
    reader: SessionReader,
    ids: Sequence[UUID],
) -> set[UUID]:
    """Return which sessions of ``ids`` :func:`visible_sql` keeps for ``reader``."""
    params: list[object] = [list(ids)]
    clause = visible_sql("i", params=params, reader=reader)
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            vetted_sql(
                "SELECT i.id FROM inquiries AS i WHERE i.id = ANY($1::uuid[]) AND ",
                clause,
            ),
            *params,
        )
    return {from_plain(row["id"], UUID) for row in rows}


async def _not_hidden(
    store: Store,
    reader: SessionReader,
    ids: Sequence[UUID],
) -> set[UUID]:
    """Return which rows naming ``ids`` :func:`hidden_exists_sql` keeps for ``reader``."""
    params: list[object] = [list(ids)]
    clause = hidden_exists_sql("t.session_id", params=params, reader=reader)
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            vetted_sql(
                "SELECT t.session_id FROM unnest($1::uuid[]) AS t(session_id) WHERE ",
                clause,
            ),
            *params,
        )
    return {from_plain(row["session_id"], UUID) for row in rows}


def _reads(*audiences: str) -> SessionReader:
    """Return a reader of every session of ``audiences`` and of no chat."""
    return SessionReader(audiences=frozenset(audiences), every_session=True)


def _named(email: str) -> SessionReader:
    """Return a reader of ``open`` and ``private`` known by ``email`` alone."""
    return SessionReader(audiences=_HELD, email=email, assistant=_ASSISTANT)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
