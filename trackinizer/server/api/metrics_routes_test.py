"""Experiment-metric routes, end to end over PGlite.

Each test replays a request a reviewer sent to the live app, so the assertion
is the HTTP status a client sees -- the store's error mapping, the app's
exception handlers, and the response serializer all in the path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.wire.bodies import SubmitExperiment, SubmitIssue
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.wire_metrics import MetricPoint, experiment_metrics_path
from trackinizer.wire.wire_metrics_query import (
    METRIC_RANK_PATH,
    experiment_metric_query_path,
    experiment_metric_write_path,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


_BIGINT_OVERFLOW = str(2**63)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_masked_write_cannot_store_a_key_reads_reject(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A tab key the read would refuse is refused at the write instead."""
    client, store = pglite_route_client
    eid = await _experiment(store, [])
    written = await client.post(
        experiment_metric_write_path(eid),
        json={"masks": _cell("\t", "0"), "write": 1.0},
    )
    assert written.status_code == 409
    assert (await client.get(experiment_metrics_path(eid))).status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_nul_key_is_a_client_error_on_every_path(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Postgres ``text`` cannot hold NUL; no path may hand it one."""
    client, store = pglite_route_client
    eid = await _experiment(store, [])
    logged = await client.post(
        experiment_metrics_path(eid),
        json={"points": [{"key": "lo\u0000ss", "step": 0, "value": 1.0}]},
    )
    written = await client.post(
        experiment_metric_write_path(eid),
        json={"masks": _cell("lo\u0000ss", "0"), "write": 1.0},
    )
    queried = await client.post(
        experiment_metric_query_path(eid),
        json={"masks": [{"axis": "key", "op": "is", "value": "lo\u0000ss"}]},
    )
    assert (logged.status_code, written.status_code, queried.status_code) == (
        422,
        409,
        409,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_step_operand_outside_bigint_is_a_client_error(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An operand the ``bigint`` cast would overflow never reaches SQL."""
    client, store = pglite_route_client
    eid = await _experiment(store, [MetricPoint(key="loss", step=0, value=0.5)])
    step_mask = {"axis": "step", "op": "gt", "value": _BIGINT_OVERFLOW}
    queried = await client.post(
        experiment_metric_query_path(eid),
        json={"masks": [step_mask]},
    )
    written = await client.post(
        experiment_metric_write_path(eid),
        json={"masks": _cell("loss", _BIGINT_OVERFLOW), "write": 1.0},
    )
    negative = await client.post(
        experiment_metric_write_path(eid),
        json={"masks": [{"axis": "step", "op": "is", "value": "-1"}], "write": 1.0},
    )
    assert (queried.status_code, written.status_code, negative.status_code) == (
        409,
        409,
        409,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_nan_operand_is_refused_not_ordered_above_every_number(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Postgres sorts NaN above every float, so ``value lt NaN`` matched all."""
    client, store = pglite_route_client
    eid = await _experiment(
        store,
        [
            MetricPoint(key="loss", step=0, value=0.2),
            MetricPoint(key="loss", step=1, value=0.3),
        ],
    )
    written = await client.post(
        experiment_metric_write_path(eid),
        json={
            "masks": [
                {"axis": "step", "op": "ge", "value": "0"},
                {"axis": "value", "op": "lt", "value": "NaN"},
            ],
            "write": 9.0,
        },
    )
    assert written.status_code == 409
    assert [p.value for p in await store.read_metrics(eid)] == [0.2, 0.3]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_each_route_refuses_the_other_operations_controls(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A write takes no ``sort``/``limit``; a read or rank takes no ``write``."""
    client, store = pglite_route_client
    eid = await _experiment(
        store,
        [MetricPoint(key="loss", step=step, value=0.5) for step in range(3)],
    )
    every_step = [{"axis": "step", "op": "ge", "value": "0"}]
    written = await client.post(
        experiment_metric_write_path(eid),
        json={"masks": every_step, "write": 9.0, "limit": 1},
    )
    queried = await client.post(
        experiment_metric_query_path(eid),
        json={"masks": every_step, "write": 9.0},
    )
    ranked = await client.post(
        METRIC_RANK_PATH,
        json={"experiment_ids": [str(eid)], "query": {"write": 9.0}},
    )
    assert (written.status_code, queried.status_code, ranked.status_code) == (
        422,
        400,
        400,
    )
    assert [p.value for p in await store.read_metrics(eid)] == [0.5, 0.5, 0.5]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_query_takes_one_reduction_with_no_operand(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """``[max, min]`` and ``[min, max]`` returned different cells; both refuse."""
    client, store = pglite_route_client
    eid = await _experiment(
        store,
        [
            MetricPoint(key="loss", step=1, value=10.0),
            MetricPoint(key="loss", step=2, value=20.0),
        ],
    )
    path = experiment_metric_query_path(eid)
    statuses = [
        (await client.post(path, json={"masks": masks})).status_code
        for masks in (
            [_reduce("max"), _reduce("min")],
            [_reduce("min"), _reduce("max")],
            [{"axis": "step", "op": "max", "value": "1"}],
        )
    ]
    assert statuses == [409, 409, 409]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rank_returns_every_cell_up_to_the_cap_and_refuses_more(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An omitted ``limit`` is the cap, not a silent page of 50."""
    client, store = pglite_route_client
    eid = await _experiment(
        store,
        [MetricPoint(key="loss", step=step, value=0.5) for step in range(60)],
    )
    ranked = await client.post(
        METRIC_RANK_PATH,
        json={"experiment_ids": [str(eid)], "query": {"sort": "desc"}},
    )
    over_limit = await client.post(
        METRIC_RANK_PATH,
        json={"experiment_ids": [str(eid)], "query": {"limit": MAX_LIST_LIMIT + 1}},
    )
    over_ids = await client.post(
        METRIC_RANK_PATH,
        json={
            "experiment_ids": [str(uuid.uuid4()) for _ in range(MAX_LIST_LIMIT + 1)],
            "query": {},
        },
    )
    assert ranked.status_code == 200
    rows = from_plain(
        from_plain(loads(ranked.content), dict[str, object])["rows"],
        list[object],
    )
    assert len(rows) == 60
    assert (over_limit.status_code, over_ids.status_code) == (422, 422)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_read_refuses_ids_that_are_not_experiments(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A missing id is 404 and a non-Experiment 409, not an empty 200."""
    client, store = pglite_route_client
    issue_id = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="not a run"),
    )
    missing = await client.get(experiment_metrics_path(uuid.uuid4()))
    not_run = await client.get(experiment_metrics_path(issue_id))
    assert (missing.status_code, not_run.status_code) == (404, 409)


async def _experiment(store: Store, points: list[MetricPoint]) -> uuid.UUID:
    """Create an Experiment and log ``points`` against it."""
    eid = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="run"),
    )
    if points:
        await store.log_metrics(eid, points)
    return eid


def _cell(key: str, step: str) -> list[dict[str, str]]:
    """Return the masks pinning one ``(key, step)`` cell."""
    return [
        {"axis": "key", "op": "is", "value": key},
        {"axis": "step", "op": "is", "value": step},
    ]


def _reduce(op: str) -> dict[str, str]:
    """Return a step-axis reduction clause."""
    return {"axis": "step", "op": op}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
