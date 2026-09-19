"""Regression tests for shared Trackinizer fixtures."""

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import inspect
import random

import pytest


# pytest-postgresql imports psycopg, and psycopg without libpq raises a plain
# ImportError, which ``importorskip`` passes through unless told otherwise.
pytest.importorskip("psycopg", exc_type=ImportError)

from port_for import api
from pytest_postgresql import janitor
from pytest_postgresql.config import get_config
from pytest_postgresql.executors.proc import PostgreSQLExecutor
from pytest_postgresql.factories import process
from pytest_postgresql.plugin import postgresql_proc

from trackinizer import conftest


@pytest.mark.parametrize(("workers", "retries"), [(7, 5), (1, 9)])
def test_pg_dsn_reserves_ports_for_seeded_workers(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: int,
    retries: int,
) -> None:
    """Cover concurrent reservations and preserve a larger configured budget."""
    (tmp_path / "worker").mkdir()
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", str(workers))
    monkeypatch.setattr(request.config, "workerinput", {}, raising=False)
    monkeypatch.setattr(request.config.option, "postgresql_port_search_count", retries)
    monkeypatch.setattr(tmp_path_factory, "getbasetemp", lambda: tmp_path / "worker")
    # Port policy discovery is independent of exclusive reservation. Keep the
    # real RNG selection, socket availability checks, and sentinel lifecycle.
    monkeypatch.setattr(api, "available_good_ports", lambda: set(range(10_000, 10_032)))
    monkeypatch.setattr(process, "_pg_exe", MagicMock(return_value=Path("/unused")))
    executor = MagicMock(
        host="localhost",
        user="postgres",
        password=None,
        dbname="test",
    )
    executor.logfile = str(tmp_path / "unused.log")
    monkeypatch.setattr(process, "PostgreSQLExecutor", MagicMock(return_value=executor))
    monkeypatch.setattr(process, "DatabaseJanitor", MagicMock())
    # ``pg_dsn`` imports ``DatabaseJanitor`` when it runs, so patch it at the source.
    monkeypatch.setattr(janitor, "DatabaseJanitor", MagicMock())
    fixture = contextmanager(
        cast(
            Callable[
                [pytest.FixtureRequest, pytest.TempPathFactory],
                Generator[PostgreSQLExecutor],
            ],
            inspect.unwrap(postgresql_proc),
        ),
    )
    dsn_fixture = contextmanager(
        cast(
            Callable[[pytest.FixtureRequest], Generator[str]],
            inspect.unwrap(conftest.pg_dsn),
        ),
    )
    with ExitStack() as stack:

        def get_fixture(name: str) -> PostgreSQLExecutor:
            assert name == "postgresql_proc"
            return stack.enter_context(fixture(request, tmp_path_factory))

        fixture_request = MagicMock(spec=pytest.FixtureRequest, config=request.config)
        fixture_request.getfixturevalue.side_effect = get_fixture
        for _ in range(workers):
            random.seed(1337)
            stack.enter_context(
                dsn_fixture(cast(pytest.FixtureRequest, fixture_request)),
            )
        assert len(list(tmp_path.glob("*.port"))) == workers
        assert get_config(request).port_search_count == max(retries, workers - 1)
    assert not list(tmp_path.glob("*.port"))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
