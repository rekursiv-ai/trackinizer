"""Wire contract for device login: a CLI gets an API key by a code a person approves.

A program that cannot open a browser asks for a code, prints it, and polls. The
person opens the verification page, signs in, checks who is asking and approves. The
next poll returns an API key once; the server keeps no copy of the secret. When the
request named a machine, the approval also makes it the approver's own confirmed
personal machine, and the same reply carries the one-use token the host joins with.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from trackinizer.wire.wire_machines import NAME_PATTERN
from trackinizer.wire.wire_service_accounts import TokenRole


__all__ = [
    "DEVICE_APPROVE_PATH",
    "DEVICE_CODE_PATH",
    "DEVICE_CODE_TTL_SEC",
    "DEVICE_DENY_PATH",
    "DEVICE_LOOKUP_PATH",
    "DEVICE_PAGE_PATH",
    "DEVICE_POLL_PATH",
    "DEVICE_POLL_SEC",
    "USER_CODE_ALPHABET",
    "USER_CODE_LENGTH",
    "WRONG_ENTRIES_LOCK",
    "DeviceCode",
    "DeviceCodeRequest",
    "DeviceDecision",
    "DeviceLoginView",
    "DeviceStatus",
    "DeviceToken",
    "DeviceTokenRequest",
    "IssuedKey",
    "format_user_code",
    "normalize_user_code",
]


DEVICE_PAGE_PATH: Final = "/auth/device"
DEVICE_CODE_PATH: Final = "/auth/device/code"
DEVICE_POLL_PATH: Final = "/auth/device/token"
DEVICE_APPROVE_PATH: Final = "/auth/device/approve"
DEVICE_DENY_PATH: Final = "/auth/device/deny"
DEVICE_LOOKUP_PATH: Final = "/auth/device/lookup"

DEVICE_CODE_TTL_SEC: Final = 600
"""How long a code stays open for approval, in seconds."""

DEVICE_POLL_SEC: Final = 5
"""The least gap between two polls; a sooner poll is told to slow down."""

WRONG_ENTRIES_LOCK: Final = 5
"""Wrong codes one person may enter within a code's lifetime before every entry is refused."""

USER_CODE_ALPHABET: Final = "BCDFGHJKLMNPQRSTVWXZ"
"""The characters of a code: consonants only, so no word and no 0/O or 1/I mix-ups."""

USER_CODE_LENGTH: Final = 8
"""How many characters a code has, shown as two groups of four."""

type DeviceStatus = Literal[
    "pending",
    "slow_down",
    "denied",
    "expired_token",
    "approved",
]
"""``pending`` and ``slow_down`` ask the poller to wait; ``denied`` and ``expired_token``
end it; ``approved`` carries the key."""


def normalize_user_code(text: str) -> str:
    """Return the stored form of a typed code: upper case, without dashes or spaces.

    Args:
      text: What a person typed or a link carried, such as ``bcdf-ghjk``.

    Returns:
      code: The code as the server stores it, such as ``BCDFGHJK``.

    """
    return re.sub(r"[\s-]", "", text).upper()


def format_user_code(code: str) -> str:
    """Return a stored code in the form people read: ``BCDF-GHJK``."""
    half = USER_CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


class DeviceCodeRequest(BaseModel):
    """Ask for a code: which program is asking, from where, for which machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    """The program, such as ``rai-shell``; the page names it and the key is named for it."""

    host: str = Field(default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,62})?$")
    """The computer's host name, shown on the page and in the key's name."""

    machine: str | None = Field(default=None, pattern=NAME_PATTERN)
    """A machine to enroll as the approver's own; ``None`` asks for a key alone."""


class DeviceCode(BaseModel):
    """The code a program shows, and the secret it polls with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_code: str = Field(repr=False)
    """The poll secret; the server keeps only a hash of it."""

    user_code: str
    """What the person checks and approves, such as ``BCDF-GHJK``."""

    verification_uri: str
    """The page where the person enters the code."""

    verification_uri_complete: str
    """The same page with the code filled in."""

    expires_in: int
    """Seconds until the code stops working."""

    interval: int
    """The least seconds between two polls."""


class DeviceTokenRequest(BaseModel):
    """Poll for the outcome of a code."""

    # Without this, the error for an over-long code quotes the code.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    device_code: str = Field(max_length=128, repr=False)


class IssuedKey(BaseModel):
    """The key an approval produced, shown once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: str = Field(repr=False)
    """The API key's secret; the server keeps only a hash of it."""

    role: TokenRole
    """The key's role: a writer, or less when its approver holds less."""

    email: str
    """Who approved, and so whose key it is."""

    machine_token: str = Field(default="", repr=False)
    """The one-use enrollment token for the requested machine; empty when none was."""


class DeviceToken(BaseModel):
    """The answer to a poll."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DeviceStatus

    issued: IssuedKey | None = None
    """Present exactly when ``status`` is ``approved``."""

    @model_validator(mode="after")
    def _key_only_when_approved(self) -> Self:
        """Refuse an approval with no key and a key with no approval."""
        if (self.issued is not None) != (self.status == "approved"):
            raise ValueError("a key comes with an approval, and only with one")
        return self


class DeviceDecision(BaseModel):
    """A person's approval or denial of the code they were shown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_code: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    """The code as typed; case, dashes and spaces are ignored."""


class DeviceLoginView(BaseModel):
    """What the approval page shows a signed-in person about the code they entered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_code: str
    """The code, as people read it."""

    client: str
    """The program that asked."""

    host: str
    """The computer it runs on; empty when it did not say."""

    machine: str | None
    """The machine the approval would enroll as theirs, or ``None``."""

    role: TokenRole
    """The role the key would hold: a writer, or less when they hold less."""
