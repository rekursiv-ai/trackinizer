""":class:`_StoreShared` -- the concrete base every ``Store`` mixin inherits.

Holds the shared instance state (``engine``, ``access_policy``,
``_last_used_bumped_at``, ``_verified_bearers``). Cross-mixin *method* visibility is
provided by the mixin inheritance chain (each mixin inherits the mixin whose methods
it calls), so this base only needs to own the genuinely shared state -- no stubs, no
``Any``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

from trackinizer.server.access_policy import AccessPolicy, DefaultPolicy


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.auth import AuthIdentity


__all__ = ["_StoreShared"]


class _StoreShared:
    """Shared instance state for every ``Store`` mixin.

    The composed :class:`Store` is built from mixins split by concern; each
    inherits this base so ``self.engine`` and ``self.access_policy`` resolve
    uniformly. Cross-mixin method calls resolve through the mixin inheritance
    chain, not through this base.
    """

    def __init__(
        self,
        engine: DatabaseEngine,
        *,
        access_policy: AccessPolicy | None = None,
    ) -> None:
        """Construct a Store bound to ``engine``.

        ``access_policy`` is the deployment's, today's rules when unset: the store
        asks it inside its own transactions, when a row is created and when a
        row's account moves.
        """
        self.engine = engine
        self.access_policy: AccessPolicy = access_policy or DefaultPolicy()
        # Throttle for ``api_keys.last_used_at`` UPDATE coalescing; see
        # :meth:`should_bump_api_key_last_used`. Per-instance so separate
        # Stores keep independent bookkeeping and the memory releases when
        # the Store is collected.
        self._last_used_bumped_at: dict[UUID, float] = {}
        # Verified-bearer cache; see :meth:`cached_bearer_identity`. Keyed by
        # sha256 of the presented secret so no plaintext token is held in
        # memory. Per-instance for the same reasons as the throttle above.
        self._verified_bearers: dict[bytes, tuple[AuthIdentity, float]] = {}
        # Bulkhead cap for concurrent heavy web read routes so a burst of
        # analytical reads cannot starve auth, writes, heartbeats, or relays.
        self.heavy_read_semaphore: asyncio.Semaphore = asyncio.Semaphore(4)
