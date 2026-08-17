"""Authentication: secret hashing, bearer tokens, session cookies, bootstrap.

Two credential flows share one :class:`AuthIdentity` dataclass so route
handlers never branch on credential type: bearer tokens (machine clients)
and Google-OAuth browser session cookies (interactive users).

The pieces:

* :class:`AuthIdentity` -- the principal injected by ``current_user``.
  ``api_key_id`` is ``None`` for session-authed requests, a real id for
  bearer requests.
* :func:`hash_secret` / :func:`verify_secret` -- secret hashing via stdlib
  :func:`hashlib.scrypt`, chosen to avoid a new dependency. The encoded
  hash carries its own parameters: ``scrypt$n$r$p$saltB64$keyB64``.
* :func:`current_user` -- the FastAPI dependency. Bearer header wins;
  otherwise the signed session cookie. 401 on no/bad credential or
  disabled user.
* :func:`allowlist_match` -- matches an email against the allowlist for
  the OAuth callback; literal match wins over a ``*@domain`` wildcard.
* :func:`bootstrap_admin` -- seeds the first admin when ``users`` is empty.
* :func:`require_role` -- per-route minimum-role enforcement.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

import base64
import binascii
import functools
import hashlib
import logging
import os
import secrets
import time
import uuid

from fastapi import Depends, HTTPException, Request

from trackinizer.lib.postgres import Conn, DatabaseEngine
from trackinizer.lib.userdirs import data_dir
from trackinizer.server.notify import tx
from trackinizer.server.session import read_session_cookie


if TYPE_CHECKING:
    # Runtime import would cycle: Store imports bootstrap_admin from here.
    from trackinizer.server.store.core import Store


logger = logging.getLogger(__name__)


__all__ = [
    "BOOTSTRAP_ADMIN_ENV",
    "BOOTSTRAP_TOKEN_FILE_ENV",
    "ROLE_ORDER",
    "TOKEN_PREFIX_LEN",
    "AuthIdentity",
    "Role",
    "RoleCeilingError",
    "UserStatus",
    "allowlist_match",
    "bootstrap_admin",
    "create_api_key",
    "current_user",
    "effective_role",
    "generate_token",
    "hash_secret",
    "list_api_keys",
    "lookup_user_by_id",
    "require_role",
    "revoke_api_key",
    "seed_no_auth_user",
    "set_api_key_role",
    "verify_secret",
]


# scrypt cost: ~50ms per hash, within the OWASP 2023 minimum. Encoded
# into every hash so a future bump rotates per-row, leaving old rows valid.
_SCRYPT_N: Final[int] = 2**14
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SCRYPT_DKLEN: Final[int] = 32
_SCRYPT_SALT_BYTES: Final[int] = 16
_SCRYPT_MAX_N: Final[int] = 2**20
_SCRYPT_MAX_R: Final[int] = 128
_SCRYPT_MAX_P: Final[int] = 128

# Entropy bytes in a freshly minted secret; 32 gives ~43 base64 chars.
# The ``trax_`` prefix tags leaked tokens so they're greppable in logs.
_TOKEN_BYTES: int = 32  # config-globals: ignore -- token entropy size (security parameter), shared by 2 call sites
_TOKEN_LABEL: Final[str] = "trax_"  # noqa: S105 -- not a hardcoded password.

# Visible prefix stored per api_keys row. Drives UI display and scopes
# scrypt-verify to the rows sharing that prefix.
TOKEN_PREFIX_LEN: Final[int] = 12

BOOTSTRAP_ADMIN_ENV: Final[str] = "TRACKINIZER_BOOTSTRAP_ADMIN"
BOOTSTRAP_TOKEN_FILE_ENV: Final[str] = "TRACKINIZER_BOOTSTRAP_TOKEN_FILE"  # noqa: S105 -- env-var name, not a credential.

_BEARER_PREFIX: Final[str] = "Bearer "

# Throttle for ``last_used_at`` writes. Without it, a hot key (e.g. a CI
# bot) serializes every request on one row lock just to refresh a timestamp
# the UI shows at minute granularity. Each Store caps the write to one per
# key per interval. The entry cap bounds memory under a flood of distinct
# keys, evicting the oldest tracked entry when full.
LAST_USED_BUMP_INTERVAL_SEC: float = 60.0  # config-globals: ignore -- shared default consumed by store/core.py; threading would cross the module boundary
LAST_USED_BUMPED_AT_MAX_ENTRIES: int = 10_000  # config-globals: ignore -- shared default consumed by store/core.py; threading would cross the module boundary
# Module-level so tests can monkeypatch the throttle's clock.
monotonic_clock = time.monotonic


type Role = Literal["viewer", "writer", "admin"]
type UserStatus = Literal["active", "disabled"]


# Weakest-to-strongest so role comparison is an index lookup. Viewers
# read, writers mutate, admins manage users.
ROLE_ORDER: Final[tuple[Role, ...]] = (
    "viewer",
    "writer",
    "admin",
)


class RoleCeilingError(Exception):
    """Requested role exceeds the caller's effective ceiling.

    Raised when a caller tries to mint or promote a key above the weaker of
    their user role and the credential they presented; the routes turn it
    into a 403 so a writer can never grant admin and a scoped-down key can
    never self-escalate.
    """


def effective_role(user_role: Role, key_role: Role) -> Role:
    """Return the weaker of the user's role and the key's role ceiling.

    A bearer request is authorized at the minimum of the user's standing
    role and the presented key's ceiling. Session-authed requests carry no
    key, so that path just passes the user role through.
    """
    return ROLE_ORDER[min(ROLE_ORDER.index(user_role), ROLE_ORDER.index(key_role))]


# Synthetic identity returned under ``--no-auth``. Fixed UUID so audit rows
# across runs share a key; the email is obviously fake.
_NO_AUTH_USER_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NO_AUTH_EMAIL: Final[str] = "no-auth@localhost"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthIdentity:
    """The authenticated principal for one request.

    Attributes:
      user_id: ``users.id`` of the resolved principal.
      api_key_id: ``api_keys.id`` of the credential used, or ``None`` for a
        session-cookie request. Stamped onto ``change_log.api_key_id``, so a
        NULL there marks a session-authed write.
      email: Principal's email; the human-readable identity.
      role: ``viewer`` / ``writer`` / ``admin``.

    """

    user_id: uuid.UUID
    api_key_id: uuid.UUID | None
    email: str
    role: Role


def hash_secret(secret: str) -> str:
    """Hash a plaintext secret with scrypt and a random salt.

    Returns a self-describing ``scrypt$n$r$p$saltB64$keyB64`` string. The
    cost parameters are baked in per-row so a future bump rolls forward
    without invalidating old hashes; :func:`verify_secret` reads them back.
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    key = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_b64encode(salt)}${_b64encode(key)}"
    )


