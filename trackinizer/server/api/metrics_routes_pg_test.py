"""Experiment-metric rank on real PostgreSQL, where a statement timeout fires.

PGlite ignores ``statement_timeout``: under ``SET LOCAL statement_timeout = 50`` it
runs ``pg_sleep(1)`` to completion. Only a real server can show a rank filter's
regex being cut off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import time

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server import regex_timeout
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.wire.bodies import SubmitExperiment
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_metrics_query import METRIC_RANK_PATH


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(loop_scope="session")
async def served(integ_store: Store) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve the whole app on the real engine, as a viewer-capable writer."""
    app.state.engine = integ_store.engine
    app.state.store = integ_store
    app.state.inbound = InboundQueue()
    install_identity(make_test_identity(api_key_id=None))
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http
    finally:
        clear_identity_override()
        del app.state.engine, app.state.store, app.state.inbound


async def test_rank_filter_regex_stops_at_the_statement_timeout(
    served: httpx2.AsyncClient,
    integ_store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow ``title re`` rank filter is cancelled at the list's bound, as a 400.

    The backreference pattern costs live PG18 3.4s on this title; uncapped, the
    rank ran it to the end and answered 200.
    """
    eid = await integ_store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title="ab" * 1_000 + "c"),
    )
    await integ_store.log_metrics(eid, [MetricPoint(key="score", step=0, value=1.0)])
    monkeypatch.setattr(regex_timeout, "STATEMENT_TIMEOUT_MS", 100)
    started = time.perf_counter()
    ranked = await served.post(
        METRIC_RANK_PATH,
        json={
            "filters": [{"field": "title", "op": "re", "value": r"^(.*)(.*)\1\2$"}],
            "query": {},
        },
    )
    elapsed_sec = time.perf_counter() - started
    assert (ranked.status_code, elapsed_sec < 1) == (400, True), (
        ranked.text,
        elapsed_sec,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
