"""Tests for the statement-timeout bound on queries carrying a caller's regex."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from trackinizer.conftest import make_conn
from trackinizer.server.regex_timeout import (
    STATEMENT_TIMEOUT_MS,
    _statement_timeout_ms,
    apply_regex_statement_timeout,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_ENV = "TRACKINIZER_SEARCH_TIMEOUT_MS"


@pytest.mark.parametrize("raw", ["", "   "], ids=["empty", "blank"])
def test_an_unset_override_is_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv(_ENV, raw)

    assert _statement_timeout_ms() == 5_000


def test_an_absent_override_is_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    assert _statement_timeout_ms() == 5_000


def test_a_positive_override_is_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, " 250 ")

    assert _statement_timeout_ms() == 250


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_a_non_positive_override_raises_rather_than_disabling_the_bound(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv(_ENV, raw)

    with pytest.raises(ValueError, match="must be a positive integer"):
        _ = _statement_timeout_ms()


def test_a_non_integer_override_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "5s")

    with pytest.raises(ValueError, match="invalid literal"):
        _ = _statement_timeout_ms()


@pytest.mark.asyncio
async def test_the_bound_is_set_local_on_the_connection() -> None:
    conn = make_conn()

    await apply_regex_statement_timeout(cast("Conn", conn))

    conn.execute.assert_awaited_once_with(
        f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
