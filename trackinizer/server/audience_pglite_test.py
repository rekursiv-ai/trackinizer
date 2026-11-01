"""The transcript test keeps and drops the right rows on a real database.

The unit tests pin the binding of each fragment. This runs them against ``inquiries``
to show that the parameter numbering, the array cast, the anti-join and the rules of
each organisation mode do what ``TABLE`` says, for a cast of readers that each take
one clause of the rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from treekle import from_plain

import pytest

from trackinizer.server.audience import (
    NOBODY,
    SessionReader,
    caller_reader,
    hidden_exists_sql,
    reads_session,
    visible_sql,
)
from trackinizer.server.store.session_feed import FeedScope
from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.server.config import ChatOrgs
    from trackinizer.server.store.core import Store


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

ASSISTANT: Final = "scout@service.invalid"
T0: Final = datetime(2026, 10, 1, tzinfo=UTC)
HELD: Final = frozenset({"open", "private"})

# Who reads, as each signs in, with Google's hd claim: Ann owns the sessions; Olu is
# her colleague, and Lee her colleague who holds staff; Con signs in with a consumer
# account on her domain's address, which Google names no member of it; Sam is on a
# consumer domain; Sid is a stranger, and Ada an admin of another domain; the
# assistant; and a service account the operator made staff.
CAST: Final[Mapping[str, tuple[str, frozenset[str], bool, str | None]]] = {
    "ann": ("ann@lab.example", HELD, False, "lab.example"),
    "olu": ("olu@lab.example", HELD, False, "lab.example"),
    "lee": ("lee@lab.example", AUDIENCES, False, "lab.example"),
    "con": ("con@lab.example", HELD, False, None),
    "sam": ("sam@gmail.com", HELD, False, None),
    "sid": ("sid@else.example", HELD, False, "else.example"),
    "ada": ("ada@else.example", HELD, True, "else.example"),
    "scout": (ASSISTANT, HELD, False, None),
    "lead": ("lead@service.invalid", AUDIENCES, False, None),
}

EVERYONE: Final = frozenset(CAST)

# Who reads each session, by organisation mode. The sessions are Ann's but ``s_svc``,
# the service account's.
TABLE: Final[Mapping[str, Mapping[str, frozenset[str]]]] = {
    "single": {
        "s_open": EVERYONE,
        "s_private": frozenset({"ann", "lee", "ada"}),
        "s_staff": frozenset({"lee", "lead"}),
        "s_svc": EVERYONE,
    },
    "domain": {
        "s_open": frozenset({"ann", "olu", "lee", "ada"}),
        "s_private": frozenset({"ann", "lee", "ada"}),
        "s_staff": frozenset({"lee", "lead"}),
        "s_svc": frozenset({"lead", "lee", "ada"}),
    },
    "individual": {
        "s_open": frozenset({"ann", "lee", "ada"}),
        "s_private": frozenset({"ann", "lee", "ada"}),
        "s_staff": frozenset({"lee", "lead"}),
        "s_svc": frozenset({"lead", "lee", "ada"}),
    },
}


# The by-id read tests one session at a time, so a run over all nine readers makes
# ~60 round trips and no longer fits a unit test's budget. The readers split into
# groups that each carry a share, keeping every reader's row of the table checked by
# every path; the groups partition ``CAST`` exactly.
READER_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    ("ann", "olu", "lee"),
    ("con", "sam", "sid"),
    ("ada", "scout", "lead"),
)
assert {name for group in READER_GROUPS for name in group} == EVERYONE


@pytest.mark.parametrize("orgs", ["single", "domain", "individual"])
@pytest.mark.parametrize(
    "who",
    READER_GROUPS,
    ids=["-".join(group) for group in READER_GROUPS],
)
async def test_the_visibility_table_holds(
    store: Store,
    orgs: ChatOrgs,
    who: tuple[str, ...],
) -> None:
    """Every reader reads exactly its row of the table, many rows at once or by id."""
    sessions = await _seed_cast(store)
    ids = list(sessions.values())
    readers = {name: cast_reader(name, orgs=orgs) for name in who}
    named = {session: name for name, session in sessions.items()}

    listed = {
        name: {named[each] for each in await visible_ids(store, reader, ids)}
        for name, reader in readers.items()
    }
    unhidden = {
        name: {named[each] for each in await unhidden_ids(store, reader, ids)}
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
        oversight=True,
    )

    assert await visible_ids(store, NOBODY, ids) == set()
    assert await unhidden_ids(store, NOBODY, ids) == set()
    assert await store.read_feed() == []
    assert len(await store.read_feed(scope=FeedScope(reader=every))) == 1


async def test_hidden_false_drops_private_from_listings_not_reads(
    store: Store,
) -> None:
    """A listing asks for no private session; a read by id keeps it."""
    sessions = await _seed_cast(store)
    ann = cast_reader("ann", orgs="single")
    listing = cast_reader("ann", orgs="single", hidden=False)
    ids = list(sessions.values())

    async with store.engine.acquire() as conn:
        read = await reads_session(conn, reader=ann, session_id=sessions["s_private"])
    kept = await visible_ids(store, ann, ids)
    listed = await visible_ids(store, listing, ids)

    assert read is True
    assert kept - listed == {sessions["s_private"]}


async def test_visible_keeps_the_sessions_of_an_audience_read(store: Store) -> None:
    open_id = await insert_session(store, audience="open")
    staff_id = await insert_session(store, audience="staff")
    partner_id = await insert_session(store, audience="partner")
    ids = [open_id, staff_id, partner_id]

    assert await visible_ids(store, _reads("open"), ids) == {open_id}
    assert await visible_ids(store, _reads("open", "staff"), ids) == {open_id, staff_id}
    assert await visible_ids(store, _reads(), ids) == set()


async def test_hidden_drops_the_rows_of_a_session_not_read_and_keeps_the_rest(
    store: Store,
) -> None:
    open_id = await insert_session(store, audience="open")
    staff_id = await insert_session(store, audience="staff")
    no_session = uuid4()
    ids = [open_id, staff_id, no_session]

    assert await unhidden_ids(store, _reads("open"), ids) == {open_id, no_session}
    assert await unhidden_ids(store, _reads("open", "staff"), ids) == set(ids)
    assert await unhidden_ids(store, _reads(), ids) == {no_session}


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


async def _seed_cast(store: Store) -> dict[str, UUID]:
    """Seed every session of ``TABLE``, by name."""
    sessions = {
        name: await insert_session(store, audience=audience, account="Ann@Lab.example")
        for name, audience in (
            ("s_open", "open"),
            ("s_private", "private"),
            ("s_staff", "staff"),
        )
    }
    sessions["s_svc"] = await insert_session(store, account="lead@service.invalid")
    return sessions


async def insert_session(
    store: Store,
    *,
    audience: str = "open",
    cli: str | None = None,
    account: str = "tester@example.com",
    key: UUID | None = None,
    created: datetime = T0,
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


def cast_reader(name: str, *, orgs: ChatOrgs, hidden: bool = True) -> SessionReader:
    """Return the reader ``name`` of the cast signs in as, on a server in ``orgs``."""
    email, audiences, admin, hosted_domain = CAST[name]
    return caller_reader(
        email,
        audiences=audiences,
        admin=admin,
        orgs=orgs,
        assistant=ASSISTANT,
        hosted_domain=hosted_domain,
        hidden=hidden,
    )


async def visible_ids(
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


async def unhidden_ids(
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
    """Return a reader of every ``open`` session of ``audiences``."""
    return SessionReader(audiences=frozenset(audiences), every_session=True)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