def verify_secret(secret: str, encoded: str) -> bool:
    """Constant-time check of ``secret`` against a :func:`hash_secret` output.

    Returns ``True`` only when ``secret`` reproduces the stored key under the
    encoded parameters. A malformed ``encoded`` returns ``False`` rather than
    raising, so a corrupted row can't crash the auth middleware.
    """
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = _b64decode(parts[4])
        expected = _b64decode(parts[5])
    except (ValueError, binascii.Error):
        return False
    if (
        n < 2
        or n > _SCRYPT_MAX_N
        or n & (n - 1) != 0
        or r < 1
        or r > _SCRYPT_MAX_R
        or p < 1
        or p > _SCRYPT_MAX_P
    ):
        return False
    candidate = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
    )
    return secrets.compare_digest(candidate, expected)


def generate_token() -> tuple[str, str]:
    """Mint a fresh API-key secret and its stored prefix.

    Returns ``(secret, prefix)``. The secret is the full plaintext token,
    shown to the user once and never stored. The prefix is its first
    :data:`TOKEN_PREFIX_LEN` chars, persisted for display and lookup scoping.
    """
    secret = _TOKEN_LABEL + secrets.token_urlsafe(_TOKEN_BYTES)
    return secret, secret[:TOKEN_PREFIX_LEN]


