"""MiniMax chat-completions transport restrictions.

Load model facts with ``trackinizer.lib.agent.catalog.models("minimax")``.
Transport capabilities narrow those rows with ``&``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from trackinizer.lib.agent.catalog.openai import compatible


if TYPE_CHECKING:
    from trackinizer.lib.agent.types.capability import ModelCapability
    from trackinizer.lib.agent.types.cost import ServiceTier


__all__ = ["api"]


def api() -> ModelCapability:
    """Return the chat-completions transport with MiniMax's ``priority`` tier.

    Returns:
      capability: The shared compatible transport, plus ``priority``.

    """
    transport = compatible()
    priority: frozenset[ServiceTier] = frozenset({"priority"})
    return replace(transport, service_tier=transport.service_tier | priority)
