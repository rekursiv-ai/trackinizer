"""Who reads and writes a session, and who opens one into which audience.

A session is read and written by the key that opened it and by a caller the
transcript test lets read it. Input requires its account or opening key; the assistant
may relay input for a writer of that account, and read for a user who reads it.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import AsyncMock

import uuid

from fastapi import HTTPException

import pytest

from trackinizer.conftest import (
    FakeEngine,
    RecordingPolicy,
    make_conn,
    make_store,
    set_field_row,
)
from trackinizer.server.access_policy import AccessDeniedError, Resource
from trackinizer.server.api import session_access
from trackinizer.server.api.conftest import (
    OPENER,
    SCOUT,
    access_refusal,
    access_refused,
    make_asker,
    make_caller,
    make_test_identity,
)
from trackinizer.server.api.session_access import (
    SessionAccess,
    granted_audience,
    require_attributable,
    require_readable,
    require_row_account,
    require_row_purge,
    require_row_write,
    require_session_access,
    require_session_id_change,
    require_session_row_write,
    require_session_write_access,
    resolve_asker,
)
from trackinizer.server.api.session_identity import Asker
from trackinizer.server.audience import SessionReader
from trackinizer.server.auth import AuthIdentity, Role
from trackinizer.server.config import Assistant, Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.types.errors import NotFoundError
from trackinizer.types.inquiries import AgentSession


if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import Request

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.mock_test_support import MockConnection
    from trackinizer.server.access_policy import Principal


MODES: tuple[SessionAccess, ...] = ("read", "write", "input")
CONTROLS: tuple[SessionAccess, ...] = ("write", "input")


class TestRequireSessionAccess:
    """Whether the check asks the database, and what it makes of the answer."""

    @pytest.mark.asyncio
    async def test_reading_a_session_grants_no_control_of_it(self) -> None:
        session = AgentSession(audience="staff", account="owner@example.com")
        reader = make_caller("open", "staff")

        assert not await access_refused(reader, session, "read", answers=[True])
        assert await access_refused(reader, session, "read", answers=[False])
        for mode in CONTROLS:
            assert await access_refused(reader, session, mode)

    @pytest.mark.parametrize("mode", ["write"])
    @pytest.mark.parametrize("key", [None, uuid.UUID(int=10)])
    @pytest.mark.asyncio
    async def test_the_account_writes_what_it_still_reads(
        self,
        mode: SessionAccess,
        key: uuid.UUID | None,
    ) -> None:
        session = AgentSession(
            account="Owner@Example.com",
            audience="staff",
            opened_by_api_key_id=OPENER,
        )
        owner = make_caller("open", email="owner@example.com", key=key)

        assert not await access_refused(owner, session, mode, answers=[True])
        assert await access_refused(owner, session, mode, answers=[False])

    @pytest.mark.parametrize("key", [None, uuid.UUID(int=10)])
    @pytest.mark.asyncio
    async def test_the_account_sends_input_with_a_browser_or_another_key(
        self,
        key: uuid.UUID | None,
    ) -> None:
        session = AgentSession(
            account="Owner@Example.com",
            audience="staff",
            opened_by_api_key_id=OPENER,
        )

        assert not await access_refused(
            make_caller("open", email="owner@example.com", key=key),
            session,
            "input",
        )

    @pytest.mark.asyncio
    async def test_a_post_asks_the_input_rule_of_a_session_that_is_no_chat(
        self,
    ) -> None:
        session = AgentSession(account="owner@example.com", audience="open")

        assert not await access_refused(
            make_caller("open", email="owner@example.com"),
            session,
            "post",
        )
        assert await access_refused(make_caller("open"), session, "post")

    @pytest.mark.asyncio
    async def test_the_opening_key_passes_every_mode_without_asking(self) -> None:
        session = AgentSession(audience="staff", opened_by_api_key_id=OPENER)
        opener = make_caller("open", key=OPENER)

        assert [await access_refused(opener, session, m) for m in MODES] == [False] * 3

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
        )


@pytest.mark.asyncio
async def test_the_transcript_check_names_the_target_session() -> None:
    conn = make_conn()
    conn.fetchval.return_value = True
    store, _engine = make_store(conn)
    request = cast(
        "Request",
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store))),
    )
    session = AgentSession(audience="open")

    await require_session_access(request, make_caller(), session=session, mode="read")

    assert conn.fetchval.await_args is not None
    assert conn.fetchval.await_args.args[1] == session.id


@pytest.mark.asyncio
@pytest.mark.parametrize("readable", [True, False])
async def test_a_delegated_read_keeps_the_asker_chat_and_caller(
    monkeypatch: pytest.MonkeyPatch,
    readable: bool,
) -> None:
    reads = AsyncMock(return_value=readable)
    monkeypatch.setattr(session_access, "reads_session", reads)
    caller = make_caller("open", email=SCOUT.email)
    session = AgentSession(audience="open")
    asker = make_asker("open")
    chat = uuid.uuid4()

    refusal = await access_refusal(
        caller,
        session,
        "read",
        assistant=SCOUT,
        asker=asker,
        chat=chat,
    )

    assert reads.await_args is not None
    assert reads.await_args.args[0] is not None
    assert reads.await_args.kwargs["session_id"] == session.id
    assert reads.await_args.kwargs["reader"] == SessionReader(
        email=asker.email,
        audiences=asker.read_audiences,
    )
    if readable:
        assert refusal is None
    else:
        assert refusal is not None
        assert (refusal.status_code, refusal.detail) == (
            403,
            {"code": "session_audience", "audience": "open"},
        )


class TestTheAssistantsAsker:
    """The assistant relays input only for a writer whose account holds the session.

    Naming an asker restricts the assistant to that user's rights, even when it opened
    the target session or can read its transcript.
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
    async def test_it_refuses_an_asker_who_only_reads_the_session(self) -> None:
        assert await self._refused_read(make_asker("open", "staff"), answers=[])

    @pytest.mark.asyncio
    async def test_it_relays_for_the_user_whose_session_it_is_without_asking(
        self,
    ) -> None:
        """A tester steers the campaign attributed to them, though it is staff's to read."""
        asker = make_asker("open", email="owner@example.com")

        assert not await self._refused_read(asker, answers=[])

    @pytest.mark.asyncio
    async def test_it_refuses_an_asker_who_neither_reads_nor_owns_the_session(
        self,
    ) -> None:
        stranger = make_asker("open", email="stranger@example.com")

        assert await self._refused_read(stranger, answers=[])

    @pytest.mark.asyncio
    async def test_it_refuses_a_send_that_names_no_asker(self) -> None:
        assert await self._refused_read(None, answers=[])

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
        assert await self._refused_read(asker, answers=[])

    @pytest.mark.parametrize("role", ["writer", "admin"])
    @pytest.mark.asyncio
    async def test_a_writer_or_an_admin_asker_may_write(self, role: Role) -> None:
        asker = make_asker("open", "staff", role=role, email="owner@example.com")

        assert not await self._refused_read(asker, answers=[])

    @pytest.mark.asyncio
    async def test_an_asker_lends_its_rights_to_no_sender_but_the_assistant(
        self,
    ) -> None:
        asker = make_asker("open", "staff", email="owner@example.com")

        assert await access_refused(
            make_caller("open", email="other@example.com"),
            self._session,
            "input",
            assistant=SCOUT,
            asker=asker,
        )

    @pytest.mark.asyncio
    async def test_an_asker_opens_no_write(self) -> None:
        """A read for an asker is ``TestTheAssistantReadsForAnAsker``'s."""
        asker = make_asker("open", "staff")

        refused = [
            await access_refused(
                self._scout,
                self._session,
                mode,
                assistant=SCOUT,
                asker=asker,
            )
            for mode in ("write",)
        ]

        assert refused == [True]

    @pytest.mark.parametrize("audience", ["open", "staff"])
    @pytest.mark.asyncio
    async def test_a_session_the_assistant_only_reads_still_needs_its_owner(
        self,
        audience: str,
    ) -> None:
        reading = make_caller("open", "staff", email="scout@example.com")

        assert await access_refused(
            reading,
            AgentSession(audience=audience),
            "input",
            assistant=SCOUT,
        )

    @pytest.mark.parametrize(
        "asker",
        [
            make_asker("open", "staff"),
            make_asker("open", role="viewer", email="owner@example.com"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_named_asker_cannot_borrow_the_assistants_opening_key(
        self,
        asker: Asker,
    ) -> None:
        caller = replace(self._scout, api_key_id=OPENER)
        session = replace(self._session, opened_by_api_key_id=OPENER)

        assert await access_refused(
            caller,
            session,
            "input",
            assistant=SCOUT,
            asker=asker,
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
    """An admin may purge rows, but only a session's account may reassign it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("email", "role"),
        [("Ada@Example.com", "writer"), ("root@example.com", "admin")],
    )
    async def test_its_account_and_an_admin_pass(self, email: str, role: Role) -> None:
        request = _serving({"kind": "Issue", "account": "ada@example.com"})
        caller = replace(make_caller("open", email=email), role=role)

        await require_row_account(request, caller, uuid.uuid4())
        await require_row_purge(request, caller, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_another_writer_neither_moves_nor_purges_it(self) -> None:
        request = _serving({"kind": "Issue", "account": "ada@example.com"})
        caller = make_caller("open", email="bob@example.com")

        with pytest.raises(HTTPException) as moved:
            await require_row_account(request, caller, uuid.uuid4())
        with pytest.raises(HTTPException) as purged:
            await require_row_purge(request, caller, uuid.uuid4())

        assert [moved.value.status_code, purged.value.status_code] == [403, 403]
        assert (
            moved.value.detail
            == "A row's account is changed by that account or an admin"
        )

    @pytest.mark.parametrize("audience", ["open", "staff", "private", None])
    @pytest.mark.asyncio
    async def test_an_admin_cannot_reassign_another_accounts_session(
        self,
        audience: str | None,
    ) -> None:
        request = _serving(
            {
                "kind": "AgentSession",
                "account": "ada@example.com",
                "audience": audience,
            },
        )
        caller = replace(make_caller("open", email="root@example.com"), role="admin")

        with pytest.raises(HTTPException) as refused:
            await require_row_account(request, caller, uuid.uuid4())

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "A session's account is changed by that account alone",
        )

    @pytest.mark.asyncio
    async def test_the_session_account_can_still_transfer_it(self) -> None:
        request = _serving(
            {"kind": "AgentSession", "account": "Ada@Example.com", "audience": "open"},
        )

        await require_row_account(
            request,
            make_caller("open", email="ada@example.com"),
            uuid.uuid4(),
        )

    @pytest.mark.asyncio
    async def test_a_session_the_purger_neither_opened_nor_reads_is_hidden(
        self,
    ) -> None:
        request = _serving(
            {"kind": "AgentSession", "account": "ada@example.com", "audience": "staff"},
        )
        admin = replace(make_caller("open", email="root@example.com"), role="admin")

        with pytest.raises(HTTPException) as refused:
            await require_row_purge(request, admin, uuid.uuid4())

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            {"code": "session_audience", "audience": "staff"},
        )

    @pytest.mark.asyncio
    async def test_a_row_that_is_gone_is_left_to_the_route(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        state = SimpleNamespace(store=store, config=Config())
        request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))
        caller = make_caller("open", email="bob@example.com")
        target_id = uuid.uuid4()

        await require_row_account(request, caller, target_id)

        assert conn.fetchrow.await_args is not None
        assert conn.fetchrow.await_args.args[1] == target_id


class TestRequireSessionIdChange:
    """Only a session's opening key, or an admin, renames its ``cli_session_id``."""

    _SESSION: Final[dict[str, object]] = {
        "kind": "AgentSession",
        "account": "ada@example.com",
        "audience": "open",
        "cli_session_id": "claude:abc",
        "opener": OPENER,
    }

    @pytest.mark.parametrize(
        "caller",
        [
            make_caller("open", key=OPENER),
            replace(make_caller("open"), role="admin"),
        ],
        ids=["opener", "admin"],
    )
    @pytest.mark.asyncio
    async def test_the_opening_key_or_an_admin_renames_it(
        self,
        caller: AuthIdentity,
    ) -> None:
        await require_session_id_change(
            _serving(self._SESSION),
            caller,
            uuid.uuid4(),
        )

    @pytest.mark.parametrize("key", [None, uuid.UUID(int=7)])
    @pytest.mark.asyncio
    async def test_another_key_or_none_is_refused(self, key: uuid.UUID | None) -> None:
        with pytest.raises(HTTPException) as refused:
            await require_session_id_change(
                _serving(self._SESSION),
                make_caller("open", email="ada@example.com", key=key),
                uuid.uuid4(),
            )

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "A session's cli_session_id is changed by the key that opened it",
        )

    @pytest.mark.parametrize("row", [None, {"kind": "Issue", "account": "a@b.c"}])
    @pytest.mark.asyncio
    async def test_a_row_that_is_no_session_is_left_alone(
        self,
        row: dict[str, object] | None,
    ) -> None:
        await require_session_id_change(
            _serving(row),
            make_caller("open"),
            uuid.uuid4(),
        )


class TestRequireReadable:
    @pytest.mark.asyncio
    async def test_a_row_the_caller_may_not_read_is_not_found(self) -> None:
        hidden = uuid.uuid4()
        conn = make_conn()
        conn.fetchval.return_value = hidden
        store, _engine = make_store(conn)
        request = cast(
            "Request",
            SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(store=store, config=Config()),
                ),
                state=SimpleNamespace(),
            ),
        )

        with pytest.raises(NotFoundError, match=f"inquiry {hidden} not found"):
            await require_readable(request, make_caller("open"), [hidden, uuid.uuid4()])

    @pytest.mark.asyncio
    async def test_naming_no_row_asks_nothing(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        request = cast(
            "Request",
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store))),
        )

        await require_readable(request, make_caller("open"), [])

        conn.fetchval.assert_not_awaited()


