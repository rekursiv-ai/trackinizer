"""Value types for accounting: :class:`Cost` (USD) and :class:`TokenCount`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import math

from trackinizer.lib.custom_json import JsonCodec
from trackinizer.types.columns import Row


@dataclass(frozen=True, slots=True, kw_only=True)
class Cost:
    """The two USD costs an action can incur, kept together as one value.

    ``agent_usd`` is the acting agent's own LLM spend; ``resource_usd`` is
    any third-party compute it triggered (cloud GPUs and the like). Both
    default to 0, since humans and cascade paths leave them unset. Every
    ``Change`` row carries its own ``Cost``, and an inquiry's total is the
    sum across its log.

    On disk this lives as two flat columns, ``marginal_cost_agent_usd`` and
    ``marginal_cost_resource_usd``, on both the inquiries table and
    ``change_log``. The wrapper exists so call signatures and submit models
    pass one value instead of threading two floats through every layer.
    """

    agent_usd: float = 0.0
    """The agent's own LLM spend, in USD."""

    resource_usd: float = 0.0
    """Third-party compute spend the action triggered, in USD."""

    # A Cost may be negative in Python -- a subtraction (new - old) yields
    # a signed delta, and someday a cost axis may encode profit. The
    # storage columns are still constrained nonnegative for now; that
    # floor lives in the schema CHECK, not here.

    def __post_init__(self) -> None:
        """Reject a non-finite cost axis at the single value-type boundary.

        A NaN / inf axis defeats the storage floor-guard
        (``marginal_cost_agent_usd + 1 >= 0`` is always False for NaN) and
        poisons every running total. Guarding here covers every construction
        path -- wire submit, store deltas, CLI -- at once, the way the column
        ``CHECK`` cannot (it never sees a NaN that compares false against it).
        A finite negative is still allowed (a signed delta).
        """
        for axis, value in (
            ("agent_usd", self.agent_usd),
            ("resource_usd", self.resource_usd),
        ):
            if not math.isfinite(value):
                raise ValueError(f"Cost.{axis} must be finite; got {value!r}")

    @property
    def total_usd(self) -> float:
        """Both axes added together."""
        return self.agent_usd + self.resource_usd

    def __bool__(self) -> bool:
        """True when either axis carries a nonzero value."""
        return bool(self.agent_usd or self.resource_usd)

    def __add__(self, other: Cost) -> Cost:
        """Add two costs component-wise.

        To fold a sequence, seed the identity yourself with
        ``sum(costs, Cost())``; plain ``sum(costs)`` starts from ``int(0)``
        and fails.
        """
        return Cost(
            agent_usd=self.agent_usd + other.agent_usd,
            resource_usd=self.resource_usd + other.resource_usd,
        )

    def __sub__(self, other: Cost) -> Cost:
        """Subtract component-wise; the result may be negative.

        Lets ``change.new.marginal_cost - change.old.marginal_cost`` read
        off a per-event delta directly.
        """
        return Cost(
            agent_usd=self.agent_usd - other.agent_usd,
            resource_usd=self.resource_usd - other.resource_usd,
        )

    @classmethod
    def from_row(cls, row: Row, *, prefix: str = "") -> Self:
        """Build from the two ``<prefix>marginal_cost_*_usd`` columns.

        ``prefix`` is empty for an inquiries row, or ``"old_"`` / ``"new_"``
        for the two sides of a ``change_log`` row. A missing column reads as
        zero on that axis.
        """
        agent_col = prefix + "marginal_cost_agent_usd"
        resource_col = prefix + "marginal_cost_resource_usd"
        return cls(
            agent_usd=float(row[agent_col] or 0) if agent_col in row else 0.0,
            resource_usd=(
                float(row[resource_col] or 0) if resource_col in row else 0.0
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenCount(JsonCodec):
    """Per-turn LLM token usage, mirroring ``sagent.types.model.TokenCount``.

    Lives alongside :class:`Cost` because both are per-action accounting
    value types. Only a model call (an ``AssistantMessage`` turn, or a
    compaction) spends tokens; user input and tool results don't. USD cost
    is inferred from these counts plus the model's pricing, not stored.
    """

    input_tokens: int = 0
    """Prompt tokens the model read."""

    output_tokens: int = 0
    """Tokens the model generated."""

    cache_creation_tokens: int = 0
    """Tokens spent creating prompt-cache breakpoints."""

    cache_read_tokens: int = 0
    """Tokens served from the prompt cache."""

    def __add__(self, other: TokenCount) -> TokenCount:
        """Add two counts component-wise.

        To fold a sequence, seed the identity yourself with
        ``sum(counts, TokenCount())``; plain ``sum(counts)`` starts from
        ``int(0)`` and fails.
        """
        return TokenCount(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=(
                self.cache_creation_tokens + other.cache_creation_tokens
            ),
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    def __sub__(self, other: TokenCount) -> TokenCount:
        """Subtract component-wise, clamped at zero per axis.

        Token counts are physical tallies, never negative; a subtraction
        that would underflow an axis floors it at ``0``.
        """
        return TokenCount(
            input_tokens=max(0, self.input_tokens - other.input_tokens),
            output_tokens=max(0, self.output_tokens - other.output_tokens),
            cache_creation_tokens=max(
                0, self.cache_creation_tokens - other.cache_creation_tokens
            ),
            cache_read_tokens=max(0, self.cache_read_tokens - other.cache_read_tokens),
        )
