"""Types shared across ``trax run``, above the adapter package.

The ``Event`` record every adapter produces and every sink consumes. It lives
here rather than beside the ``Adapter`` protocol because the sinks never touch
an adapter: keeping it in ``adapters/`` made every sink import the adapter
package for one dataclass.

The adapter-facing protocols are the sibling ``adapters/custom_types.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from trackinizer.types.agent_session_events import Kind, Message


__all__ = ["Event"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """One parsed turn, in memory, before it becomes an ``EventBody``.

    ``message`` is the typed turn content (a :data:`Message` member) the
    adapter normalized the CLI's native record into; :attr:`kind` is derived
    from it. ``model`` / ``timestamp`` are the per-turn envelope the adapter
    could read, or ``None``.
    """

    message: Message
    """The normalized turn content; its class name is the row ``kind``."""

    model: str | None = None
    """Per-turn model, when the CLI surfaced it."""

    timestamp: datetime | None = None
    """When the turn happened, on the CLI clock, when available."""

    @property
    def kind(self) -> Kind:
        """The row discriminator: the message member's class name."""
        return cast(Kind, type(self.message).__name__)
