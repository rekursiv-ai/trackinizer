"""Who reads, writes and drains a session, and who opens one into which audience.

A session is read, written and drained by the key that opened it and by a caller the
transcript test lets read it; the assistant may also send input to a session for a
user who may write there, and read one for a user who reads it.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import uuid

from fastapi import HTTPException

import pytest

from trackinizer.conftest import make_conn, make_store, set_field_row
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.session_access import (
    Asker,
    SessionAccess,
    granted_audience,
    require_attributable,
    require_row_account,
    require_row_purge,
    require_session_access,
    resolve_asker,
    session_reader,
)
from trackinizer.server.audience import SessionReader
from trackinizer.server.auth import AuthIdentity, Role
from trackinizer.server.config import Assistant, ChatOrgs, Config
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from collections.abc import Sequence
    from unittest.mock import AsyncMock

    from fastapi import Request


OPENER = uuid.uuid4()
MODES: tuple[SessionAccess, ...] = ("read", "write", "drain", "input")
SCOUT: Final = Assistant(actor="scout", email="scout@example.com")


def make_caller(
    *read: str,
    key: uuid.UUID | None = None,
    email: str = "caller@example.com",
    hosted_domain: str | None = None,
) -> AuthIdentity:
    return AuthIdentity(
        user_id=uuid.uuid4(),
        api_key_id=key,
        email=email,
        role="writer",
        read_audiences=frozenset(read),
        hosted_domain=hosted_domain,
    )


def make_asker(
    *read: str,
    role: Role = "writer",
    email: str = "asker@example.com",
) -> Asker:
    """Return a user the assistant acts for, holding ``read`` as a person."""
    return Asker(email=email, role=role, read_audiences=frozenset(read))


# Each answer is one ``fetchval``'s, in order: whether a reader reads a session, and for
# a read for an asker the chat's first session between them. The check must ask each,
# and no more.
async def access_refusal(
    caller: AuthIdentity,
    session: AgentSession,
    mode: SessionAccess,
    *,
    assistant: Assistant | None = None,
    asker: Asker | None = None,
    chat: uuid.UUID | None = None,
    answers: Sequence[object] = (),
) -> HTTPException | None:
    """Ask the check with ``assistant`` configured; the database answers ``answers``."""
    conn = make_conn()
    conn.fetchval.side_effect = list(answers)
    store, _engine = make_store(conn)
    state = SimpleNamespace(store=store, config=Config(assistant=assistant))
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))
    refusal: HTTPException | None = None
    try:
        await require_session_access(
            request,
            caller,
            session=session,
            mode=mode,
            asker=None if asker is None else replace(asker, chat=chat),
        )
    except HTTPException as error:
        refusal = error
    assert conn.fetchval.await_count == len(answers)
    return refusal


async def access_refused(
    caller: AuthIdentity,
    session: AgentSession,
    mode: SessionAccess,
    *,
    assistant: Assistant | None = None,
    asker: Asker | None = None,
    answers: Sequence[object] = (),
) -> bool:
    """Say whether the check refuses; a refusal names the session's audience."""
    refusal = await access_refusal(
        caller,
        session,
        mode,
        assistant=assistant,
        asker=asker,
        answers=answers,
    )
    if refusal is None:
        return False
    assert (refusal.status_code, refusal.detail) == (
        403,
        {"code": "session_audience", "audience": session.audience},
    )
    return True


class TestRequireSessionAccess:
    """Whether the check asks the database, and what it makes of the answer."""

    @pytest.mark.asyncio
    async def test_a_caller_the_database_says_reads_it_may_do_everything(
        self,
    ) -> None:
        session = AgentSession(audience="staff")

        reads = [
            await access_refused(make_caller(), session, m, answers=[True])
            for m in MODES
        ]
        not_ = [
            await access_refused(make_caller(), session, m, answers=[False])
            for m in MODES
        ]

        assert reads == [False] * 4
        assert not_ == [True] * 4

    @pytest.mark.asyncio
    async def test_the_opening_key_passes_every_mode_without_asking(self) -> None:
        session = AgentSession(audience="staff", opened_by_api_key_id=OPENER)
        opener = make_caller("open", key=OPENER)

        assert [await access_refused(opener, session, m) for m in MODES] == [False] * 4

    @pytest.mark.asyncio
    async def test_another_key_does_not_pass_as_the_opener(self) -> None:
        session = AgentSession(audience="staff", opened_by_api_key_id=OPENER)
        other = make_caller("open", key=uuid.uuid4())

        assert await access_refused(other, session, "read", answers=[False])

    @pytest.mark.asyncio
    async def test_no_key_never_matches_a_session_with_no_opening_key(self) -> None:
        """A browser has no key, and ``--no-auth`` sessions have no opener."""
        session = AgentSession(audience="staff", opened_by_api_key_id=None)

        assert await access_refused(
            make_caller("open", key=None),
            session,
            "write",
            answers=[False],
        )


