"""Who continues a science chat and who forks it, by organisation.

A science chat belongs to its starter's organisation: the people whose verified email
has the starter's domain. They continue it by typing in it. Anyone else forks it: typing
starts a new chat of their own from the chat's latest line, and the original is never
posted into. A person always continues their own chat.

A consumer mail domain is no organisation, since its users have nothing in common, so
those users fork every chat that is not their own. A server configured ``single`` is one
organisation, whoever signs in, and nothing forks by typing there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final


if TYPE_CHECKING:
    from trackinizer.server.config import ChatOrgs


__all__ = [
    "CONSUMER_DOMAINS",
    "ConversationTakenError",
    "ForeignChatError",
    "may_continue",
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


class ForeignChatError(Exception):
    """A person outside a chat's organisation tried to post into it."""


class ConversationTakenError(Exception):
    """A fork was asked to start as a conversation someone else already began."""


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


def may_continue(poster: str, *, starter: str, orgs: ChatOrgs) -> bool:
    """Return whether ``poster`` may type into the chat ``starter`` began.

    Args:
      poster: The attested email of the person typing.
      starter: The email of the person who started the chat.
      orgs: How the server groups its users.

    Returns:
      continues: True for the starter, for anyone at all on a ``single`` server, and
        otherwise for those in the starter's organisation.

    """
    if orgs == "single" or poster.lower() == starter.lower():
        return True
    org = org_of(poster)
    return org is not None and org == org_of(starter)
