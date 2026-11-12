"""Tests for a session's token and cost totals: each CLI's usage, counted once."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.server.visuals.session_usage import session_usage
from trackinizer.types.session_records import SessionRecordRow


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from treekle import PlainTree

    from trackinizer.sagent.model.capability import ModelCapability


_SESSION = uuid.UUID("5d0c4b6e-7a59-4d3a-9d3e-2d2f3b6c1a10")
_DAY = date(2026, 11, 6)


def _rows(
    *records: tuple[str, Mapping[str, PlainTree]],
    part: int = 0,
) -> list[SessionRecordRow]:
    return [
        SessionRecordRow(
            session_id=_SESSION,
            part=part,
            idx=idx,
            kind=kind,
            payload=payload,
        )
        for idx, (kind, payload) in enumerate(records)
    ]


def _answer(
    request: str,
    model: str = "claude-opus-5-5",
) -> tuple[str, Mapping[str, PlainTree]]:
    return (
        "AssistantMessage",
        {"extra": {"requestId": request, "message": {"model": model}}},
    )


def _claude(**tokens: int) -> tuple[str, Mapping[str, PlainTree]]:
    return ("TokenUsage", {"info": dict(tokens)})


def _codex(
    input_tokens: int,
    cached: int,
    output: int,
) -> tuple[str, Mapping[str, PlainTree]]:
    total = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
    }
    return (
        "TokenUsage",
        {
            "info": {
                "total_token_usage": total,
                "last_token_usage": total,
                "model_context_window": 258_400,
            },
        },
    )


def test_a_claude_call_whose_lines_repeat_its_usage_counts_once() -> None:
    call = {
        "input_tokens": 2,
        "cache_read_input_tokens": 40_000,
        "cache_creation_input_tokens": 700,
        "output_tokens": 30,
    }
    rows = _rows(
        (
            "Thinking",
            {"extra": {"requestId": "r1", "message": {"model": "claude-opus-5-5"}}},
        ),
        _claude(**call),
        _answer("r1"),
        _claude(**call),
        _answer("r2"),
        _claude(input_tokens=5, cache_read_input_tokens=41_000, output_tokens=10),
    )
    usage = session_usage(rows, formats={0: "claude"}, today=_DAY)
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (
        40_702 + 41_005,
        40,
        81_000,
    )
    assert usage.context_tokens == 41_005
    assert usage.context_window is not None
    assert usage.context_window > 41_005
    assert usage.usd is not None
    assert usage.usd > 0


def test_a_claude_call_on_fable_5_2_is_priced_at_its_list_prices() -> None:
    """Per MTok: $8 in, $10 a 5m and $16 a 1h cache write, $0.20 a cache read, $40 out."""
    usage_info: Mapping[str, PlainTree] = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 3_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
        "cache_read_input_tokens": 10_000_000,
        "output_tokens": 1_000_000,
    }
    rows = _rows(
        _answer("r1", model="claude-fable-5-2"),
        ("TokenUsage", {"info": usage_info}),
    )
    usage = session_usage(rows, formats={0: "claude"}, today=_DAY)
    assert usage.usd == pytest.approx(8 + 2 * 10 + 16 + 10 * 0.2 + 40)
    assert usage.context_window == 1_000_000


def test_codex_counts_what_its_running_totals_grew_by() -> None:
    rows = _rows(
        ("TurnContext", {"model": "gpt-5.6-sol"}),
        _codex(1_000, 200, 50),
        _codex(1_000, 200, 50),
        _codex(2_500, 900, 80),
    )
    usage = session_usage(rows, formats={0: "codex"}, today=_DAY)
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (
        2_500,
        80,
        900,
    )
    assert usage.context_window == 258_400
    assert usage.usd is not None


def test_sagent_states_its_own_spend() -> None:
    rows = _rows(
        ("TokenUsage", {"info": {"spend_usd": 0.25}}),
        ("TokenUsage", {"info": {"spend_usd": 0.5}}),
    )
    usage = session_usage(rows, formats={0: "sagent"}, today=_DAY)
    assert usage.usd == pytest.approx(0.75)
    assert usage.input_tokens == 0


def test_one_call_is_enough_to_know_the_cost() -> None:
    rows = _rows(("TokenUsage", {"info": {"spend_usd": 0.25}}))
    usage = session_usage(rows, formats={0: "sagent"}, today=_DAY)
    assert usage.usd == pytest.approx(0.25)


def test_a_model_the_tables_do_not_know_leaves_the_cost_unknown_not_low() -> None:
    def known(cli: str, model: str) -> ModelCapability | None:
        del cli, model
        return None

    rows = _rows(
        _answer("r1", model="claude-nonesuch"),
        _claude(input_tokens=10, output_tokens=5),
    )
    usage = session_usage(rows, formats={0: "claude"}, today=_DAY, capability=known)
    assert (usage.input_tokens, usage.usd) == (10, None)


def test_a_model_sagents_tables_do_not_know_leaves_the_cost_unknown() -> None:
    """Priced by sagent's own tables, which name no such model."""
    rows = _rows(
        _answer("r1", model="claude-nonesuch"),
        _claude(input_tokens=10, output_tokens=5),
    )
    usage = session_usage(rows, formats={0: "claude"}, today=_DAY)
    assert (usage.input_tokens, usage.usd, usage.context_window) == (10, None, None)


@pytest.mark.parametrize(
    ("cli", "model"),
    [("claude", ""), ("gemini", "claude-opus-5-5")],
)
def test_a_call_with_no_model_or_no_priced_cli_leaves_the_cost_unknown(
    cli: str,
    model: str,
) -> None:
    """No model named, or a format sagent's tables do not price: no guess."""
    rows = _rows(
        (
            "AssistantMessage",
            {"extra": {"requestId": "r1", "message": {"model": model}}},
        ),
        _claude(input_tokens=10, output_tokens=5),
    )
    usage = session_usage(rows, formats={0: cli}, today=_DAY)
    assert (usage.input_tokens, usage.usd) == (10, None)


def test_no_calls_cost_nothing_known() -> None:
    rows: Sequence[SessionRecordRow] = _rows(("UserMessage", {"content": "hi"}))
    usage = session_usage(rows, formats={0: "claude"}, today=_DAY)
    assert (usage.input_tokens, usage.context_tokens, usage.usd) == (0, None, None)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