async def current_user(request: Request) -> AuthIdentity:
    """FastAPI dependency resolving the bearer-token or session-cookie principal.

    Tries the ``Authorization: Bearer`` header first, then the signed
    ``trackinizer_session`` cookie, else raises 401. Bearer wins when both
    are present so an operator with a live browser session and a token in a
    curl wrapper sees the token's identity; the audit row records the
    credential actually presented.

    ``request.app.state.store`` must hold the live :class:`Store` (it owns the
    engine and the ``last_used_at`` throttle). Raises 401 when no credential
    is present, or the credential is unknown, revoked, or owned by a disabled
    user.
    """
    # ``--no-auth`` collapses the resolver to a synthetic admin -- demo mode
    # only. Tests that skip the lifespan leave ``config`` unset and fall
    # through to the bearer / session paths.
    config = getattr(request.app.state, "config", None)
    if config is not None and config.auth_disabled:
        return AuthIdentity(
            user_id=_NO_AUTH_USER_ID,
            api_key_id=None,
            email=_NO_AUTH_EMAIL,
            role="admin",
        )
    # The Store carries the per-process ``last_used_at`` throttle.
    store = cast("Store", request.app.state.store)
    bearer = _try_extract_bearer(request)
    if bearer is not None:
        identity = await _resolve_identity(
            store,
            bearer,
            request_id=str(getattr(request.state, "request_id", "")),
        )
        if identity is None:
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return identity
    session_user_id = _try_read_session_user_id(request)
    if session_user_id is not None:
        identity = await _resolve_session_identity(store.engine, session_user_id)
        if identity is not None:
            return identity
    raise HTTPException(status_code=401, detail="not authenticated")


def require_role(min_role: Role) -> Callable[..., Awaitable[AuthIdentity]]:
    """Return a FastAPI dependency that requires ``min_role`` or higher.

    The dependency resolves the principal via :func:`current_user` (so
    missing or bad credentials still 401), then 403s when the resolved role
    ranks below ``min_role``. Otherwise it returns the identity unchanged.
    Use ``"viewer"`` on reads, ``"writer"`` on mutations, ``"admin"`` on
    user management.
    """
    return _RoleRequirement(min_role=min_role)


@dataclass(frozen=True, slots=True, kw_only=True)
class _RoleRequirement:
    min_role: Role

    async def __call__(
        self,
        identity: Annotated[AuthIdentity, Depends(current_user)],
    ) -> AuthIdentity:
        if ROLE_ORDER.index(identity.role) < ROLE_ORDER.index(self.min_role):
            raise HTTPException(
                status_code=403,
                detail=f"role {identity.role!r} insufficient; need {self.min_role!r}",
            )
        return identity


async def create_api_key(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    name: str,
    role: Role | None = None,
    ceiling: Role,
) -> tuple[uuid.UUID, str, str, Role]:
    """Insert a fresh ``api_keys`` row; return ``(key_id, secret, prefix, role)``.

    The mint is capped at the *effective ceiling*: the weaker of the owning
    user's role and ``ceiling`` (the role of the credential the caller
    presented). ``role`` defaults to that ceiling and a stronger value raises
    :class:`RoleCeilingError`. ``ceiling`` is mandatory and has no uncapped
    value -- an API-key-authenticated request passes ``ceiling=identity.role``
    so a scoped-down key cannot mint a stronger one; a credential-less internal
    caller (the bootstrap seed) passes the role it is provisioning. The
    returned ``secret`` is the plaintext token, shown once and never
    re-derivable from the row.

    Args:
      conn: Open connection; the INSERT runs on it directly.
      user_id: Owning ``users.id``.
      name: Human label for the key.
      role: Requested role; defaults to the effective ceiling.
      ceiling: Effective role of the presenting credential (or the seeded role
        for the credential-less bootstrap path).

    Raises:
      RoleCeilingError: ``role`` is stronger than the effective ceiling.
      LookupError: ``user_id`` is not a row in ``users``.

    """
    user_role = await _fetch_user_role(conn, user_id)
    max_role = effective_role(user_role, ceiling)
    final_role: Role = max_role if role is None else role
    if ROLE_ORDER.index(final_role) > ROLE_ORDER.index(max_role):
        raise RoleCeilingError(
            f"requested role {final_role!r} exceeds ceiling {max_role!r}"
        )
    key_id = uuid.uuid4()
    secret, prefix = generate_token()
    secret_hash = hash_secret(secret)
    await conn.execute(
        "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        key_id,
        user_id,
        name,
        secret_hash,
        prefix,
        final_role,
    )
    return key_id, secret, prefix, final_role


async def _fetch_user_role(conn: Conn, user_id: uuid.UUID) -> Role:
    """Return one user's role; raise when the user id is unknown."""
    user_role_raw = await conn.fetchval("SELECT role FROM users WHERE id = $1", user_id)
    if user_role_raw is None:
        raise LookupError(f"unknown user {user_id}")
    return cast(Role, user_role_raw)


