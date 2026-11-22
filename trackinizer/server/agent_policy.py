"""The seam an addon decides the work of the server's remote agents through.

A remote agent service runs agents for people on what the server provides. Three
points only core sees are an addon's to act on, through the one policy a deployment's
addons may set (``AddonManifest.agent_policy``):

- whom a service serves beyond the people its configuration admits
  (:meth:`AgentPolicy.serves`);
- whether an agent's start, or a line to a live one, goes ahead
  (:meth:`AgentPolicy.refusal`): a refusal answers the caller with the status, code
  and message the policy names;
- what follows a batch of the records such an agent stored (:meth:`AgentPolicy.stored`):
  the policy may ask that the agents of some sessions be halted, and each of those
  sessions' viewers told why.

With no addon setting one, a service serves its ``admits`` alone, and nothing is
refused or halted. An agent on a person's own host is none of these: core asks the
policy nothing about it.

Typical usage example:

  refused = await policy.refusal(engine, asker=email, session_id=s, work="start")
  if refused is not None:
      raise refusal(refused.status, code=refused.code, message=refused.message)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from uuid import UUID


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.wire.wire_session_ir import RecordBody


__all__ = ["AgentPolicy", "AgentWork", "Halt", "Refusal"]


type AgentWork = Literal["start", "line"]
"""What a person or an agent asks of a remote agent: to start it, or to take a line."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Refusal:
    """Why work does not go ahead, as the caller is answered.

    Attributes:
      status: The HTTP status.
      code: The ``code`` of the answer's detail, which a page may act on.
      message: One sentence for the caller.

    """

    status: int
    code: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Halt:
    """Agents to halt, every agent of each session's process, and what to tell.

    Attributes:
      sessions: The sessions whose agents are halted; each must be live.
      notice: What each of those sessions' viewers is told, once.

    """

    sessions: frozenset[UUID]
    notice: str


@runtime_checkable
class AgentPolicy(Protocol):
    """What an addon decides of the work of the server's remote agents."""

    def serves(self, asker: str) -> bool:
        """Return whether a remote agent service serves ``asker`` beyond its admits.

        Args:
          asker: The caller's address.

        Returns:
          served: Whether to serve them too; ``False`` leaves its ``admits`` alone.

        """
        ...

    async def refusal(
        self,
        engine: DatabaseEngine,
        *,
        asker: str,
        session_id: UUID,
        work: AgentWork,
    ) -> Refusal | None:
        """Return why a remote agent's start, or a line to one, does not go ahead.

        Args:
          engine: The server's database.
          asker: The caller's address: a person, or another session's agent's key.
          session_id: The session the agent serves.
          work: What is asked.

        Returns:
          refused: How the caller is answered; ``None`` to let it go ahead.

        """
        ...

    async def stored(
        self,
        engine: DatabaseEngine,
        *,
        session_id: UUID,
        part: int,
        records: Sequence[RecordBody],
        live: Sequence[UUID],
    ) -> Halt | None:
        """Take a batch of records a remote agent stored; say which agents to halt.

        Core calls it after the records commit, with every record the upload holds,
        those stored before by an earlier try of it too.

        Args:
          engine: The server's database.
          session_id: The session the records are stored in.
          part: The part they are stored in.
          records: The upload's records, in ``idx`` order.
          live: The sessions each with an agent live on a remote agent service now.

        Returns:
          halt: Which of ``live`` to halt and what to tell; ``None`` for none.

        """
        ...
