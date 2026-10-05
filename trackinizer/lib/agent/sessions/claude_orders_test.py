"""Tests for Claude record key orders."""

from __future__ import annotations

from trackinizer.lib.agent.sessions.claude_orders import failed_order


def test_failed_order_preserves_provider_diagnostics_order() -> None:
    assert failed_order()[:6] == (
        "parentUuid",
        "isSidechain",
        "type",
        "uuid",
        "timestamp",
        "message",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