@pytest.mark.parametrize(
    ("orgs", "email", "hosted_domain", "org", "oversight"),
    [
        ("domain", "Ann@Lab.Example", "Lab.example", "lab.example", True),
        ("domain", "Ann@Lab.Example", None, None, True),
        ("domain", "sam@gmail.com", None, None, True),
        ("domain", "agent@service.invalid", "service.invalid", None, False),
        ("domain", "scout@example.com", "example.com", None, False),
        ("single", "Ann@Lab.Example", "lab.example", None, True),
        ("single", "agent@service.invalid", None, None, False),
        ("single", "scout@example.com", None, None, False),
        ("individual", "Ann@Lab.Example", "lab.example", None, True),
        ("individual", "scout@example.com", None, None, False),
    ],
)
def test_session_reader_follows_the_chat_orgs_setting(
    orgs: ChatOrgs,
    *,
    email: str,
    hosted_domain: str | None,
    org: str | None,
    oversight: bool,
) -> None:
    """Only a person has an organisation, which Google's hd claim names, and oversees.

    One server reads every session.
    """
    state = SimpleNamespace(config=Config(assistant=SCOUT, chat_orgs=orgs))
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))

    reader = session_reader(
        request,
        make_caller("open", "staff", email=email, hosted_domain=hosted_domain),
    )
    listing = session_reader(request, make_caller("open", email=email), hidden=False)

    assert reader == SessionReader(
        audiences=frozenset({"open", "staff"}),
        email=email.lower(),
        org=org,
        every_session=orgs == "single",
        oversight=oversight,
    )
    assert (listing.hidden, listing.oversight) == (False, False)


def test_without_auth_the_one_caller_reads_every_session_and_open_chat() -> None:
    state = SimpleNamespace(config=Config(auth_disabled=True, chat_orgs="individual"))
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))

    reader = session_reader(request, make_caller(email="no-auth@localhost"))

    assert reader == SessionReader(
        audiences=AUDIENCES,
        email="no-auth@localhost",
        every_session=True,
        oversight=True,
    )


