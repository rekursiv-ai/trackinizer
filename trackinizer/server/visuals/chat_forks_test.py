"""Tests for who continues a science chat and who forks it, shares it and deletes it."""

from __future__ import annotations

import pytest

from trackinizer.server.visuals.chat_forks import (
    CONSUMER_DOMAINS,
    ChatRefusal,
    ChatRefusedError,
    delete_refusal,
    may_continue,
    may_share,
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
    assert may_continue(poster, starter=starter, shared_with=(), orgs="domain") is (
        continues
    )


def test_a_server_of_one_organisation_is_continued_by_anyone() -> None:
    for poster, starter in (
        ("jan@other.org", "ada@example.com"),
        ("bob@gmail.com", "alice@gmail.com"),
    ):
        assert may_continue(poster, starter=starter, shared_with=(), orgs="single")


def test_an_individual_chat_is_continued_by_its_starter_and_who_it_is_shared_with() -> (
    None
):
    assert _individual("Ada@Example.com")
    assert _individual("Grace@Example.com", "grace@example.com")
    # A colleague of the starter's domain forks it too, until it is shared with them.
    assert not _individual("grace@example.com")
    assert not _individual("jan@other.org", "grace@example.com")


def test_a_share_lets_a_stranger_continue_a_chat_of_another_organisation() -> None:
    assert may_continue(
        "jan@other.org",
        starter="ada@example.com",
        shared_with=("jan@other.org",),
        orgs="domain",
    )
    assert may_continue(
        "bob@gmail.com",
        starter="alice@gmail.com",
        shared_with=("bob@gmail.com",),
        orgs="domain",
    )


def test_its_starter_and_an_admin_share_a_chat_and_nobody_else_does() -> None:
    assert may_share("Ada@Example.com", starter="ada@example.com", admin=False)
    assert may_share("root@example.com", starter="ada@example.com", admin=True)
    assert not may_share("grace@example.com", starter="ada@example.com", admin=False)


@pytest.mark.parametrize(
    ("caller", "admin", "opened", "others_posted", "refusal"),
    [
        # The starter deletes a chat only they posted in, and not one someone joined.
        ("Ada@Example.com", False, False, False, None),
        ("ada@example.com", False, False, True, "chat_has_other_posters"),
        # An admin, and the key that opened it, delete it whoever posted.
        ("root@example.com", True, False, True, None),
        ("kb@service.invalid", False, True, True, None),
        # Anyone else is no starter.
        ("grace@example.com", False, False, False, "chat_not_starter"),
        ("grace@example.com", False, False, True, "chat_not_starter"),
    ],
)
def test_who_deletes_a_chat_is_who_made_it_and_admins(
    caller: str,
    admin: bool,
    opened: bool,
    others_posted: bool,
    refusal: ChatRefusal | None,
) -> None:
    assert (
        delete_refusal(
            caller,
            starter="ada@example.com",
            admin=admin,
            opened=opened,
            others_posted=others_posted,
        )
        == refusal
    )


def test_a_refusal_carries_its_code() -> None:
    error = ChatRefusedError("chat_has_other_posters")

    assert (error.code, str(error)) == (
        "chat_has_other_posters",
        "chat_has_other_posters",
    )


def _individual(poster: str, *shared_with: str) -> bool:
    """Say whether ``poster`` continues Ada's chat on an ``individual`` server."""
    return may_continue(
        poster,
        starter="ada@example.com",
        shared_with=shared_with,
        orgs="individual",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
