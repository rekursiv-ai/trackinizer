"""The one rule a tool pipe opens under, which each sweep asks again of a pipe up.

The configured assistant's own key, live and of a writer or admin, may reach a machine
for an active writer or admin who owns it, whose credential is live, whose owner has
confirmed it and whose host reports ``tools: true``. A pipe a host has dialed back for
also needs that host to stay the machine's current instance. Whether the machine is up
is not asked: its host dials back, or does not.

One query reads all of it, and the first check that fails is named, so the open's
refusal and the sweep's warning say the same thing.

Typical usage example:

  access = await tool_access(conn, assistant=a, api_key_id=k, asker=e, machine=name)
  if access.failed is not None:
      refuse(access.failed)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from treekle import ReadError, from_plain


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.server.config import Assistant


__all__ = ["Check", "ToolAccess", "serves_tools", "tool_access"]


type Check = Literal[
    "assistant",
    "key",
    "asker",
    "machine",
    "credential",
    "confirmed",
    "tools",
    "instance",
]
"""A part of the rule, as a refusal and a warning name it.

``assistant``: the server has one configured. ``key``: the key is live, a writer's or an
admin's, of the assistant, who is an active writer or admin. ``asker``: the asker is an
active writer or admin. ``machine``: the asker owns a machine of that name or id.
``credential``: its credential is live. ``confirmed``: its owner confirmed it. ``tools``:
its host serves tools. ``instance``: the host attached is still its current one."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolAccess:
    """What the rule says of one key, asker and machine."""

    machine_id: UUID | None
    """The machine named, of the asker's; ``None`` for none."""

    failed: Check | None
    """The first check that fails; ``None`` when the rule holds."""


_RULE: Final = (
    "SELECT "
    "EXISTS (SELECT 1 FROM api_keys k JOIN users a ON a.id = k.user_id "
    "WHERE k.id = $1 AND k.revoked_at IS NULL AND k.role IN ('writer', 'admin') "
    "AND a.email = $2 AND a.status = 'active' AND a.role IN ('writer', 'admin')) "
    "AS key, "
    "EXISTS (SELECT 1 FROM users u WHERE u.email = $3 AND u.status = 'active' "
    "AND u.role IN ('writer', 'admin')) AS asker, "
    "m.id, m.facts, m.confirmed_at IS NOT NULL AS confirmed, m.host_instance, "
    "EXISTS (SELECT 1 FROM machine_credentials c "
    "WHERE c.machine_id = m.id AND c.revoked_at IS NULL) AS credentialed "
    "FROM (SELECT 1) AS one "
    "LEFT JOIN machines m ON m.owner = $3 AND (m.id = $4::uuid OR m.name = $5::text)"
)


async def tool_access(
    conn: Conn,
    *,
    assistant: Assistant | None,
    api_key_id: UUID | None,
    asker: str | None,
    machine: UUID | str,
    instance: UUID | None = None,
) -> ToolAccess:
    """Return what the rule says of ``asker``'s ``machine``, reached by ``api_key_id``.

    Args:
      conn: Where the rule is read, in one query.
      assistant: The server's configured assistant; ``None`` for none.
      api_key_id: The key the agent's leg came with; ``None`` for no key.
      asker: The person the assistant acts for, in any case; ``None`` for none named.
      machine: The machine, by its id or, as an agent names it, its name.
      instance: The host that attached; ``None`` before one has.

    Returns:
      access: The machine's id, and the first check that fails.

    """
    if assistant is None:
        return ToolAccess(machine_id=None, failed="assistant")
    # One row: the machine's, or nulls where there is none.
    (row,) = await conn.fetch(
        _RULE,
        api_key_id,
        assistant.email,
        None if asker is None else asker.lower(),
        machine if isinstance(machine, UUID) else None,
        machine if isinstance(machine, str) else None,
    )
    machine_id = from_plain(row["id"], UUID, default=None)
    checks: list[tuple[Check, bool]] = [
        ("key", row["key"] is True),
        ("asker", row["asker"] is True),
        ("machine", machine_id is not None),
        ("credential", row["credentialed"] is True),
        ("confirmed", row["confirmed"] is True),
        ("tools", serves_tools(row["facts"])),
        ("instance", instance is None or row["host_instance"] == instance),
    ]
    failed: Check | None = next((check for check, holds in checks if not holds), None)
    return ToolAccess(machine_id=machine_id, failed=failed)


def serves_tools(facts: object) -> bool:
    """Return whether a machine's facts say its host serves tools to an agent."""
    try:
        return from_plain(facts, dict[str, object]).get("tools") is True
    except ReadError:
        return False
