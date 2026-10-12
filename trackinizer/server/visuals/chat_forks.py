"""Who continues a science chat and who forks it; who shares it and who deletes it.

A science chat is its starter's. They continue it by typing in it, and so do the people
they share it with. Who else does is the server's setting: the starter's organisation
(``domain``), the people whose verified email has the starter's domain; everyone
(``single``); or nobody (``individual``). Anyone else forks it: typing starts a new chat
of their own from the chat's latest line, and the original is never posted into.

A consumer mail domain is no organisation, since its users have nothing in common, so
under ``domain`` those users fork every chat that is not their own or shared with them.

The starter shares their chat and stops sharing it, and so does an admin. One rule
decides who deletes: you delete what you made, admins delete anything. The starter
deletes their chat while every person's line in it is theirs; once someone else has
posted, only an admin, or the key that opened it, does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal


if TYPE_CHECKING:
    from collections.abc import Collection

    from trackinizer.server.config import ChatOrgs


__all__ = [
    "CONSUMER_DOMAINS",
    "ChatRefusal",
    "ChatRefusedError",
    "ConversationTakenError",
    "ForeignChatError",
    "delete_refusal",
    "may_continue",
    "may_share",
    "org_of",
]


CONSUMER_DOMAINS: Final = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
    },
)
"""Mail domains open to anyone, which never make their users one organisation."""


type ChatRefusal = Literal["chat_not_starter", "chat_has_other_posters"]
"""Why a caller may not share or delete a chat: they did not start it, or they did and
someone else has posted in it since."""


class ForeignChatError(Exception):
    """A person who may not continue a chat tried to post into it."""


class ConversationTakenError(Exception):
    """A fork was asked to start as a conversation someone else already began."""


class ChatRefusedError(Exception):
    """A caller may not share or delete a chat; ``code`` says why."""

    def __init__(self, code: ChatRefusal) -> None:
        super().__init__(code)
        self.code: ChatRefusal = code


def org_of(email: str) -> str | None:
    """Return the organisation an email belongs to: its domain, unless a consumer one.

    Args:
      email: A verified email address.

    Returns:
      org: The lowercase domain; ``None`` for a consumer domain or text with no
        domain.

    """
    _, at, domain = email.lower().rpartition("@")
    return None if not at or domain in CONSUMER_DOMAINS else domain or None


def may_continue(
    poster: str,
    *,
    starter: str,
    shared_with: Collection[str],
    orgs: ChatOrgs,
) -> bool:
    """Return whether ``poster`` may type into the chat ``starter`` began.

    Args:
      poster: The attested email of the person typing.
      starter: The email of the person who started the chat.
      shared_with: The lowercase emails the starter shared the chat with.
      orgs: Who else continues a chat.

    Returns:
      continues: True for the starter, for the people the chat is shared with, for
        anyone at all on a ``single`` server, and on a ``domain`` one for those in
        the starter's organisation.

    """
    person = poster.lower()
    if orgs == "single" or person == starter.lower() or person in shared_with:
        return True
    if orgs == "individual":
        return False
    org = org_of(person)
    return org is not None and org == org_of(starter)


def may_share(caller: str, *, starter: str, admin: bool) -> bool:
    """Return whether ``caller`` may share the chat ``starter`` began, or unshare it."""
    return admin or caller.lower() == starter.lower()


def delete_refusal(
    caller: str,
    *,
    starter: str,
    admin: bool,
    opened: bool,
    others_posted: bool,
) -> ChatRefusal | None:
    """Return why ``caller`` may not delete the chat ``starter`` began, if they may not.

    Args:
      caller: The email of the person asking.
      starter: The email of the person who started the chat.
      admin: Whether the caller is an admin.
      opened: Whether the caller's key opened every session of the chat.
      others_posted: Whether a person's line in it is not the starter's, a line a
        fork copied counting as its original poster's.

    Returns:
      refusal: None when the caller may delete it.

    """
    if admin or opened:
        return None
    if caller.lower() != starter.lower():
        return "chat_not_starter"
    return "chat_has_other_posters" if others_posted else None
