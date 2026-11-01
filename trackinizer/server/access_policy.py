"""The seam an access-control addon plugs into, and today's rules as its default.

Core asks one policy who may read, write or run a resource, and what a new
resource's grants are. Core never stores grants or groups: an addon that sets
``AddonManifest.access_policy`` keeps them in its own schema. With no such addon
installed, :class:`DefaultPolicy` answers, and every answer is the one the server
gave before the seam existed: roles and session audiences decide, as they always
did, at the call sites that already check them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from trackinizer.server.auth import Role
from trackinizer.server.values import vetted_sql


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = [
    "AccessDeniedError",
    "AccessPolicy",
    "Action",
    "DefaultPolicy",
    "Principal",
    "Resource",
    "ResourceKind",
    "inquiry_read_filter",
]


type ResourceKind = Literal[
    "inquiry",
    "session",
    "canvas",
    "variable",
    "machine",
    "terminal",
    "gpu_job",
    "machine_job",
    "box",
]
"""Every kind of resource with an owner; a grant is keyed by kind and id."""

type Action = Literal["read", "write", "run"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal:
    """Who is asking, as the policy sees them.

    Attributes:
      email: The authenticated account, lowercase.
      role: The server-wide role, already the minimum of user and key.
      groups: The policy's own group ids for this account, computed once per
        request by :meth:`AccessPolicy.groups`; empty under the default policy.
      api_key_id: The API key the request used; ``None`` for a browser session or
        the system.

    """

    email: str
    role: Role
    groups: frozenset[str] = frozenset()
    api_key_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Resource:
    """One resource a check is about.

    Attributes:
      kind: What the resource is.
      id: Its id as text: a UUID, a ``BIGSERIAL``, or a composite key's parts
        joined by ``/``, so one grant table keys every kind.
      account: The originator the resource is attributed to, when it has one.

    """

    kind: ResourceKind
    id: str
    account: str | None = None


class AccessDeniedError(Exception):
    """The policy refused an action; the route answers 403 with this message."""


class AccessPolicy(Protocol):
    """Who may read, write or run a resource, and what a new one's grants are.

    Where each method runs, and so what it may rely on:

    - ``on_create`` and ``validate`` run inside the store's write transaction, so
      what they write commits or rolls back with the change they follow.
    - ``check`` runs in a transaction of its own just before the guarded write
      (the lock route alone shares the write's), so it must only read: anything it
      writes or locks outlives a write that then fails. It moves inside the
      store's transaction before any deployment enforces a policy.
    - ``groups`` runs once per request, outside any transaction.
    """

    async def groups(self, conn: Conn, principal: Principal) -> frozenset[str]:
        """Return the principal's groups; core calls this once per request."""
        ...

    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        """Return a SQL boolean over ``alias`` that holds for each readable row.

        Readers AND it into their WHERE clauses before pagination. For a session,
        ``session`` is the transcript (its records) and ``inquiry`` is the row,
        both keyed by the session's inquiry UUID.

        Args:
          kind: The kind of the rows ``alias`` names.
          alias: The SQL alias of the table or subquery holding those rows.
          params: The query's positional parameters; append any the fragment binds.
          principal: The reader, with ``groups`` already filled.

        Returns:
          predicate: A boolean SQL expression, safe to ``AND`` into a WHERE clause.

        """
        ...

    async def check(
        self,
        conn: Conn,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> None:
        """Raise :class:`AccessDeniedError` unless ``principal`` may act on it."""
        ...

    async def on_create(
        self,
        conn: Conn,
        *,
        principal: Principal | None,
        resource: Resource,
    ) -> None:
        """Record a new resource's grants; ``principal`` is ``None`` for the system."""
        ...

    async def prepare_account_change(self, conn: Conn, *, resource: Resource) -> None:
        """Acquire policy locks before core locks the inquiry being reassigned."""
        ...

    async def validate(self, conn: Conn, *, resource: Resource) -> None:
        """Raise ``ConflictError`` when the resource's state breaks an invariant.

        Core calls it after a change to the resource's ``account``, in the same
        transaction, so the change rolls back with the refusal.

        Args:
          conn: The caller's connection, inside its transaction.
          resource: The resource as it stands after the change.

        """
        ...

    async def on_user(self, conn: Conn, *, email: str, role: Role) -> None:
        """Note an account's role, at every ``users`` insert and role change."""
        ...


def inquiry_read_filter(
    reference: str,
    *,
    policy: AccessPolicy | None,
    principal: Principal | None,
    params: list[object],
) -> str:
    """Gate a qualified inquiry-id SQL expression by its current row's visibility.

    Args:
      reference: Trusted, qualified SQL expression naming an inquiry UUID.
      policy: Deployment policy; None preserves trusted internal reads.
      principal: Request reader; None preserves trusted internal reads.
      params: Positional bindings extended by the policy.

    Returns:
      predicate: TRUE for unrestricted reads, otherwise a correlated EXISTS.

    """
    if policy is None or principal is None:
        return "TRUE"
    predicate = policy.read_filter(
        "inquiry",
        "readable_inquiry",
        params=params,
        principal=principal,
    )
    if predicate == "TRUE":
        return predicate
    return vetted_sql(
        "EXISTS (SELECT 1 FROM inquiries readable_inquiry WHERE readable_inquiry.id = ",
        reference,
        " AND (",
        predicate,
        "))",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DefaultPolicy:
    """Today's rules: roles and audiences decide where they already did."""

    async def groups(self, conn: Conn, principal: Principal) -> frozenset[str]:
        """Return no groups: the default policy keeps none."""
        del conn, principal
        return frozenset[str]()

    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        """Return ``TRUE``: audiences, not this seam, filter reads by default."""
        del kind, alias, params, principal
        return "TRUE"

    async def check(
        self,
        conn: Conn,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> None:
        """Allow: the route's role and audience guards decide by default."""
        del conn, principal, resource, action

    async def on_create(
        self,
        conn: Conn,
        *,
        principal: Principal | None,
        resource: Resource,
    ) -> None:
        """Record nothing: the default policy keeps no grants."""
        del conn, principal, resource

    async def prepare_account_change(self, conn: Conn, *, resource: Resource) -> None:
        """Acquire no locks: the default policy keeps no mutable grants."""
        del conn, resource

    async def validate(self, conn: Conn, *, resource: Resource) -> None:
        """Accept every state: the default policy holds no invariants."""
        del conn, resource

    async def on_user(self, conn: Conn, *, email: str, role: Role) -> None:
        """Note nothing: the default policy keeps no memberships."""
        del conn, email, role
