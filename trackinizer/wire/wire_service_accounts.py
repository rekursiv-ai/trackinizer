"""Wire contract for service accounts: identities that are programs, not people.

A service account is a user an admin creates without a sign-in, for a program that needs
its own identity and key. Its address is ``NAME@service.invalid``; ``.invalid`` is
reserved for names that never resolve, so no identity provider issues it and no sign-in
can claim it. It is a viewer or a writer and never an admin. The program authenticates
with API keys an admin mints for it, and the account can mint none itself.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from trackinizer.wire.audience import AudienceName


__all__ = [
    "NAME_PATTERN",
    "SERVICE_ACCOUNTS_PATH",
    "SERVICE_ACCOUNT_KEY_REVOKE_PATH",
    "SERVICE_ACCOUNT_TOKENS_PATH",
    "SERVICE_DOMAIN",
    "ServiceAccount",
    "ServiceAccountCreate",
    "ServiceRole",
    "ServiceToken",
    "ServiceTokenCreate",
    "ServiceTokenCreated",
    "ServiceTokenList",
    "TokenRole",
    "is_service_address",
    "service_address",
]


SERVICE_DOMAIN: Final = "service.invalid"
"""The domain of every service account's address, reserved by RFC 2606."""

NAME_PATTERN: Final = r"^[a-z][a-z0-9-]{1,30}$"
"""A service account's name, the part of its address before the ``@``."""

SERVICE_ACCOUNTS_PATH: Final = "/api/admin/service-accounts"
SERVICE_ACCOUNT_TOKENS_PATH: Final = "/api/admin/service-accounts/{user_id}/tokens"
SERVICE_ACCOUNT_KEY_REVOKE_PATH: Final = (
    "/api/admin/service-accounts/{user_id}/tokens/{key_id}/revoke"
)

type ServiceRole = Literal["viewer", "writer"]
"""The roles a service account may hold; never ``admin``."""

type TokenRole = Literal["viewer", "writer", "admin"]
"""A key's role ceiling. One above its account's role is refused."""


def service_address(name: str) -> str:
    """Return the address of the service account called ``name``."""
    return f"{name}@{SERVICE_DOMAIN}"


def is_service_address(email: str) -> bool:
    """Say whether an address, or an allowlist pattern, is at the service domain.

    No sign-in may admit such an address: it names a service account or no one.

    Args:
      email: An address or pattern, in any case and with any padding.

    Returns:
      service: Whether it ends in ``@service.invalid``.

    """
    return email.strip().lower().endswith(f"@{SERVICE_DOMAIN}")


class ServiceAccountCreate(BaseModel):
    """Create a service account."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    """The account's name; its address is ``<name>@service.invalid``."""

    role: ServiceRole
    """The most any key of the account may do."""


class ServiceAccount(BaseModel):
    """A service account, in the shape ``GET /api/admin/users`` lists a user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """The user id."""

    email: str
    """The account's address."""

    name: str
    """The account's name."""

    role: ServiceRole
    """The account's role."""

    status: Literal["active", "disabled"]
    """Whether the account's keys authenticate."""

    created_at: str
    """When it was created, ISO 8601."""

    last_login: str | None
    """Always ``None``: a service account never signs in."""

    staff: bool
    """Whether the server's staff setting names the account."""


class ServiceTokenCreate(BaseModel):
    """Mint a key for a service account."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    """A label for the key."""

    role: TokenRole | None = None
    """The key's role; the account's role when left out, and at most that."""

    open_into: AudienceName | None = None
    """The one audience the key opens sessions into; none for no pin."""


class ServiceToken(BaseModel):
    """One key of a service account, as listed; never its secret or hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    prefix: str
    """The first characters of the secret, to recognise the key by."""

    role: TokenRole
    open_into: str | None
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class ServiceTokenCreated(BaseModel):
    """A freshly minted key, with its secret, which no later answer repeats."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    prefix: str
    role: TokenRole
    open_into: str | None
    secret: str
    """The plaintext key, shown once; the server keeps only its hash."""


class ServiceTokenList(BaseModel):
    """Every key of a service account, newest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens: list[ServiceToken]