class TestRequireSessionWriteAccess:
    """A viewer writes only the session its own key opened; a writer any."""

    def test_a_viewer_writes_the_session_its_key_opened(self) -> None:
        viewer = replace(make_caller("open", key=OPENER), role="viewer")

        require_session_write_access(
            viewer,
            AgentSession(opened_by_api_key_id=OPENER),
        )

    @pytest.mark.parametrize("key", [None, uuid.UUID(int=7)])
    def test_a_viewer_without_the_opening_key_is_refused(
        self,
        key: uuid.UUID | None,
    ) -> None:
        viewer = replace(make_caller("open", key=key), role="viewer")

        with pytest.raises(HTTPException) as refused:
            require_session_write_access(
                viewer,
                AgentSession(opened_by_api_key_id=OPENER),
            )

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "Own session key required",
        )

    def test_a_writer_needs_no_key(self) -> None:
        require_session_write_access(
            make_caller("open", key=None),
            AgentSession(opened_by_api_key_id=OPENER),
        )


class TestRowWrite:
    """The access policy is asked about an existing row, inside a transaction."""

    @pytest.mark.asyncio
    async def test_the_policy_is_asked_about_the_row_and_its_account(self) -> None:
        policy = RecordingPolicy()
        request, conn = _policed(policy, account="ada@example.com")
        target_id = uuid.uuid4()

        await require_row_write(request, make_caller("open"), target_id)

        assert conn.fetchval.await_args is not None
        assert conn.fetchval.await_args.args[1] == target_id
        principal, resource, action = cast(
            "tuple[Principal, Resource, str]",
            policy.of("check")[0],
        )
        assert principal.email == "caller@example.com"
        assert principal.groups == policy.groups_of
        assert resource == Resource(
            kind="inquiry",
            id=str(target_id),
            account="ada@example.com",
        )
        assert action == "write"
        assert policy.sent[-1][-1] == "BEGIN"

    @pytest.mark.asyncio
    async def test_a_refusal_reaches_the_caller(self) -> None:
        request, _conn = _policed(
            RecordingPolicy(deny_writes=True),
            account="ada@example.com",
        )

        with pytest.raises(AccessDeniedError):
            await require_row_write(request, make_caller("open"), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_a_row_that_is_gone_is_not_asked_about(self) -> None:
        policy = RecordingPolicy(deny_writes=True)
        request, _conn = _policed(policy, account=None)

        await require_row_write(request, make_caller("open"), uuid.uuid4())

        assert policy.of("check") == []


class TestRequireSessionRowWrite:
    """A write by id to a session needs its audience; other rows pass."""

    @pytest.mark.parametrize("row", [None, {"kind": "Issue", "account": "a@b.c"}])
    @pytest.mark.asyncio
    async def test_a_row_that_is_no_session_passes_without_the_chat_rule(
        self,
        row: dict[str, object] | None,
    ) -> None:
        await require_session_row_write(_serving(row), make_caller(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_a_session_the_caller_neither_opened_nor_reads_is_hidden(
        self,
    ) -> None:
        request = _serving(
            {"kind": "AgentSession", "account": "ada@example.com", "audience": "staff"},
        )

        with pytest.raises(HTTPException) as refused:
            await require_session_row_write(request, make_caller("open"), uuid.uuid4())

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            {"code": "session_audience", "audience": "staff"},
        )

    @pytest.mark.parametrize(
        ("caller", "audience"),
        [(make_caller("open"), "open"), (make_caller(key=OPENER), "staff")],
        ids=["reads it", "opened it"],
    )
    @pytest.mark.asyncio
    async def test_a_session_it_reads_or_opened_goes_on_to_the_chat_rule(
        self,
        caller: AuthIdentity,
        audience: str,
    ) -> None:
        request = _serving(
            {
                "kind": "AgentSession",
                "account": "ada@example.com",
                "audience": audience,
                "cli_session_id": "claude:abc",
                "opener": OPENER,
            },
        )
        target_id = uuid.uuid4()

        await require_session_row_write(request, caller, target_id)


@pytest.mark.asyncio
async def test_purging_a_row_that_is_gone_is_left_to_the_route() -> None:
    await require_row_purge(
        _serving(None),
        make_caller("open", email="bob@example.com"),
        uuid.uuid4(),
    )


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


def _serving(row: dict[str, object] | None) -> Request:
    """Return a request whose store reads ``row`` for any id."""
    conn = make_conn()
    set_field_row(conn, row)
    store, _engine = make_store(conn)
    state = SimpleNamespace(store=store, config=Config())
    return cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))


def _policed(
    policy: RecordingPolicy,
    *,
    account: str | None,
) -> tuple[Request, MockConnection]:
    """Return a request whose store asks ``policy`` and finds a row held by ``account``."""
    conn = make_conn()
    conn.fetchval.return_value = account
    store = Store(
        cast("DatabaseEngine", FakeEngine(conn)),
        embed=StubEmbedder(),
        access_policy=policy,
    )
    state = SimpleNamespace(store=store, config=Config())
    request = SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace())
    return cast("Request", request), conn


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
