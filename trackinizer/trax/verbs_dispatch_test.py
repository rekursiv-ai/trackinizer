"""Regressions for preflighted metric writes and relation indices."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import argparse

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import verbs
from trackinizer.trax.conftest import FakeClient, run
from trackinizer.wire.refs import SeqRef


if TYPE_CHECKING:
    from trackinizer.client.client import Client


@pytest.mark.parametrize("before", [["title", "to", "x"], ["2"]])
@pytest.mark.parametrize(
    ("tail", "message"),
    [
        (["at", "loss", "to", "0.5"], "must mask 'step'"),
        (["to", "0.5"], "must mask 'step'"),
        (["at", "step", "is", "3", "to", "high"], "must be a number"),
        (["at", "step", "is", "3", "to", "nan"], "must be finite"),
        (["at", "step", "is", "3", "to", "inf"], "must be finite"),
        (["at", "step", "is", "3", "to", "--", "-inf"], "must be finite"),
        (["at", "step", "is", "3", "to", "1e999"], "must be finite"),
        (["at", "step", "re", "3", "to", "0.5"], "unknown metric operator"),
        (["at", "step", "is", "3", "to", "0.5", "limit", "1"], "reads"),
        (["at", "loss", "at", "step", "max", "to", "0.5"], "read-only"),
        (["at", "loss", "at", "step", "min", "to", "0.5"], "read-only"),
        (["at", "step", "is", "abc", "to", "0.5"], "valid metric step"),
        (["at", "step", "is", "-1", "to", "0.5"], "valid metric step"),
        (["at", "step", "is", str(2**63), "to", "0.5"], "valid metric step"),
        (["at", "step", "is", "9" * 4301, "to", "0.5"], "valid metric step"),
        (["at", "step", "is", "3.0", "to", "0.5"], "valid metric step"),
        (
            ["at", "step", "is", "3", "at", "value", "lt", "NaN", "to", "0.5"],
            "valid metric value",
        ),
        (
            ["at", "step", "is", "3", "at", "value", "lt", "inf", "to", "0.5"],
            "valid metric value",
        ),
        (
            ["at", "step", "is", "3", "at", "value", "lt", "--", "-inf", "to", "0.5"],
            "valid metric value",
        ),
        (
            ["at", "step", "is", "3", "at", "value", "lt", "high", "to", "0.5"],
            "valid metric value",
        ),
        (
            ["at", "step", "is", "3", "at", "key", "is", "", "to", "0.5"],
            "valid metric key",
        ),
        (
            ["at", "step", "is", "3", "at", "key", "is", "\t", "to", "0.5"],
            "valid metric key",
        ),
        (
            ["at", "step", "is", "3", "at", "key", "is", "lo\x00ss", "to", "0.5"],
            "valid metric key",
        ),
        (
            ["at", "step", "is", "3", "at", "key", "is", "x" * 513, "to", "0.5"],
            "valid metric key",
        ),
    ],
)
def test_invalid_metric_write_makes_no_client_calls(
    client: FakeClient,
    before: list[str],
    tail: list[str],
    message: str,
) -> None:
    with pytest.raises(ClientError, match=message):
        run(["experiment", *before, "metric", *tail], client)
    assert client.calls == []


@pytest.mark.parametrize("index", ["²", "①"])
def test_relation_index_rejects_non_decimal_digits(
    client: FakeClient,
    index: str,
) -> None:
    with pytest.raises(ClientError, match="relation index must be a positive integer"):
        verbs.Kind.run_relation(
            SeqRef(kind="Issue", seq=1),
            ("requires", True),
            [index],
            argparse.Namespace(sort="", format_="table"),
            lambda: cast("Client", client),
        )


def test_relation_index_reports_overlong_decimal(client: FakeClient) -> None:
    with pytest.raises(ClientError):
        verbs.Kind.run_relation(
            SeqRef(kind="Issue", seq=1),
            ("requires", True),
            ["9" * 4301],
            argparse.Namespace(sort="", format_="table"),
            lambda: cast("Client", client),
        )


@pytest.mark.parametrize("index", ["7", "07", "٧", "７"])
def test_relation_index_normalizes_decimal_peer_sequence(index: str) -> None:
    rows: list[dict[str, object]] = [{"seq": 7}]
    assert verbs.Kind._select_relation_row(rows, "requires", index) == rows[0]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
