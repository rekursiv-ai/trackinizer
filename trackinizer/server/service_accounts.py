"""What a service account may be handed, and the pin a key minted for it carries.

A service account holds the audiences :func:`person_audiences` gives its address: ``open``,
and ``staff`` only through an exact entry in the staff setting, since it has no hosted
domain for a ``*@domain`` pattern to need. An admin creates the account and mints its
keys, and a key never grants more than it holds: an admin hands on at most the audiences
its own credential reads and opens into, however the account's address reads. Staff stays
the operator's setting, and no admin turns a service account into staff by making one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.audience import key_audiences
from trackinizer.server.auth import (
    AudienceCeilingError,
    AuthIdentity,
    mint_open_into,
)
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from trackinizer.server.config import Assistant


__all__ = ["require_assistant_grant", "require_grantable", "service_token_pin"]


def require_grantable(
    identity: AuthIdentity,
    *,
    reads: frozenset[str],
    opens: frozenset[str],
) -> None:
    """Refuse a grant that reads or opens into more than ``identity`` does.

    Args:
      identity: The credential that grants.
      reads: The audiences the grantee reads.
      opens: The audiences the grantee opens sessions into.

    Raises:
      AudienceCeilingError: The grantee reads or opens an audience the caller does not.

    """
    if not identity.read_audiences.issuperset(reads):
        raise AudienceCeilingError(
            f"the grant reads {sorted(reads)}, beyond the caller's "
            f"{sorted(identity.read_audiences)}",
        )
    if not identity.open_audiences.issuperset(opens):
        raise AudienceCeilingError(
            f"the grant opens into {sorted(opens)}, beyond the caller's "
            f"{sorted(identity.open_audiences)}",
        )


def require_assistant_grant(
    identity: AuthIdentity,
    *,
    email: str,
    assistant: Assistant | None,
) -> None:
    """Refuse handing the assistant's account to a caller who lacks every audience.

    The assistant is recognised by address alone, whatever limits its key carries, and
    it relays into sessions of every audience. Making it, or minting its key, is
    therefore a grant of all of them.

    Args:
      identity: The credential that grants.
      email: The address of the account.
      assistant: The configured assistant, if any.

    Raises:
      AudienceCeilingError: ``email`` is the assistant's and the caller lacks an audience.

    """
    if assistant is not None and email == assistant.email:
        require_grantable(identity, reads=AUDIENCES, opens=AUDIENCES)


def service_token_pin(
    account: frozenset[str],
    *,
    minter: AuthIdentity,
    open_into: str | None,
) -> str | None:
    """Return the pin a key minted for a service account is stored with.

    The pin follows :func:`mint_open_into` for the minter: a pinned key hands its pin
    on when the request names none. It then stays inside the account, so a key never
    names an audience its account does not hold. Last, what the key would read and
    open once stored must lie inside what the minter reads and opens, which an unpinned
    key for a staff account would not when the minter is not staff.

    Args:
      account: The audiences the account holds.
      minter: The credential that presented the mint request.
      open_into: The requested pin, or ``None`` for no pin.

    Returns:
      pin: The audience to pin the key to, or ``None``.

    Raises:
      AudienceCeilingError: The pin names an audience the account does not hold, or
        the key would reach beyond the minter.

    """
    pin = mint_open_into(minter, open_into=open_into)
    if pin is not None and pin not in account:
        raise AudienceCeilingError(
            f"requested pin {pin!r} exceeds the audiences the account opens into, "
            f"{sorted(account)}",
        )
    reach = key_audiences(account, open_into=pin)
    require_grantable(minter, reads=reach.read, opens=reach.opening)
    return pin
