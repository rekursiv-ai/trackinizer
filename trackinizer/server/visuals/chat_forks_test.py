"""Tests for who continues a science chat and who forks it."""

from __future__ import annotations

import pytest

from trackinizer.server.visuals.chat_forks import (
    CONSUMER_DOMAINS,
    may_continue,
    org_of,
)


def test_an_organisation_is_the_domain_of_a_verified_email() -> None:
    assert org_of("Ada@Example.COM") == "example.com"
    assert org_of("grace@lab.example.com") == "lab.example.com"
    assert org_of("not-an-email") is None
    assert org_of("nobody@") is None


@pytest.mark.parametrize("domain", sorted(CONSUMER_DOMAINS))
def test_a_consumer_mail_domain_is_no_organisation(domain: str) -> None:
    assert org_of(f"ada@{domain}") is None


def test_the_consumer_domains_are_these_and_no_others() -> None:
    assert sorted(CONSUMER_DOMAINS) == [
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "me.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
    ]


@pytest.mark.parametrize(
    ("poster", "starter", "continues"),
    [
        ("grace@example.com", "ada@example.com", True),
        ("grace@EXAMPLE.com", "ada@example.com", True),
        ("jan@other.org", "ada@example.com", False),
        ("grace@lab.example.com", "ada@example.com", False),
        # No consumer domain is an organisation, so two people on one never share one.
        ("bob@gmail.com", "alice@gmail.com", False),
        ("bob@gmail.com", "ada@example.com", False),
        ("ada@example.com", "alice@gmail.com", False),
        # Whoever started a chat goes on in it.
        ("alice@gmail.com", "alice@gmail.com", True),
        ("Alice@Gmail.com", "alice@gmail.com", True),
    ],
)
def test_a_person_continues_a_chat_of_their_own_organisation_and_forks_others(
    poster: str,
    starter: str,
    continues: bool,
) -> None:
    assert may_continue(poster, starter=starter, orgs="domain") is continues


def test_a_server_of_one_organisation_is_continued_by_anyone() -> None:
    assert may_continue("jan@other.org", starter="ada@example.com", orgs="single")
    assert may_continue("bob@gmail.com", starter="alice@gmail.com", orgs="single")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
