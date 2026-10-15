"""Who holds which audience, and what the transcript test asks of each reader.

Staff patterns, the owner's set, a key's limits; the reader each organisation mode
derives; the SQL the test binds; who may hide a session; and which writes to a
transcript a live stream's viewer hears of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.audience import (
    NOBODY,
    Audiences,
    SessionReader,
    TranscriptPings,
    audiences_for,
    hidden_exists_sql,
    key_audiences,
    may_hide,
    members_reader,
    org_of,
    reader_for,
    visible_sql,
)
from trackinizer.server.values import vetted_sql


if TYPE_CHECKING:
    from trackinizer.server.config import ChatOrgs


_BOTH = frozenset({"open", "private", "staff"})
_OPEN = frozenset({"open", "private"})


class TestAudiencesFor:
    def test_unset_means_nobody_is_staff(self) -> None:
        assert (
            audiences_for("ada@example.com", hosted_domain="example.com", staff=())
            == _OPEN
        )

    def test_a_literal_pattern_matches_the_verified_email_alone(self) -> None:
        staff = ("ada@example.com",)
        assert (
            audiences_for("ada@example.com", hosted_domain=None, staff=staff) == _BOTH
        )
        assert (
            audiences_for("bob@example.com", hosted_domain="example.com", staff=staff)
            == _OPEN
        )

    def test_a_literal_pattern_ignores_case(self) -> None:
        assert (
            audiences_for(
                "Ada@Example.COM",
                hosted_domain=None,
                staff=("ada@example.com",),
            )
            == _BOTH
        )

    def test_a_domain_pattern_needs_the_hosted_domain_claim(self) -> None:
        staff = ("*@example.com",)
        assert (
            audiences_for("ada@example.com", hosted_domain="example.com", staff=staff)
            == _BOTH
        )
        assert (
            audiences_for("ada@example.com", hosted_domain="EXAMPLE.com", staff=staff)
            == _BOTH
        )

    def test_a_consumer_account_on_the_staff_domain_is_not_staff(self) -> None:
        """Google verifies the address, not the organisation; no ``hd`` means none."""
        staff = ("*@example.com",)
        assert (
            audiences_for("ada@example.com", hosted_domain=None, staff=staff) == _OPEN
        )

    def test_a_domain_pattern_needs_the_claim_to_name_the_same_domain(self) -> None:
        staff = ("*@example.com",)
        assert (
            audiences_for("ada@example.com", hosted_domain="other.org", staff=staff)
            == _OPEN
        )
        assert (
            audiences_for("ada@other.org", hosted_domain="example.com", staff=staff)
            == _OPEN
        )

    def test_a_subdomain_is_not_the_domain(self) -> None:
        staff = ("*@example.com",)
        assert (
            audiences_for(
                "ada@mail.example.com",
                hosted_domain="mail.example.com",
                staff=staff,
            )
            == _OPEN
        )

    def test_a_domain_pattern_is_not_a_literal_address(self) -> None:
        """A login whose email is literally ``*@host`` still needs the ``hd`` claim."""
        staff = ("*@example.com",)
        assert audiences_for("*@example.com", hosted_domain=None, staff=staff) == _OPEN
        assert (
            audiences_for("*@example.com", hosted_domain="other.org", staff=staff)
            == _OPEN
        )

    def test_an_address_without_a_host_is_never_staff(self) -> None:
        assert (
            audiences_for(
                "example.com",
                hosted_domain="example.com",
                staff=("*@example.com",),
            )
            == _OPEN
        )


class TestServiceAccounts:
    """A service account has no hosted domain, so only an exact entry makes it staff."""

    def test_an_exact_address_entry_makes_the_account_staff(self) -> None:
        assert (
            audiences_for(
                "campaign@service.invalid",
                hosted_domain=None,
                staff=("campaign@service.invalid",),
            )
            == _BOTH
        )

    def test_a_domain_pattern_never_matches_a_service_account(self) -> None:
        """No hosted domain, so ``*@service.invalid`` names no one."""
        assert (
            audiences_for(
                "campaign@service.invalid",
                hosted_domain=None,
                staff=("*@service.invalid",),
            )
            == _OPEN
        )

    def test_an_entry_for_one_account_leaves_its_neighbours_open_only(self) -> None:
        assert (
            audiences_for(
                "assistant@service.invalid",
                hosted_domain=None,
                staff=("campaign@service.invalid", "*@service.invalid"),
            )
            == _OPEN
        )


class TestKeyAudiences:
    def test_a_plain_key_holds_what_its_owner_holds(self) -> None:
        assert key_audiences(_BOTH, open_into=None) == Audiences(
            read=_BOTH,
            opening=_BOTH,
            pinned=None,
        )

    def test_a_pin_opens_only_its_value_and_reads_what_the_owner_reads(self) -> None:
        pinned = key_audiences(_BOTH, open_into="staff")
        assert pinned == Audiences(
            read=_BOTH,
            opening=frozenset({"staff"}),
            pinned="staff",
        )

    def test_a_pin_the_owner_lost_opens_nothing_and_stays_pinned(self) -> None:
        """A demoted owner's staff key fails closed: no silent fall back to ``open``."""
        pinned = key_audiences(_OPEN, open_into="staff")
        assert pinned.opening == frozenset()
        assert pinned.pinned == "staff"


