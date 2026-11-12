"""Host credentials: minting, parsing, and the dependency host routes use.

A host holds one host credential, ``trax_machine_<32 hex id>_<43 char secret>``,
and an admin hands it a one-use enrollment token, ``enr_<32 hex id>_<43 char
secret>``, to get it. The id names the row and the secret is 256 random bits, of
which only a SHA-256 digest is stored. A digest of that much entropy cannot be
brute-forced, so unlike an API key there is no scrypt step and no verified-bearer
cache: a lookup is one indexed read, and a revoke applies on the next request.

:func:`current_host` is separate from :func:`auth.require_role` and shares no
state with it. A host credential never authenticates a user route (``auth``
answers 401 before any lookup), and an API key never authenticates a host route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, cast
from uuid import UUID, uuid4

import hashlib
import re
import secrets

from fastapi import HTTPException, Request
from treekle import from_plain

from trackinizer.server import auth
from trackinizer.server.api._routes_shared import request_engine
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_daemon import (
    CREDENTIAL_PREFIX,
    ENROLLMENT_PREFIX,
)


if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

    from trackinizer.lib.postgres import Conn


__all__ = [
    "HostIdentity",
    "MintedSecret",
    "ParsedSecret",
    "current_host",
    "host_revoked",
    "mint_secret",
    "parse_credential",
    "parse_enrollment",
    "request_host",
    "should_bump_last_used",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class HostIdentity:
    """The host a request's credential belongs to."""

    host_id: UUID
    name: str
    credential_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class MintedSecret:
    """A fresh bearer string, and what the server stores of it."""

    id: UUID
    token: str = field(repr=False)
    digest: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedSecret:
    """The id and digest of a presented bearer string."""

    id: UUID
    digest: bytes


# At most this many credential ids are tracked for the ``last_used`` throttle; one
# live credential exists per host, so the cap only bounds a long-lived process.
_BUMPED_MAX_ENTRIES: Final = 1_024

_CREDENTIAL: Final = re.compile(
    rf"{re.escape(CREDENTIAL_PREFIX)}([0-9a-f]{{32}})_([A-Za-z0-9_-]{{43}})",
)
_ENROLLMENT: Final = re.compile(
    rf"{re.escape(ENROLLMENT_PREFIX)}([0-9a-f]{{32}})_([A-Za-z0-9_-]{{43}})",
)


def mint_secret(prefix: str) -> MintedSecret:
    """Mint a bearer string ``<prefix><32 hex id>_<43 char secret>``.

    Args:
      prefix: :data:`CREDENTIAL_PREFIX` or :data:`ENROLLMENT_PREFIX`.

    Returns:
      secret: Its row id, plaintext token (shown once) and stored digest.

    """
    secret_id = uuid4()
    secret = secrets.token_urlsafe(32)
    return MintedSecret(
        id=secret_id,
        token=f"{prefix}{secret_id.hex}_{secret}",
        digest=_digest(secret),
    )


def parse_credential(text: str) -> ParsedSecret | None:
    """Parse a host credential; ``None`` when it has any other shape."""
    return _parse(_CREDENTIAL, text)


def parse_enrollment(text: str) -> ParsedSecret | None:
    """Parse an enrollment token; ``None`` when it has any other shape."""
    return _parse(_ENROLLMENT, text)


def should_bump_last_used(
    bumped: dict[UUID, float],
    credential_id: UUID,
    *,
    now: float,
) -> bool:
    """Return True when ``credential_id``'s ``last_used`` is due, and record it.

    A host calls every few seconds; writing the column each time would turn the
    credential row into a hot row, so it is refreshed once per interval.

    Args:
      bumped: When each credential was last written, on the monotonic clock.
      credential_id: The credential that just authenticated.
      now: The monotonic clock's reading.

    Returns:
      due: True for the first request and any one past the interval.

    """
    last = bumped.get(credential_id)
    if last is not None and now - last < auth.LAST_USED_BUMP_INTERVAL_SEC:
        return False
    if len(bumped) >= _BUMPED_MAX_ENTRIES:
        bumped.clear()
    bumped[credential_id] = now
    return True