class TestTheAssistantsAsker:
    """The assistant reaches a session it cannot read only for a user who may write there.

    It sends a line for a signed-in user, so it passes on that user's rights and none
    of its own: the user is the session's account, or reads the session, and can write
    as a person.
    """

    _session = AgentSession(audience="staff", account="owner@example.com")
    _scout = make_caller("open", email="scout@example.com")

    async def _refused_read(
        self,
        asker: Asker | None,
        *,
        answers: Sequence[object],
        session: AgentSession | None = None,
    ) -> bool:
        return await access_refused(
            self._scout,
            session or self._session,
            "input",
            assistant=SCOUT,
            asker=asker,
            answers=answers,
        )

    @pytest.mark.asyncio
    async def test_it_relays_for_an_asker_who_reads_the_session(self) -> None:
        assert not await self._refused_read(
            make_asker("open", "staff"),
            answers=[False, True],
        )

    @pytest.mark.asyncio
    async def test_it_relays_for_the_user_whose_session_it_is_without_asking(
        self,
    ) -> None:
        """A tester steers the campaign attributed to them, though it is staff's to read."""
        asker = make_asker("open", email="owner@example.com")

        assert not await self._refused_read(asker, answers=[False])

    @pytest.mark.asyncio
    async def test_it_refuses_an_asker_who_neither_reads_nor_owns_the_session(
        self,
    ) -> None:
        stranger = make_asker("open", email="stranger@example.com")

        assert await self._refused_read(stranger, answers=[False, False])

    @pytest.mark.asyncio
    async def test_it_refuses_a_send_that_names_no_asker(self) -> None:
        assert await self._refused_read(None, answers=[False])

    @pytest.mark.parametrize(
        "asker",
        [
            pytest.param(make_asker("open", "staff", role="viewer"), id="reads it"),
            pytest.param(
                make_asker("open", role="viewer", email="owner@example.com"),
                id="owns it",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_it_refuses_an_asker_who_may_not_write(self, asker: Asker) -> None:
        assert await self._refused_read(asker, answers=[False])

    @pytest.mark.parametrize("role", ["writer", "admin"])
    @pytest.mark.asyncio
    async def test_a_writer_or_an_admin_asker_may_write(self, role: Role) -> None:
        asker = make_asker("open", "staff", role=role)

        assert not await self._refused_read(asker, answers=[False, True])

    @pytest.mark.asyncio
    async def test_an_asker_lends_its_rights_to_no_sender_but_the_assistant(
        self,
    ) -> None:
        asker = make_asker("open", "staff")

        assert await access_refused(
            make_caller("open", email="other@example.com"),
            self._session,
            "input",
            assistant=SCOUT,
            asker=asker,
            answers=[False],
        )

    @pytest.mark.asyncio
    async def test_an_asker_opens_neither_a_write_nor_a_drain(self) -> None:
        """A read for an asker is ``TestTheAssistantReadsForAnAsker``'s."""
        asker = make_asker("open", "staff")

        refused = [
            await access_refused(
                self._scout,
                self._session,
                mode,
                assistant=SCOUT,
                asker=asker,
                answers=[False],
            )
            for mode in ("write", "drain")
        ]

        assert refused == [True] * 2

    @pytest.mark.parametrize("audience", ["open", "staff"])
    @pytest.mark.asyncio
    async def test_a_session_the_assistant_reads_needs_no_asker(
        self,
        audience: str,
    ) -> None:
        """The assistant is then a writer like another, and asks no one's leave."""
        reading = make_caller("open", "staff", email="scout@example.com")

        assert not await access_refused(
            reading,
            AgentSession(audience=audience),
            "input",
            assistant=SCOUT,
            answers=[True],
        )


_USER_SQL: Final = (
    "SELECT email, role, hosted_domain FROM users "
    "WHERE email = $1 AND status = 'active'"
)


class TestResolveAsker:
    """The server reads the asker's rights from its own tables, and only for the assistant."""

    @classmethod
    def _asked(
        cls,
        conn: AsyncMock,
        *,
        staff: tuple[str, ...] = (),
        assistant: Assistant | None = SCOUT,
    ) -> Request:
        """Return a request whose app serves ``conn`` under the configured assistant."""
        store, _engine = make_store(conn)
        config = Config(assistant=assistant, staff=staff)
        state = SimpleNamespace(store=store, config=config)
        return cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))

    @classmethod
    def _sender(cls, email: str) -> AuthIdentity:
        return make_caller("open", email=email)

    @pytest.mark.asyncio
    async def test_it_finds_an_active_user_by_account_whatever_the_case_named(
        self,
    ) -> None:
        conn = make_conn()
        set_field_row(
            conn,
            {"email": "wes@open.example", "role": "writer", "hosted_domain": None},
        )

        asker = await resolve_asker(
            self._asked(conn),
            self._sender("scout@example.com"),
            email="WES@Open.Example",
        )

        assert asker == Asker(
            email="wes@open.example",
            role="writer",
            read_audiences=frozenset({"open", "private"}),
        )
        assert conn.fetchrow.await_args is not None
        assert conn.fetchrow.await_args.args == (_USER_SQL, "wes@open.example")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("hosted_domain", "read"),
        [
            ("staff.example", {"open", "private", "staff"}),
            ("other.example", {"open", "private"}),
            (None, {"open", "private"}),
        ],
    )
    async def test_it_gives_the_asker_the_audiences_a_person_with_that_account_holds(
        self,
        hosted_domain: str | None,
        read: set[str],
    ) -> None:
        conn = make_conn()
        set_field_row(
            conn,
            {
                "email": "lee@staff.example",
                "role": "admin",
                "hosted_domain": hosted_domain,
            },
        )

        asker = await resolve_asker(
            self._asked(conn, staff=("*@staff.example",)),
            self._sender("scout@example.com"),
            email="lee@staff.example",
        )

        assert asker == Asker(
            email="lee@staff.example",
            role="admin",
            read_audiences=frozenset(read),
            hosted_domain=hosted_domain,
        )

    @pytest.mark.asyncio
    async def test_an_account_the_server_does_not_hold_active_is_an_asker_with_nothing(
        self,
    ) -> None:
        """Named, it stands in for the assistant's own read; held, it gives nothing."""
        conn = make_conn()
        set_field_row(conn, None)

        asker = await resolve_asker(
            self._asked(conn),
            self._sender("scout@example.com"),
            email="Gone@open.example",
        )

        assert asker == Asker(
            email="gone@open.example",
            role="viewer",
            read_audiences=frozenset(),
        )

    @pytest.mark.asyncio
    async def test_it_asks_nothing_when_no_account_is_named(self) -> None:
        conn = make_conn()

        asker = await resolve_asker(
            self._asked(conn),
            self._sender("scout@example.com"),
            email=None,
        )

        assert asker is None
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_asks_nothing_for_a_sender_that_is_not_the_assistant(self) -> None:
        conn = make_conn()

        asker = await resolve_asker(
            self._asked(conn),
            self._sender("other@example.com"),
            email="wes@open.example",
        )

        assert asker is None
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_asks_nothing_when_no_assistant_is_configured(self) -> None:
        conn = make_conn()

        asker = await resolve_asker(
            self._asked(conn, assistant=None),
            self._sender("scout@example.com"),
            email="wes@open.example",
        )

        assert asker is None
        conn.fetchrow.assert_not_awaited()