_ANN: Final = SessionReader(
    audiences=_BOTH,
    email="ann@lab.example",
    org="lab.example",
    every_session=False,
    every_chat=True,
    oversight=True,
    hidden=False,
    assistant="scout@service.invalid",
)
_ANN_VALUES: Final = (
    ["open", "private", "staff"],
    "ann@lab.example",
    "lab.example",
    False,
    True,
    True,
    False,
    "scout@service.invalid",
)


class TestSqlFragments:
    def test_visible_binds_the_reader_after_the_values_already_bound(self) -> None:
        params: list[object] = ["before"]

        clause = visible_sql("i", params=params, reader=_ANN)

        assert params == ["before", *_ANN_VALUES]
        assert {f"${n}::" for n in range(2, 10)} <= {
            clause[at : at + 4] for at in range(len(clause) - 3)
        }
        assert "$1" not in clause
        assert "$10" not in clause
        assert clause.startswith("COALESCE(i.agentsession_audience = ANY($2::text[])")

    def test_hidden_binds_the_reader_after_the_values_already_bound(self) -> None:
        params: list[object] = ["a", "b"]

        clause = hidden_exists_sql("e.session_id", params=params, reader=_ANN)

        assert clause == vetted_sql(
            "NOT EXISTS (SELECT 1 FROM inquiries AS hidden "
            "WHERE hidden.id = e.session_id AND NOT ",
            visible_sql("hidden", params=["a", "b"], reader=_ANN),
            ")",
        )
        assert params == ["a", "b", *_ANN_VALUES]

    def test_one_row_looks_up_its_own_chats_starter_alone(self) -> None:
        many = visible_sql("t", params=[], reader=_ANN)
        one = visible_sql("t", params=[], reader=_ANN, single=True)

        assert "LIKE 'chat:%' ORDER BY" in many
        assert (
            "began.agentsession_cli_session_id = t.agentsession_cli_session_id"
        ) in one

    @pytest.mark.parametrize(
        "alias",
        ["began", "began_key", "began_by", "shared", "began_chat"],
    )
    def test_the_row_takes_no_alias_the_chat_test_uses_inside(self, alias: str) -> None:
        with pytest.raises(ValueError, match="alias the science chat test uses"):
            _ = visible_sql(alias, params=[], reader=NOBODY)


_SCOUT: Final = "scout@service.invalid"

