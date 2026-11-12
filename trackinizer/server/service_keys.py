"""The service accounts a deployment names, and the one live key each keeps, at every start.

``Config.service_keys`` names them (``TRACKINIZER_SERVICE_KEYS``, ``NAME:ROLE`` pairs such as
``chat:writer,gpu-broker:writer``) and the directory their keys live in
(``TRACKINIZER_SERVICE_KEY_DIR``), one file per account named for it, mode 0600. At each
start the server makes an account that is missing, keeps the key in its file when it is a
live, unpinned key of that account at that role, and otherwise mints one and writes it. Then
it revokes every other key of the account: an account named here is the deployment's, and
only the key in its file works. systemd hands each file to the one service that reads it
(``LoadCredential=``), and the service reads it through ``TRACKINIZER_TOKEN_FILE``. No admin
key is involved. The app's lifespan runs it once the schema is in place, beside the
``--no-auth`` seed.

To rotate a key, delete its file and restart the server, then every service that reads it:
``LoadCredential=`` copies the file when a service starts, so one left running keeps the
revoked key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import logging
import uuid

from trackinizer.server.auth import (
    TOKEN_PREFIX_LEN,
    Role,
    UserObserver,
    create_api_key,
    publish_token_file,
    stage_token_file,
    verify_secret,
)
from trackinizer.server.notify import tx
from trackinizer.wire.wire_service_accounts import service_address


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.config import ServiceKeys


__all__ = ["ensure_service_keys"]


_logger = logging.getLogger(__name__)


async def ensure_service_keys(
    conn: Conn,
    *,
    keys: ServiceKeys,
    policy: UserObserver,
) -> None:
    """Keep each named service account's one live key in its file.

    Args:
      conn: Open database connection, in no transaction.
      keys: The accounts and the directory their key files live in.
      policy: The deployment's access policy, told of each account it makes.

    Raises:
      ValueError: An account named is disabled or holds another role.

    """
    # The lock spans each file's read and publish, not just the database's transaction:
    # two servers starting at once would otherwise both find no file, both mint, and the
    # second would revoke the key the first writes.
    await conn.execute("SELECT pg_advisory_lock(hashtext('trackinizer.service_keys'))")
    try:
        for name, role in keys.accounts:
            await _ensure(
                conn,
                policy=policy,
                name=name,
                role=role,
                path=keys.directory / name,
            )
    finally:
        await conn.execute(
            "SELECT pg_advisory_unlock(hashtext('trackinizer.service_keys'))",
        )


async def _ensure(
    conn: Conn,
    *,
    policy: UserObserver,
    name: str,
    role: Role,
    path: Path,
) -> None:
    """Keep one account's key: the file's when it works, else a new one."""
    email = service_address(name)
    held = _held(path)
    async with tx(conn):
        user_id = await _account(conn, policy=policy, email=email, name=name, role=role)
        key_id = await _working_key(conn, user_id=user_id, role=role, secret=held)
        minted = key_id is None
        if minted:
            # The account holds ``role`` (``_account`` checked), so the key gets it too.
            key_id, secret, _prefix, _role = await create_api_key(
                conn,
                user_id=user_id,
                name=f"{name}-service",
                ceiling=role,
            )
            stage_token_file(path, secret)
        # pragma: no mutate start -- SQL text, which no fake connection judges.
        revoked = await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() "
            "WHERE user_id = $1 AND id <> $2 AND revoked_at IS NULL",
            user_id,
            key_id,
        )
        # pragma: no mutate end
    if minted:
        publish_token_file(path)
    _logger.warning(
        "event=trackinizer_service_key account_email=%r key_id=%s minted=%s revoked=%s",
        email,
        key_id,
        minted,
        revoked.removeprefix("UPDATE "),
    )


def _held(path: Path) -> str:
    """Return the key in ``path``, or empty when there is no file."""
    if not path.is_file():
        return ""
    # pragma: no mutate start -- a key is ASCII; every encoding reads it.
    text = path.read_text(encoding="utf-8")
    # pragma: no mutate end
    return text.strip()


async def _account(
    conn: Conn,
    *,
    policy: UserObserver,
    email: str,
    name: str,
    role: Role,
) -> uuid.UUID:
    """Return the account's id, making it when it is missing."""
    # pragma: no mutate start -- SQL text, which no fake connection judges.
    row = await conn.fetchrow(
        "SELECT id, role, status FROM users WHERE email = $1 FOR UPDATE",
        email,
    )
    # pragma: no mutate end
    if row is None:
        user_id = uuid.uuid4()
        # pragma: no mutate start -- SQL text, which no fake connection judges.
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, $3, $4, 'active')",
            user_id,
            email,
            name,
            role,
        )
        # pragma: no mutate end
        await policy.on_user(conn, email=email, role=role)
        return user_id
    if row["status"] != "active" or row["role"] != role:
        raise ValueError(
            f"{email} is {row['status']} with role {row['role']}, not active with "
            f"role {role}: fix the account or TRACKINIZER_SERVICE_KEYS",
        )
    return cast(uuid.UUID, row["id"])


async def _working_key(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    role: Role,
    secret: str,
) -> uuid.UUID | None:
    """Return the id of ``secret`` when it is a live, unpinned key of the account at ``role``."""
    if not secret:
        return None
    # pragma: no mutate start -- SQL text, which no fake connection judges.
    rows = await conn.fetch(
        "SELECT id, secret_hash, role, open_into, revoked_at FROM api_keys "
        "WHERE user_id = $1 AND prefix = $2",
        user_id,
        secret[:TOKEN_PREFIX_LEN],
    )
    # pragma: no mutate end
    for row in rows:
        if (
            row["revoked_at"] is None
            and row["open_into"] is None
            and row["role"] == role
            and verify_secret(secret, cast(str, row["secret_hash"]))
        ):
            return cast(uuid.UUID, row["id"])
    return None
