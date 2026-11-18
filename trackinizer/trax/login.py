"""Sign in to a server from a terminal: show a code, a person approves it in a browser.

``login`` asks the server for a device code, prints where to enter it, and polls until
the person approves. The key it gets is saved as a profile named for the server, and
made the current profile only when trax had none. ``trax profile NAME login URL`` runs
it; ``rai-shell`` runs it with the machine it is about to connect, whose one-use
enrollment token the same approval returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import re
import socket
import time

from trackinizer.client.client import Client, server_url
from trackinizer.client.errors import ClientError
from trackinizer.trax.profile import (
    Profile,
    has_usable_profile,
    read_profile,
    save_profile,
    switch_profile,
    validate_profile_name,
)
from trackinizer.trax.render import echo
from trackinizer.wire.wire_device import (
    DEVICE_CODE_PATH,
    DEVICE_POLL_PATH,
    DEVICE_POLL_SEC,
    DeviceCode,
    DeviceCodeRequest,
    DeviceToken,
    IssuedKey,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel


__all__ = ["LoginResult", "login"]


_LOOPBACK: Final = frozenset({"localhost", "127.0.0.1", "::1"})
"""Hosts a key may cross in clear text: they never leave this computer."""


@dataclass(frozen=True, kw_only=True, slots=True)
class LoginResult:
    """What a finished login leaves behind."""

    profile: str
    """The profile the key was saved under."""

    role: str
    """The key's role: a writer, or less when the approver holds less."""

    machine_token: str = field(default="", repr=False)
    """The one-use ``enr_`` token for the requested machine; empty when none was."""


def login(
    url: str,
    *,
    profile: str = "",
    machine: str | None = None,
    client: str = "trax",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> LoginResult:
    """Log in to ``url`` by a code a person approves, and save the key as a profile.

    The profile is named for the server (``trackinizer.example`` for
    ``https://trackinizer.example``) unless ``profile`` names it. It becomes the current
    profile only when trax has none: no pinned profile and no ``default`` one.

    Args:
      url: The server's URL.
      profile: The profile name to save under; empty names it for the server.
      machine: A machine to enroll as the approver's own, confirmed by the approval.
      client: The program asking, shown on the approval page and in the key's name:
        ``trax`` for ``trax profile NAME login URL``, whose key trax holds; an endpoint
        such as ``rai-shell`` passes its own name.
      sleep: Waits between polls; a test replaces it.
      clock: Monotonic seconds, for the code's lifetime; a test replaces it.

    Returns:
      result: The profile's name, the key's role, and the machine's enrollment token
        when ``machine`` was named.

    Raises:
      ClientError: The URL or name is unusable, or plain ``http`` to a server that is
        not on this computer, the server is unreachable or answers
        something unexpected, the person denies the login, or the code expires first.

    """
    base = _secure_url(server_url(url, "login URL"))
    name = profile or _server_profile(base)
    validate_profile_name(name)
    request = _validated(
        DeviceCodeRequest,
        {
            "client": client,
            "host": _hostname(),
            "machine": machine,
        },
        where="login request",
    )
    with Client(base) as http:
        code = _validated(
            DeviceCode,
            http.post(DEVICE_CODE_PATH, body=request.model_dump(mode="json")),
            where=DEVICE_CODE_PATH,
        )
        echo(f"To log in to {base}, open {code.verification_uri_complete}")
        echo(f"and approve the code {code.user_code}. Waiting...")
        issued = _await_key(http, code, sleep=sleep, clock=clock)
    if machine is not None and not issued.machine_token:
        raise ClientError(f"the server enrolled no token for machine {machine!r}")
    # Asked before the save: a profile named ``default`` would answer yes by itself.
    unpinned = not has_usable_profile()
    save_profile(
        name,
        Profile(url=base, author=_saved_author(name), api_key=issued.api_key),
    )
    if unpinned:
        switch_profile(name)
    return LoginResult(
        profile=name,
        role=issued.role,
        machine_token=issued.machine_token,
    )


def _hostname() -> str:
    """Return this computer's short host name, cut to what the server accepts."""
    short = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "-", short).lstrip("._-")[:63]


# A key sent over plain http is readable on the way, and so is the link it prints, which
# a hostile hop could swap for a look-alike page.
def _secure_url(base: str) -> str:
    """Return ``base``, or refuse plain ``http`` to a server beyond this computer."""
    parts = urlsplit(base)
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ClientError(
            f"login URL {base!r} is plain http; a login over it can be read or "
            "rewritten on the way. Use https.",
        )
    return base


def _saved_author(name: str) -> str:
    """Return the audit actor the profile ``name`` already holds, or empty."""
    try:
        return read_profile(name).author
    except ClientError:
        return ""


def _server_profile(base: str) -> str:
    """Return the profile name for a server: its host, and its port when it has one."""
    parts = urlsplit(base)
    host = re.sub(r"[^A-Za-z0-9._-]", "-", parts.hostname or "")
    return host if parts.port is None else f"{host}-{parts.port}"


def _await_key(
    http: Client,
    code: DeviceCode,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> IssuedKey:
    """Poll until the person approves; raise when they deny or the code runs out."""
    deadline = clock() + code.expires_in
    interval = float(code.interval)
    while clock() < deadline:
        sleep(interval)
        token = _validated(
            DeviceToken,
            http.post(DEVICE_POLL_PATH, body={"device_code": code.device_code}),
            where=DEVICE_POLL_PATH,
        )
        if token.status == "slow_down":
            interval += DEVICE_POLL_SEC
        elif token.status == "denied":
            raise ClientError("the login was denied in the browser")
        elif token.status == "expired_token":
            raise ClientError("the code expired before it was approved; log in again")
        elif token.issued is not None:
            return token.issued
    raise ClientError("the code was not approved in time; log in again")


# A secret may sit in the payload, so a malformed one is reported without its input.
def _validated[M: BaseModel](model: type[M], payload: object, *, where: str) -> M:
    """Validate ``payload`` into ``model``, or raise a ``ClientError`` naming ``where``."""
    try:
        return model.model_validate(payload)
    except ValueError:
        raise ClientError(f"{where} returned a malformed payload") from None