# (mode, email, Google's hd claim, audiences, admin, delegated, (org, every session,
# every chat, oversight)).
_REACH: Final = (
    (
        "single",
        "Ann@Lab.example",
        "lab.example",
        _OPEN,
        False,
        False,
        (None, True, True, False),
    ),
    (
        "domain",
        "ann@lab.example",
        "Lab.example",
        _OPEN,
        False,
        False,
        ("lab.example", False, False, False),
    ),
    (
        "individual",
        "ann@lab.example",
        "lab.example",
        _OPEN,
        False,
        False,
        (None, False, False, False),
    ),
    (
        "domain",
        "ann@lab.example",
        None,
        _OPEN,
        False,
        False,
        (None, False, False, False),
    ),
    ("domain", "ann@gmail.com", None, _OPEN, False, False, (None, False, False, False)),
    (
        "single",
        "lee@lab.example",
        "lab.example",
        _BOTH,
        False,
        False,
        (None, True, True, True),
    ),
    (
        "individual",
        "ada@lab.example",
        "lab.example",
        _OPEN,
        True,
        False,
        (None, False, False, True),
    ),
    (
        "single",
        "lee@lab.example",
        "lab.example",
        _BOTH,
        True,
        True,
        (None, True, False, False),
    ),
    (
        "domain",
        "lee@lab.example",
        "lab.example",
        _BOTH,
        False,
        True,
        ("lab.example", False, False, False),
    ),
    (
        "single",
        "lead@service.invalid",
        "service.invalid",
        _BOTH,
        False,
        False,
        (None, True, False, False),
    ),
    (
        "domain",
        "lead@service.invalid",
        "service.invalid",
        _BOTH,
        True,
        False,
        (None, False, False, False),
    ),
    ("single", _SCOUT, None, _OPEN, False, False, (None, True, True, False)),
    (
        "domain",
        _SCOUT,
        "service.invalid",
        _OPEN,
        False,
        False,
        (None, False, False, False),
    ),
)


class TestReaderFor:
    @pytest.mark.parametrize("case", _REACH)
    def test_reader_for_gives_each_mode_its_reach(
        self,
        case: tuple[
            ChatOrgs,
            str,
            str | None,
            frozenset[str],
            bool,
            bool,
            tuple[str | None, bool, bool, bool],
        ],
    ) -> None:
        """Only a person has an organisation or oversees; a delegated read loses both.

        A person's organisation is its domain in ``domain`` mode alone, when Google's
        ``hd`` claim names it, and it oversees as staff or an admin. A service
        account and the assistant do neither, and a delegated read keeps neither
        oversight nor every chat.
        """
        orgs, email, hosted_domain, audiences, admin, delegated, reach = case
        reader = reader_for(
            email,
            audiences=audiences,
            admin=admin,
            orgs=orgs,
            assistant=_SCOUT,
            hosted_domain=hosted_domain,
            delegated=delegated,
        )

        assert (
            reader.org,
            reader.every_session,
            reader.every_chat,
            reader.oversight,
        ) == reach
        assert (reader.email, reader.audiences, reader.assistant, reader.hidden) == (
            email.lower(),
            audiences,
            _SCOUT,
            True,
        )

    def test_a_listing_asks_for_no_hidden_session(self) -> None:
        reader = reader_for(
            "ann@lab.example",
            audiences=_OPEN,
            admin=False,
            orgs="single",
            assistant="",
            hosted_domain=None,
            hidden=False,
        )

        assert not reader.hidden

    def test_no_reader_reads_nothing_until_named(self) -> None:
        assert SessionReader(audiences=frozenset()) == NOBODY


class TestOrgOf:
    def test_a_member_belongs_to_the_domain_google_names(self) -> None:
        assert org_of("Ada@Example.COM", hosted_domain="Example.com") == "example.com"
        assert (
            org_of("grace@lab.example.com", hosted_domain="lab.example.com")
            == "lab.example.com"
        )

    @pytest.mark.parametrize(
        ("email", "hosted_domain"),
        [
            ("ada@example.com", None),
            ("ada@example.com", "other.org"),
            ("ada@gmail.com", None),
            ("not-an-email", ""),
            ("nobody@", ""),
        ],
        ids=[
            "consumer account",
            "another domain",
            "consumer mail",
            "no @",
            "no domain",
        ],
    )
    def test_an_address_whose_domain_google_does_not_name_is_no_organisation(
        self,
        email: str,
        hosted_domain: str | None,
    ) -> None:
        """Google verifies a consumer account's company address but names no ``hd``."""
        assert org_of(email, hosted_domain=hosted_domain) is None


