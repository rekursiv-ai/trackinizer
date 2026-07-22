"""Per-request client-supplied ``change_log.id`` slot and its accessors.

The ``Idempotency-Key`` header names one logical mutation: the slot holds the
client UUID until the first :meth:`emit_change` consumes it, so cascade rows
fall back to fresh server-minted ids. Shared across the submit, session, and
cascade mixins, so it lives in its own module to avoid an import cycle.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


__all__ = [
    "_CLIENT_CHANGE_ID",
    "_ChangeIdSlot",
    "_consume_client_change_id",
    "_peek_client_change_id",
    "set_client_change_id",
]


# Per-request slot for a client-supplied ``change_log.id``, delivered via the
# ``Idempotency-Key`` header. ``emit_change`` consumes it on its first call so
# cascade rows fall back to fresh server-minted ids: the client's UUID names
# one logical mutation, not the whole transaction.
#
# A mutable single-cell holder, not a bare ``UUID``, so ``asyncio.gather``
# siblings share one consume cursor: each task copies the ContextVar snapshot
# but the holder object is shared by reference, so the first sibling to consume
# clears the cell for all others.
@dataclass(slots=True, kw_only=True)
class _ChangeIdSlot:
    """Single-cell mutable holder for the active client-supplied change UUID."""

    value: UUID | None


_CLIENT_CHANGE_ID: ContextVar[_ChangeIdSlot | None] = ContextVar(
    "trackinizer_client_change_id", default=None
)


def set_client_change_id(value: UUID | None) -> None:
    """Store (or clear) the client-supplied change UUID for the next mutation.

    Setting a value installs a fresh shared holder. Clearing (``value is None``)
    MUTATES the existing holder's cell -- not just the current task's ContextVar
    -- so an ``asyncio.gather`` sibling that copied the same holder by reference
    sees the drain too. Without that, a no-op/replay path that clears the slot in
    one task would leave the stale key visible to siblings (the consume cursor is
    shared by reference precisely so all siblings observe one another's drains).
    """
    if value is not None:
        _CLIENT_CHANGE_ID.set(_ChangeIdSlot(value=value))
        return
    slot = _CLIENT_CHANGE_ID.get()
    if slot is not None:
        slot.value = None
    else:
        _CLIENT_CHANGE_ID.set(None)


def _consume_client_change_id() -> UUID | None:
    """Return and clear the client-supplied change UUID, if any.

    Clearing on read makes second and later calls (cascade rows, gather
    siblings) fall back to fresh server-minted ids.
    """
    slot = _CLIENT_CHANGE_ID.get()
    if slot is None:
        return None
    value = slot.value
    slot.value = None
    return value


def _peek_client_change_id() -> UUID | None:
    """Return the client-supplied change UUID without consuming it.

    Used by the submit path to read the header-set idempotency key for the
    replay probe and the collision-recovery probe, while leaving the slot
    intact so ``emit_change`` still consumes it on the first write.
    """
    slot = _CLIENT_CHANGE_ID.get()
    return slot.value if slot is not None else None
