"""Who holds which audience: staff patterns, the owner's set and a key's limits."""

from __future__ import annotations

from trackinizer.server.audience import (
    Audiences,
    audiences_for,
    hidden_exists_sql,
    key_audiences,
    visible_sql,
)


_BOTH = frozenset({"open", "staff"})
_OPEN = frozenset({"open"})


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


class TestSqlFragments:
    def test_visible_binds_the_audiences_after_the_values_already_bound(self) -> None:
        params: list[object] = ["before"]

        clause = visible_sql("i.agentsession_audience", params=params, audiences=_BOTH)

        assert clause == "i.agentsession_audience = ANY($2::text[])"
        assert params == ["before", ["open", "staff"]]

    def test_hidden_binds_the_audiences_after_the_values_already_bound(self) -> None:
        params: list[object] = ["a", "b"]

        clause = hidden_exists_sql("e.session_id", params=params, audiences=_OPEN)

        assert "hidden.id = e.session_id" in clause
        assert "= ANY($3::text[])" in clause
        assert params == ["a", "b", ["open"]]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