class TestMembersReader:
    def test_an_open_chat_is_open_to_the_organisation_its_mode_names(self) -> None:
        """Every person on one organisation; the starter's email domain under domain."""
        single = members_reader("Ann@Lab.example", orgs="single", assistant=_SCOUT)
        domain = members_reader("Ann@Lab.example", orgs="domain", assistant=_SCOUT)

        assert single is not None
        assert domain is not None
        assert (single.org, single.every_session, single.every_chat) == (
            None,
            True,
            True,
        )
        assert (domain.org, domain.every_session, domain.every_chat) == (
            "lab.example",
            False,
            False,
        )
        assert (domain.email, domain.oversight, domain.audiences) == ("", False, _OPEN)

    @pytest.mark.parametrize(
        ("starter", "orgs"),
        [("ann@lab.example", "individual"), ("sam@gmail.com", "domain")],
        ids=["each person alone", "a consumer domain"],
    )
    def test_no_one_else_reads_where_no_organisation_is(
        self,
        starter: str,
        orgs: ChatOrgs,
    ) -> None:
        assert members_reader(starter, orgs=orgs, assistant=_SCOUT) is None


class TestTranscriptPings:
    @pytest.mark.asyncio
    async def test_a_session_in_an_audience_the_viewer_lacks_is_asked_of_no_one(
        self,
    ) -> None:
        engine = FakeEngine()
        pings = TranscriptPings(
            engine=cast(DatabaseEngine, engine),
            reader=SessionReader(audiences=_OPEN),
        )

        assert not await pings.hears(uuid4(), "staff")
        engine.conn.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_each_answer_is_the_databases_kept_under_the_session_and_audience(
        self,
    ) -> None:
        """A hide names another audience, so its session is asked of afresh."""
        engine = FakeEngine()
        engine.conn.fetchval = AsyncMock(side_effect=[True, False])
        pings = TranscriptPings(engine=cast(DatabaseEngine, engine), reader=_ANN)
        session = uuid4()

        heard = [
            await pings.hears(session, each) for each in ("open", "open", "private")
        ]

        assert heard == [True, True, False]
        assert engine.conn.fetchval.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("keep_sec", "most_kept"),
        [(0.0, 4_096), (30.0, 1)],
        ids=["kept no time", "kept one"],
    )
    async def test_an_answer_no_longer_kept_is_asked_again(
        self,
        keep_sec: float,
        most_kept: int,
    ) -> None:
        engine = FakeEngine()
        engine.conn.fetchval = AsyncMock(return_value=True)
        pings = TranscriptPings(
            engine=cast(DatabaseEngine, engine),
            reader=_ANN,
            keep_sec=keep_sec,
            most_kept=most_kept,
        )
        first, second = uuid4(), uuid4()

        for session in (first, second, first):
            assert await pings.hears(session, "open")

        assert engine.conn.fetchval.await_count == 3


class TestMayHide:
    @pytest.mark.parametrize("audience", ["open", "private"])
    def test_an_owner_hides_and_shows_their_session(self, audience: str) -> None:
        owners = ("ann@lab.example", "")
        assert may_hide("Ann@Lab.example", owners=owners, audience=audience)

    def test_nobody_else_and_no_staff_session(self) -> None:
        owners = ("ann@lab.example", "key@lab.example")

        assert may_hide("key@lab.example", owners=owners, audience="open")
        assert not may_hide("bob@lab.example", owners=owners, audience="open")
        assert not may_hide("ann@lab.example", owners=owners, audience="staff")
        assert not may_hide("ann@lab.example", owners=owners, audience=None)

    def test_an_unnamed_owner_is_no_one(self) -> None:
        """A session no key opened has no opener, which no caller matches."""
        assert not may_hide("", owners=("ann@lab.example", ""), audience="open")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