async def current_host(request: Request, host_id: UUID) -> HostIdentity:
    """Authenticate an HTTP host route by its host credential; see :func:`request_host`."""
    return await request_host(request, host_id)


# Separate from ``current_host`` for the reason ``auth.request_user`` is separate from
# ``current_user``: the dependency's annotation must stay importable at runtime.
async def request_host(request: HTTPConnection, host_id: UUID) -> HostIdentity:
    """Authenticate a host route or WebSocket by the host credential in the bearer.

    A malformed, unknown or wrong-secret credential is 401 with one body, so a
    caller learns nothing about which ids exist. Only a credential whose secret
    matches reaches the revoked check, which is 410 ``host_revoked``: the answer
    is for a holder of a once-valid secret and nobody else. A valid credential used
    on another host's path is 404, since 403 would confirm the id exists.

    Args:
      request: Request or WebSocket carrying the database engine and the Authorization.
      host_id: The host in the route's path.

    Returns:
      identity: The host and credential the request authenticated as.

    Raises:
      HTTPException: 401 for an unknown credential; 410 for a revoked one.
      NotFoundError: The credential belongs to another host.

    """
    parsed = parse_credential(auth.extract_bearer(request) or "")
    if parsed is None:
        raise _unknown()
    async with request_engine(request).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.secret_sha256, c.revoked_at, m.id AS host_id, m.name "
            "FROM host_credentials c JOIN hosts m ON m.id = c.host_id "
            "WHERE c.id = $1",
            parsed.id,
        )
        if row is None or not secrets.compare_digest(
            from_plain(row["secret_sha256"], bytes),
            parsed.digest,
        ):
            raise _unknown()
        if row["revoked_at"] is not None:
            raise host_revoked()
        if from_plain(row["host_id"], UUID) != host_id:
            raise NotFoundError("host is not registered")
        if should_bump_last_used(
            _bumped(request),
            parsed.id,
            now=auth.monotonic_clock(),
        ):
            await _touch(conn, parsed.id)
    return HostIdentity(
        host_id=host_id,
        name=from_plain(row["name"], str),
        credential_id=parsed.id,
    )


def host_revoked() -> HTTPException:
    """Return the 410 a host gets for a credential that was revoked."""
    return HTTPException(status_code=410, detail="host_revoked")


def _parse(pattern: re.Pattern[str], text: str) -> ParsedSecret | None:
    """Split a bearer string into its row id and secret digest."""
    match = pattern.fullmatch(text)
    if match is None:
        return None
    return ParsedSecret(id=UUID(hex=match[1]), digest=_digest(match[2]))


def _digest(secret: str) -> bytes:
    """Return the SHA-256 of a secret, the form the tables store."""
    return hashlib.sha256(secret.encode("ascii")).digest()


def _unknown() -> HTTPException:
    """Return the one 401 every unrecognised credential gets."""
    return HTTPException(status_code=401, detail="invalid host credential")


async def _touch(conn: Conn, credential_id: UUID) -> None:
    """Stamp a credential's ``last_used`` with the database clock."""
    await conn.execute(
        "UPDATE host_credentials SET last_used = now() WHERE id = $1",
        credential_id,
    )


class _State(Protocol):
    host_credential_bumped: dict[UUID, float]


class _App(Protocol):
    state: _State


def _bumped(request: HTTPConnection) -> dict[UUID, float]:
    """Return the app's ``last_used`` throttle, creating it on first use."""
    state = cast("_App", request.app).state
    bumped = cast(
        "dict[UUID, float] | None",
        getattr(state, "host_credential_bumped", None),
    )
    if bumped is None:
        bumped = {}
        state.host_credential_bumped = bumped
    return bumped
