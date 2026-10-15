"""Who shares a science chat and who deletes it.

A science chat is its starter's. Who else reads it, and so posts in it and forks it,
is :func:`~trackinizer.server.audience.visible_sql`'s rule, the one every read of
a transcript takes: the people the starter shares it with, and as the server's
organisation mode says, the starter's organisation (``domain``) or everyone
(``single``). Anyone else neither reads it nor posts in it, and so cannot fork it
either: a fork opens with copies of its original's lines. A reader forks it only
when the fork, open to its own starter's organisation, reaches no one the chat
does not: under ``domain``, a person it is shared with from another organisation
posts in it and does not fork it.

The starter shares their chat and stops sharing it. An admin only stops sharing it: a
share lets its person read the chat, and an admin reads no one else's. One rule
decides who deletes: you delete what you made, admins delete anything. The starter
deletes their chat while every person's line in it is theirs; once someone else has
posted, only an admin, or the key that opened it, does.
"""

from __future__ import annotations

from typing import Literal


__all__ = [
    "ChatClosedError",
    "ChatClosure",
    "ChatRefusal",
    "ChatRefusedError",
    "ConversationTakenError",
    "ForeignChatError",
    "delete_refusal",
    "may_share",
]


type ChatRefusal = Literal["chat_not_starter", "chat_has_other_posters"]
"""Why a caller may not share or delete a chat: they did not start it, or they did and
someone else has posted in it since."""


class ForeignChatError(Exception):
    """A person who may not read a chat tried to post into it."""


class ConversationTakenError(Exception):
    """A fork was asked to start as a conversation someone else already began."""


type ChatClosure = Literal["chat_fork_closed", "chat_fork_wider", "chat_staff_only"]
"""Why a chat's audience refuses what its reader asks: a ``private`` or ``staff`` chat
is forked by no one, since a fork's copies would not keep its audience; one is
forked by no one whose fork its organisation would read and the chat's readers do
not include; a ``staff`` chat is shared with no one who does not hold ``staff``."""


class ChatRefusedError(Exception):
    """A caller may not share or delete a chat; ``code`` says why."""

    def __init__(self, code: ChatRefusal) -> None:
        super().__init__(code)
        self.code: ChatRefusal = code


class ChatClosedError(Exception):
    """A chat's audience refuses a fork or a share; ``code`` says which."""

    def __init__(self, code: ChatClosure) -> None:
        super().__init__(code)
        self.code: ChatClosure = code


def may_share(caller: str, *, starter: str, admin: bool, shared: bool) -> bool:
    """Return whether ``caller`` may share the chat ``starter`` began, or unshare it.

    A share lets its person read the chat, so only the starter grants one. An admin
    stops sharing it but never shares it, with anyone, itself included.

    Args:
      caller: The email of the person asking.
      starter: The email of the person who started the chat.
      admin: Whether the caller is an admin.
      shared: Share it; False stops sharing it.

    Returns:
      allowed: True for the starter, and for an admin stopping a share.

    """
    return caller.lower() == starter.lower() or (admin and not shared)


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
