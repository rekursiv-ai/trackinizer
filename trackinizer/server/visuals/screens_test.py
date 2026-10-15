"""Tests for the records a route names, and what a sender's visuals carry."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

import pytest

from trackinizer.server.visuals.screens import _named, read_screen
from trackinizer.server.visuals.workspaces import VisualInstance


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_ID = uuid.UUID("889ffcb2-cf44-43e7-9806-eb08428c6203")


@pytest.mark.asyncio
async def test_each_visual_carries_its_record_and_its_params_as_the_canvas_holds() -> (
    None
):
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"id": _ID, "kind": "Issue", "seq": 12, "title": "Brief"}],
    )
    narrowed: dict[str, str | int | bool] = {
        "path": "pkg/",
        "changed": True,
        "file": "bar.py",
    }

    screen = await read_screen(
        cast("Conn", conn),
        page=None,
        trail=[],
        visuals=[
            VisualInstance(
                id=uuid.uuid4(),
                type="trax.files",
                version=1,
                record_id=_ID,
                params=narrowed,
            ),
            VisualInstance(id=uuid.uuid4(), type="trax.browse", version=1),
        ],
    )

    assert [
        (each.type, each.record and each.record.seq, each.params)
        for each in screen.visuals
    ] == [("trax.files", 12, narrowed), ("trax.browse", None, {})]


@pytest.mark.parametrize(
    ("route", "named"),
    [
        (f"#/lookup/{_ID}", _ID),
        (f"#/inquiry/{str(_ID).upper()}", _ID),
        ("#/ref/Experiment/407", ("Experiment", 407)),
        ("#/ref/experiment/407/", ("Experiment", 407)),
        ("#/ref/Exp%65riment/407", ("Experiment", 407)),
        (f"#/graph?focus={_ID}&hops=2", _ID),
        ("#/graph?focus=issue/11&hops=2", ("Issue", 11)),
        ("#/graph?group=none&focus=Belief/3", ("Belief", 3)),
    ],
)
def test_a_route_that_names_a_record_is_that_record(
    route: str,
    named: uuid.UUID | tuple[str, int],
) -> None:
    assert _named(route) == named


@pytest.mark.parametrize(
    "route",
    [
        "#/",
        "#/graph",
        "#/graph?focus=Issue/11/12",
        "#/graph?focus=Nothing/11",
        "#/list/Issue",
        "#/lookup/not-a-uuid",
        f"#/lookup/{_ID}/more",
        "#/ref/Issue",
        "#/ref/Issue/-1",
        "#/ref/Issue/1e3",
        "#/ref/Issue/12345678901234567890",
        "#/ref/Unknown/7",
        "#/search/Issue/7",
    ],
)
def test_a_route_that_names_no_record_names_none(route: str) -> None:
    assert _named(route) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
