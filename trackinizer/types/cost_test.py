"""Tests for the ``Cost`` value type."""

from __future__ import annotations

import math

import pytest

from trackinizer.types.cost import Cost, TokenCount


class TestModels:
    def test_cost_rejects_nonfinite_components(self) -> None:
        """NaN / inf on a cost axis is malformed at the type boundary (K2).

        A non-finite ``agent_usd`` defeats the storage floor-guard
        (``marginal_cost_agent_usd + 1 >= 0`` is always False for NaN) and
        poisons every running total. Rejecting it in ``__post_init__`` covers
        every construction path (wire submit, store deltas, CLI) at once.
        """
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(ValueError, match="finite"):
                Cost(agent_usd=bad)
            with pytest.raises(ValueError, match="finite"):
                Cost(resource_usd=bad)

    def test_cost_allows_negative_components(self) -> None:
        # A Cost may be negative in Python: a subtraction yields a signed
        # delta, and a cost axis may someday encode profit. The nonnegative
        # floor lives in the storage CHECK, not the value type.
        c = Cost(agent_usd=-0.01, resource_usd=-0.5)
        assert c.agent_usd == pytest.approx(-0.01)
        assert c.resource_usd == pytest.approx(-0.5)

    def test_sub_yields_signed_delta(self) -> None:
        new = Cost(agent_usd=1.02, resource_usd=4.0)
        old = Cost(agent_usd=1.0, resource_usd=2.5)
        delta = new - old
        assert delta.agent_usd == pytest.approx(0.02)
        assert delta.resource_usd == pytest.approx(1.5)

    def test_sub_can_go_negative(self) -> None:
        delta = Cost(agent_usd=0.5) - Cost(agent_usd=2.0)
        assert delta.agent_usd == pytest.approx(-1.5)

    def test_a_cost_is_truthy_when_either_axis_is_nonzero(self) -> None:
        assert not Cost()
        assert Cost(agent_usd=0.5)
        assert Cost(resource_usd=-1.0)
        assert Cost(agent_usd=1.0, resource_usd=2.5).total_usd == pytest.approx(3.5)

    def test_add_is_componentwise(self) -> None:
        total = Cost(agent_usd=1.0) + Cost(agent_usd=0.5, resource_usd=2.0)
        assert total.agent_usd == pytest.approx(1.5)
        assert total.resource_usd == pytest.approx(2.0)


class TestTokenCount:
    def test_add_is_componentwise(self) -> None:
        total = TokenCount(input_tokens=1, output_tokens=2) + TokenCount(
            input_tokens=10,
            cache_creation_tokens=3,
            cache_read_tokens=4,
        )
        assert total == TokenCount(
            input_tokens=11,
            output_tokens=2,
            cache_creation_tokens=3,
            cache_read_tokens=4,
        )

    def test_sub_clamps_each_axis_at_zero(self) -> None:
        delta = TokenCount(
            input_tokens=5,
            output_tokens=1,
            cache_creation_tokens=7,
            cache_read_tokens=2,
        ) - TokenCount(input_tokens=2, output_tokens=4, cache_read_tokens=9)
        assert delta == TokenCount(input_tokens=3, cache_creation_tokens=7)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