class TestRowHolder:
    """A row is purged, and moved to another account, by its account or an admin."""

    @classmethod
    def _serving(cls, row: dict[str, object] | None) -> Request:
        """Return a request whose store reads ``row`` for any id."""
        conn = make_conn()
        set_field_row(conn, row)
        store, _engine = make_store(conn)
        state = SimpleNamespace(store=store, config=Config())
        return cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("email", "role"),
        [("Ada@Example.com", "writer"), ("root@example.com", "admin")],
    )
    async def test_its_account_and_an_admin_pass(self, email: str, role: Role) -> None:
        request = self._serving({"kind": "Issue", "account": "ada@example.com"})
        caller = replace(make_caller("open", email=email), role=role)

        await require_row_account(request, caller, uuid.uuid4())
        await require_row_purge(request, caller, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_another_writer_neither_moves_nor_purges_it(self) -> None:
        request = self._serving({"kind": "Issue", "account": "ada@example.com"})
        caller = make_caller("open", email="bob@example.com")

        with pytest.raises(HTTPException) as moved:
            await require_row_account(request, caller, uuid.uuid4())
        with pytest.raises(HTTPException) as purged:
            await require_row_purge(request, caller, uuid.uuid4())

        assert [moved.value.status_code, purged.value.status_code] == [403, 403]

    @pytest.mark.asyncio
    async def test_a_row_that_is_gone_is_left_to_the_route(self) -> None:
        caller = make_caller("open", email="bob@example.com")

        await require_row_account(self._serving(None), caller, uuid.uuid4())


def _opener(pin: str | None, *opens: str) -> AuthIdentity:
    """Return a key pinned to ``pin`` whose owner opens into ``opens``."""
    return AuthIdentity(
        user_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        email="caller@example.com",
        role="writer",
        open_audiences=frozenset(opens),
        pinned_audience=pin,
    )


class TestGrantedAudience:
    """The audience a session is opened into, with no database."""

    def test_a_pinned_key_opens_into_its_pin(self) -> None:
        assert granted_audience(_opener("staff", "staff")) == "staff"

    def test_an_unpinned_caller_opens_open_even_when_it_holds_staff(self) -> None:
        assert granted_audience(_opener(None, "open", "staff")) == "open"

    @pytest.mark.parametrize("held", [(), ("open",)], ids=["nothing", "open"])
    def test_a_pin_the_caller_does_not_hold_is_refused_not_lowered_to_open(
        self,
        held: tuple[str, ...],
    ) -> None:
        with pytest.raises(HTTPException) as refused:
            _ = granted_audience(_opener("staff", *held))

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            {"code": "audience_not_held", "audience": "staff"},
        )

    def test_a_caller_that_may_not_open_into_open_is_refused(self) -> None:
        with pytest.raises(HTTPException) as refused:
            _ = granted_audience(_opener(None))

        assert refused.value.detail == {"code": "audience_not_held", "audience": "open"}

    @pytest.mark.parametrize(
        ("pin", "opens", "granted"),
        [
            (None, ("open", "private", "staff"), "private"),
            ("open", ("open",), "private"),
            ("private", ("private",), "private"),
        ],
        ids=["unpinned", "pinned open", "pinned private"],
    )
    def test_private_narrows_open_and_never_staff(
        self,
        pin: str | None,
        opens: tuple[str, ...],
        granted: str,
    ) -> None:
        """Hiding only narrows, so a key that opens ``open`` may open hidden."""
        assert granted_audience(_opener(pin, *opens), private=True) == granted
        with pytest.raises(HTTPException) as refused:
            _ = granted_audience(_opener("staff", "staff"), private=True)

        assert refused.value.detail == {
            "code": "audience_not_held",
            "audience": "private",
        }


class TestRequireAttributable:
    """Only a service account, or an admin, may put its address on a row."""

    @pytest.mark.parametrize(
        ("role", "email", "account"),
        [
            ("writer", "wes@example.com", "wes@example.com"),
            ("writer", "wes@example.com", "someone@example.com"),
            ("writer", "campaign@service.invalid", "campaign@service.invalid"),
            ("writer", "Campaign@Service.invalid", "campaign@service.invalid"),
            ("admin", "root@example.com", "campaign@service.invalid"),
        ],
    )
    def test_the_account_itself_an_admin_or_an_ordinary_address_passes(
        self,
        role: Role,
        email: str,
        account: str,
    ) -> None:
        require_attributable(make_test_identity(email=email, role=role), account)

    @pytest.mark.parametrize("role", ["viewer", "writer"])
    def test_another_callers_service_address_is_refused(self, role: Role) -> None:
        with pytest.raises(HTTPException) as refused:
            require_attributable(
                make_test_identity(email="wes@example.com", role=role),
                "campaign@service.invalid",
            )

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "A row is attributed to a service account by that account or an admin",
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