async def set_api_key_role(
    conn: Conn,
    *,
    key_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    ceiling: Role,
) -> bool:
    """Re-tier one of ``user_id``'s keys to ``role`` within the effective ceiling.

    The ceiling is the weaker of the owning user's role and ``ceiling`` (the
    role of the credential the caller presented); pass ``ceiling=identity.role``
    on any API-key-authenticated request so a scoped-down key cannot promote a
    key above itself. ``ceiling`` is mandatory and has no uncapped value.

    The UPDATE is scoped to ``(key_id, user_id)`` so one user can never
    re-tier another's key. Returns ``True`` only when a live row matched;
    ``False`` covers unknown id, foreign owner, and already-revoked keys
    alike, so a UUID-probing attacker can't tell them apart.

    Existence is checked before the ceiling: a re-tier of a key that
    doesn't exist (or isn't the caller's) returns ``False`` -> 404 even
    when ``role`` exceeds the ceiling, so a 403 never leaks that an id was
    absent rather than over-privileged. The ceiling check only fires for a
    key the caller genuinely owns.

    Args:
      conn: Open connection.
      key_id: ``api_keys.id`` to re-tier.
      user_id: Owning ``users.id``; scopes the UPDATE.
      role: Target role.
      ceiling: Effective role of the presenting credential.

    Raises:
      RoleCeilingError: ``role`` exceeds the effective ceiling for an owned,
        live key (no UPDATE run).
      LookupError: ``user_id`` is not a row in ``users``.

    """
    user_role = await _fetch_user_role(conn, user_id)
    max_role = effective_role(user_role, ceiling)
    above_ceiling = ROLE_ORDER.index(role) > ROLE_ORDER.index(max_role)
    if above_ceiling:
        # Check existence before raising: a 403 on an absent key would
        # leak that it never existed. Skip revoked keys -- a dead
        # credential is "not found" for re-tiering purposes.
        exists = await conn.fetchval(
            "SELECT 1 FROM api_keys "
            "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
            key_id,
            user_id,
        )
        if exists is None:
            return False
        raise RoleCeilingError(f"requested role {role!r} exceeds ceiling {max_role!r}")
    # Skip revoked keys: re-tiering a dead credential would only confuse the UI.
    result = await conn.execute(
        "UPDATE api_keys SET role = $3 "
        "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
        key_id,
        user_id,
        role,
    )
    return result.endswith(" 1")


async def list_api_keys(conn: Conn, *, user_id: uuid.UUID) -> list[dict[str, object]]:
    """List one user's API keys, newest first, for ``GET /api/me/tokens``.

    Only this user's rows are returned -- the trust boundary against
    cross-user enumeration. ``secret_hash`` is omitted; it must never reach
    the wire.
    """
    rows = await conn.fetch(
        "SELECT id, name, prefix, role, created_at, last_used_at, revoked_at "
        "FROM api_keys WHERE user_id = $1 "
        "ORDER BY created_at DESC, id DESC",
        user_id,
    )
    return [dict(row) for row in rows]


