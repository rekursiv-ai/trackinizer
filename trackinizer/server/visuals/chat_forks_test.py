"""Tests for who posts into a science chat, who shares it and who deletes it."""

from __future__ import annotations

import pytest

from trackinizer.server.visuals.chat_forks import (
    ChatClosedError,
    ChatRefusal,
    ChatRefusedError,
    delete_refusal,
    may_continue,
    may_share,
)


@pytest.mark.parametrize(
    ("caller", "org", "allowed"),
    [
        ("Ada@Example.com", None, True),
        ("Grace@Example.com", None, True),
        # Reading the chat, as everyone on a single server does, is no leave to post.
        ("linus@example.com", None, False),
        # A domain server's organisation of the starter posts, as it reads.
        ("linus@example.com", "example.com", True),
        ("jan@other.org", "other.org", False),
    ],
)
def test_its_starter_shares_and_a_domain_servers_organisation_post_into_a_chat(
    caller: str,
    org: str | None,
    allowed: bool,
) -> None:
    assert (
        may_continue(
            caller,
            starter="Ada@Example.com",
            shared_with=("grace@example.com",),
            org=org,
        )
        is allowed
    )


@pytest.mark.parametrize(
    ("caller", "admin", "shared", "allowed"),
    [
        ("Ada@Example.com", False, True, True),
        ("ada@example.com", False, False, True),
        # A share lets its person read the chat, which an admin does not.
        ("root@example.com", True, True, False),
        ("root@example.com", True, False, True),
        ("grace@example.com", False, True, False),
        ("grace@example.com", False, False, False),
    ],
)
def test_its_starter_shares_a_chat_and_an_admin_only_stops_sharing_it(
    caller: str,
    admin: bool,
    shared: bool,
    allowed: bool,
) -> None:
    assert (
        may_share(caller, starter="ada@example.com", admin=admin, shared=shared)
        is allowed
    )


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


def test_a_closure_carries_its_code() -> None:
    error = ChatClosedError("chat_staff_only")

    assert (error.code, str(error)) == ("chat_staff_only", "chat_staff_only")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