async def revoke_api_key(conn: Conn, *, key_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Revoke one of the caller's keys.

    Scoped to ``user_id`` so one user can't revoke another's key. Returns
    ``True`` only when a live key flipped to revoked; ``False`` (unknown,
    foreign, or already-revoked) maps to 404 at the route so a UUID-probing
    attacker can't distinguish the cases.
    """
    result = await conn.execute(
        "UPDATE api_keys SET revoked_at = clock_timestamp() "
        "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
        key_id,
        user_id,
    )
    # asyncpg returns "UPDATE n"; a trailing " 1" means one row changed.
    return result.endswith(" 1")


async def allowlist_match(conn: Conn, *, email: str) -> Role | None:
    """Return the role the allowlist grants ``email``, or ``None`` to deny.

    The allowlist holds literal emails (``user@rekursiv.ai``) and domain
    wildcards (``*@rekursiv.ai``). A literal row wins when both would match,
    so an individual override beats the domain default. The wildcard lookup
    runs only on a literal miss, keeping the common case one indexed probe.
    Matching is case-insensitive.
    """
    literal = await conn.fetchval(
        "SELECT role FROM allowlist WHERE lower(email_or_pattern) = lower($1)",
        email,
    )
    if literal is not None:
        return cast(Role, literal)
    # Wildcards share the column with literals; the ``*@`` anchor skips them.
    at_index = email.rfind("@")
    if at_index == -1:
        return None
    domain = email[at_index + 1 :]
    pattern_role = await conn.fetchval(
        "SELECT role FROM allowlist "
        "WHERE email_or_pattern LIKE '*@%' "
        "AND lower(substring(email_or_pattern FROM 3)) = lower($1) "
        "LIMIT 1",
        domain,
    )
    if pattern_role is None:
        return None
    return cast(Role, pattern_role)


async def lookup_user_by_id(
    conn: Conn,
    *,
    user_id: uuid.UUID,
) -> AuthIdentity | None:
    """Resolve a ``users.id`` to an :class:`AuthIdentity` for session auth.

    Session requests have no ``api_keys`` row, so ``api_key_id`` is ``None``.
    A missing or disabled user returns ``None`` -- indistinguishable cases,
    so a stolen-cookie attacker learns nothing.
    """
    row = await conn.fetchrow(
        "SELECT id, email, role, status FROM users WHERE id = $1",
        user_id,
    )
    if row is None:
        return None
    if row["status"] != "active":
        return None
    return AuthIdentity(
        user_id=row["id"],
        api_key_id=None,
        email=row["email"],
        role=row["role"],
    )


async def assert_account_active(engine: DatabaseEngine, account: str) -> None:
    """Reject an inquiry ``account`` that is not a live, active user.

    The "an inquiry is attributed only to an active user" rule is an
    authorization concern: ``users.status`` is meaningful only for a real
    authenticated identity, so the check lives here at the auth boundary,
    not in the Store (whose internal callers have no auth context). The
    submit and ``set_account`` routes call this on the resolved account
    before delegating to the Store, which enforces only NOT NULL.

    The check guards the value being written, never the row's existing
    account: a user disabled after being stamped is not swept.

    Raises:
      HTTPException: ``account`` has no ``active`` row in ``users`` (422).

    """
    async with engine.acquire() as conn:
        live = await conn.fetchval(
            "SELECT 1 FROM users WHERE email = $1 AND status = 'active'", account
        )
    if live is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"account {account!r} is not an active user; an inquiry must be "
                "attributed to an authenticated active account"
            ),
        )


async def bootstrap_admin(conn: Conn) -> None:
    """Seed the first admin (allowlist + user + api_key) when ``users`` is empty.

    Takes a fresh deployment to "operator can call the API immediately":
    seeds the allowlist row, the admin ``users`` row, and an api_key, then
    writes the secret to a known file (mode 0600) the operator reads once.

    Reads ``TRACKINIZER_BOOTSTRAP_ADMIN``; returns immediately if unset.
    Idempotent: once any user exists this is a no-op.

    The token file path is ``$TRACKINIZER_BOOTSTRAP_TOKEN_FILE`` or
    ``<data_dir>/trackinizer/bootstrap_token``. The write is two-phase:
    the secret is fsynced to a ``.tmp`` sibling inside the tx (an FS failure
    rolls the DB back), then renamed into place after COMMIT (a COMMIT
    failure leaves no live secret at the final path, only an orphan ``.tmp``).
    """
    # Lowercase to match the admin-UI canonical form and the read path; the
    # column is case-sensitive, so a mixed-case env value would duplicate.
    email = os.environ.get(BOOTSTRAP_ADMIN_ENV, "").strip().lower()
    if not email:
        return
    existing_user = await conn.fetchval("SELECT 1 FROM users LIMIT 1")
    if existing_user is not None:
        return
    # Two-phase write across DB + FS so a rolled-back api_keys row never
    # leaves a live plaintext secret at the final path. See the docstring.
    token_path = _bootstrap_token_path()
    user_id = uuid.uuid4()
    seeded = False
    async with tx(conn):
        await conn.execute(
            "INSERT INTO allowlist (email_or_pattern, role, added_by) "
            "VALUES ($1, 'admin', NULL) "
            "ON CONFLICT (email_or_pattern) DO NOTHING",
            email,
        )
        # name = email local part so the identity column isn't blank;
        # status='active' so the minted key works immediately. The empty-users
        # probe above is outside this tx, so under ``--workers N`` two workers
        # can both clear it; ``ON CONFLICT (email) DO NOTHING`` makes the loser's
        # insert a no-op instead of a unique-violation crash. ``RETURNING id``
        # distinguishes winner from loser: a NULL means another worker already
        # seeded the admin, so this worker must not mint a second key or publish
        # a token for a credential it does not own.
        inserted_id = await conn.fetchval(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, $3, 'admin', 'active') "
            "ON CONFLICT (email) DO NOTHING RETURNING id",
            user_id,
            email,
            email.split("@", 1)[0],
        )
        if inserted_id is not None:
            seeded = True
            # The seed provisions an admin user with no presented credential,
            # so the ceiling is the admin role it is creating.
            _key_id, secret, _prefix, _role = await create_api_key(
                conn, user_id=user_id, name="bootstrap", ceiling="admin"
            )
            _stage_bootstrap_token(token_path, secret)
    if seeded:
        _publish_bootstrap_token(token_path)


async def seed_no_auth_user(conn: Conn) -> None:
    """Seed the synthetic ``--no-auth`` principal as an active admin user.

    ``--no-auth`` collapses every request to a synthetic admin identity
    (``no-auth@localhost``) that never goes through the ``users`` table. The
    account-attribution gate (``assert_account_active``) validates a submit's
    account against ``users``, so without a real row for that email every
    write in demo mode would 422. Seeding the row -- with the same fixed id
    the resolver hands out -- makes no-auth submits attribute to a valid
    active account. Idempotent via ``ON CONFLICT``.
    """
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES ($1, $2, 'no-auth', 'admin', 'active') "
        "ON CONFLICT (email) DO NOTHING",
        _NO_AUTH_USER_ID,
        _NO_AUTH_EMAIL,
    )


def _bootstrap_token_path() -> Path:
    """Resolve the final bootstrap-token path from env override or default."""
    override = os.environ.get(BOOTSTRAP_TOKEN_FILE_ENV, "").strip()
    return (
        Path(override)
        if override
        else data_dir() / "rekursiv-ai" / "trackinizer" / "bootstrap_token"
    )


def _stage_bootstrap_token(token_path: Path, secret: str) -> None:
    """Stage the secret: fsync it to ``<final>.tmp`` inside the DB transaction.

    An ``OSError`` here propagates so ``tx(conn)`` rolls back. ``O_TRUNC``
    overwrites any ``.tmp`` orphan from a prior crash so re-bootstrap can't
    inherit a stale plaintext.
    """
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = token_path.with_name(token_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp_path.chmod(0o600)


def _publish_bootstrap_token(token_path: Path) -> None:
    """Publish the staged secret: rename ``.tmp`` to final and fsync the dir.

    Runs after the transaction commits. The POSIX rename is atomic (no
    partial file ever visible) and the dir fsync makes it crash-durable. A
    failure here is a real outage -- the commit landed but the secret never
    materialized -- so the ``.tmp`` is left for manual recovery.
    """
    tmp_path = token_path.with_name(token_path.name + ".tmp")
    tmp_path.replace(token_path)
    dir_fd = os.open(token_path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    logger.warning(
        "bootstrap admin token written to %s (read once, then rotate)",
        token_path,
    )


def _try_extract_bearer(request: Request) -> str | None:
    """Return the bearer secret from the Authorization header, or ``None``.

    A non-Bearer scheme or a blank payload yields ``None`` so
    :func:`current_user` falls through to the session path. Only "no bearer
    offered" is silent; an offered-but-unresolvable secret 401s downstream.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith(_BEARER_PREFIX):
        return None
    secret = header[len(_BEARER_PREFIX) :].strip()
    if not secret:
        return None
    return secret


def _try_read_session_user_id(request: Request) -> uuid.UUID | None:
    """Return the ``users.id`` from the signed session cookie, or ``None``.

    The session path is off when ``app.state.config`` is absent (tests that
    skip the lifespan) or ``Config.session_secret`` is unset (OAuth not
    configured); bearer auth is unaffected either way.
    """
    config = getattr(request.app.state, "config", None)
    if config is None or not config.session_secret:
        return None
    raw = read_session_cookie(
        request.cookies,
        secret=config.session_secret,
        max_age_seconds=config.session_max_age_seconds,
    )
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def _resolve_session_identity(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
) -> AuthIdentity | None:
    """Look up the ``users`` row for a session-cookie principal."""
    async with engine.acquire() as conn:
        return await lookup_user_by_id(conn, user_id=user_id)


async def _resolve_identity(
    store: Store,
    secret: str,
    *,
    request_id: str,
) -> AuthIdentity | None:
    """Resolve a bearer secret to an identity, bumping ``last_used_at``.

    Returns ``None`` (a 401 at the route) when no live key matches. Disabled
    users also return ``None``, so "unknown token" and "valid token, disabled
    user" are indistinguishable.
    """
    started = time.perf_counter()
    query_sec = 0.0
    verify_sec = 0.0
    prefix = secret[:TOKEN_PREFIX_LEN]
    async with store.engine.acquire() as conn:
        query_started = time.perf_counter()
        rows = await conn.fetch(
            "SELECT k.id AS key_id, k.secret_hash, k.role AS key_role, "
            "u.id AS user_id, u.email, u.role AS user_role, u.status "
            "FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.prefix = $1 AND k.revoked_at IS NULL",
            prefix,
        )
        query_sec += time.perf_counter() - query_started
        lookup_mode = "prefix" if rows else "miss"
        if not rows:
            verify_started = time.perf_counter()
            verify_secret(secret, _dummy_verify_hash())
            verify_sec += time.perf_counter() - verify_started
            _log_auth_resolution(
                request_id=request_id,
                lookup_mode=lookup_mode,
                outcome="rejected",
                query_sec=query_sec,
                verify_sec=verify_sec,
                duration_sec=time.perf_counter() - started,
            )
            return None
        for row in rows:
            verify_started = time.perf_counter()
            verified = verify_secret(secret, row["secret_hash"])
            verify_sec += time.perf_counter() - verify_started
            if not verified:
                continue
            if row["status"] != "active":
                # Prefix is shared, so a disabled match mustn't exclude an
                # active match later in the list.
                continue
            key_id = row["key_id"]
            if store.should_bump_api_key_last_used(key_id):
                await conn.execute(
                    "UPDATE api_keys SET last_used_at = clock_timestamp() "
                    "WHERE id = $1",
                    key_id,
                )
            identity = AuthIdentity(
                user_id=row["user_id"],
                api_key_id=key_id,
                email=row["email"],
                role=effective_role(
                    row["user_role"],
                    row["key_role"],
                ),
            )
            _log_auth_resolution(
                request_id=request_id,
                lookup_mode=lookup_mode,
                outcome="success",
                query_sec=query_sec,
                verify_sec=verify_sec,
                duration_sec=time.perf_counter() - started,
            )
            return identity
    _log_auth_resolution(
        request_id=request_id,
        lookup_mode=lookup_mode,
        outcome="rejected",
        query_sec=query_sec,
        verify_sec=verify_sec,
        duration_sec=time.perf_counter() - started,
    )
    return None


def _log_auth_resolution(
    *,
    request_id: str,
    lookup_mode: str,
    outcome: str,
    query_sec: float,
    verify_sec: float,
    duration_sec: float,
) -> None:
    logger.info(
        "event=trackinizer_auth_completed stage=bearer_auth outcome=%s "
        "lookup_mode=%s query_sec=%.6f verify_sec=%.6f duration_sec=%.6f "
        "request_id=%s",
        outcome,
        lookup_mode,
        query_sec,
        verify_sec,
        duration_sec,
        request_id,
        extra={
            "event": "trackinizer_auth_completed",
            "stage": "bearer_auth",
            "outcome": outcome,
            "lookup_mode": lookup_mode,
            "query_sec": query_sec,
            "verify_sec": verify_sec,
            "duration_sec": duration_sec,
            "request_id": request_id,
        },
    )


@functools.cache
def _dummy_verify_hash() -> str:
    """A scrypt hash of an unguessable sentinel, computed once per process.

    Verified against on a bearer prefix miss so the miss pays the same
    ~50ms scrypt cost as a hit, closing the timing oracle on which key
    prefixes exist. No minted token ever reproduces it: the sentinel is
    never returned by :func:`generate_token`. Lazy so module import (and
    test collection) doesn't eat a scrypt round on every process.
    """
    return hash_secret(secrets.token_urlsafe(_TOKEN_BYTES))


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without ``=`` padding (round-tripped by ``_b64decode``)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    """Inverse of :func:`_b64encode`; restores padding before decoding."""
    pad = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + pad)
