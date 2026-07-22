"""Tests for the trackinizer CLI entry point."""

from __future__ import annotations

from typing import Any

import argparse
import inspect
import logging

import pytest
import uvicorn
import uvicorn.server

from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.api.app import app
from trackinizer.server.config import (
    Config,
    ConfigError,
    build_engine,
)
from trackinizer.server.route_iter import registered_paths
from trackinizer.server.server import (
    _configure_logging,
    _parse_args,
    _SuppressZeroTaskCancel,
    logger,
    main,
)


class TestPureFunctions:
    def test_configure_logging_none_is_noop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_LOG_LEVEL", raising=False)
        _configure_logging(None)

    def test_configure_logging_sets_level(self) -> None:
        _configure_logging("DEBUG")
        assert logger.level == logging.DEBUG

    def test_configure_logging_env_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_LOG_LEVEL", "INFO")
        _configure_logging(None)
        assert logger.level == logging.INFO

    def test_configure_logging_invalid_raises(self) -> None:
        with pytest.raises(SystemExit):
            _configure_logging("not-a-level")

    def test_parse_args_defaults(self) -> None:
        parser = argparse.ArgumentParser()
        args, remaining = _parse_args(parser, [])
        assert args.engine == "pglite"
        assert args.host == "127.0.0.1"
        assert args.port == 8765
        assert args.embedder == "stub"
        assert remaining == []

    def test_parse_args_overrides(self) -> None:
        parser = argparse.ArgumentParser()
        args, _ = _parse_args(
            parser,
            ["--engine", "pg", "--dsn", "x", "--port", "9000"],
        )
        assert args.engine == "pg"
        assert args.dsn == "x"
        assert args.port == 9000


class TestCLIHelpers:
    def test_main_invokes_uvicorn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: dict[str, Any] = {}

        def fake_run(
            app: Any,
            *,
            host: str,
            port: int,
            workers: int,
            timeout_keep_alive: int,
            timeout_graceful_shutdown: int,
        ) -> None:
            called["app"] = app
            called["host"] = host
            called["port"] = port
            called["workers"] = workers
            called["timeout_keep_alive"] = timeout_keep_alive
            called["timeout_graceful_shutdown"] = timeout_graceful_shutdown

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr("sys.argv", ["trackinizer", "--port", "1234"])
        main()
        assert called["host"] == "127.0.0.1"
        assert called["port"] == 1234
        assert called["workers"] == 1
        assert called["timeout_keep_alive"] == 240
        # Force-close on shutdown (don't wait for connections to drain); the
        # zero-task cancel ERROR this causes is suppressed by the log filter.
        assert called["timeout_graceful_shutdown"] == 0


class TestSuppressZeroTaskCancel:
    """The shutdown-noise filter drops only the spurious zero-task cancel."""

    @staticmethod
    def _record(message: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord(
            name="uvicorn.error",
            level=logging.ERROR,
            pathname=__file__,
            lineno=0,
            msg=message,
            args=args,
            exc_info=None,
        )

    def test_drops_zero_task_cancel(self) -> None:
        filt = _SuppressZeroTaskCancel()
        # The exact message uvicorn emits when graceful-shutdown expires with no
        # tasks (timeout_graceful_shutdown=0 trips this on every clean exit).
        rec = self._record(
            "Cancel %s running task(s), timeout graceful shutdown exceeded", 0
        )
        assert filt.filter(rec) is False

    def test_keeps_real_cancel(self) -> None:
        filt = _SuppressZeroTaskCancel()
        rec = self._record(
            "Cancel %s running task(s), timeout graceful shutdown exceeded", 3
        )
        assert filt.filter(rec) is True

    def test_keeps_unrelated_error(self) -> None:
        filt = _SuppressZeroTaskCancel()
        assert filt.filter(self._record("some other uvicorn error")) is True

    def test_matches_uvicorns_actual_format_string(self) -> None:
        """Guard against uvicorn rewording the message out from under the filter.

        The filter matches a formatted string; if a uvicorn upgrade changes the
        template, the suppression silently stops working. Pin it: the zero-task
        formatting of uvicorn's own ``Server.shutdown`` source must still equal
        the string the filter drops.
        """
        source = inspect.getsource(uvicorn.server.Server.shutdown)
        assert (
            "Cancel %s running task(s), timeout graceful shutdown exceeded" in source
        ), (
            "uvicorn reworded its shutdown-cancel message; update "
            "_SuppressZeroTaskCancel to match"
        )

    def test_main_unrecognized_args_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def noop(*a: object, **k: object) -> None:
            del a, k

        monkeypatch.setattr(uvicorn, "run", noop)
        monkeypatch.setattr("sys.argv", ["trackinizer", "--bogus"])
        with pytest.raises(SystemExit):
            main()

    def test_main_pglite_with_workers_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--workers >1`` against PGlite must error out -- it corrupts the workdir."""

        def noop(
            app: Any,
            *,
            host: str,
            port: int,
            workers: int,
            timeout_keep_alive: int,
        ) -> None:
            del app, host, port, workers, timeout_keep_alive

        monkeypatch.setattr(uvicorn, "run", noop)
        monkeypatch.setattr(
            "sys.argv",
            ["trackinizer", "--engine", "pglite", "--workers", "2"],
        )
        with pytest.raises(SystemExit):
            main()


class TestCoverageRoutesAndCli:
    def test_main_web_branch_and_engine_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: dict[str, object] = {}

        def fake_run(
            passed_app: object,
            *,
            host: str,
            port: int,
            workers: int,
            timeout_keep_alive: int,
            timeout_graceful_shutdown: int,
        ) -> None:
            called["host"] = host
            called["port"] = port
            called["workers"] = workers
            called["timeout_keep_alive"] = timeout_keep_alive
            called["timeout_graceful_shutdown"] = timeout_graceful_shutdown
            called["web_attached"] = "/api/web/search" in registered_paths(app)
            del passed_app

        monkeypatch.setattr("sys.argv", ["trackinizer", "--web", "--port", "9999"])
        monkeypatch.setattr(uvicorn, "run", fake_run)
        main()
        assert called == {
            "host": "127.0.0.1",
            "port": 9999,
            "workers": 1,
            "timeout_keep_alive": 240,
            "timeout_graceful_shutdown": 0,
            "web_attached": True,
        }
        with pytest.raises(ConfigError):
            build_engine(Config(engine="pg"))
        assert isinstance(
            build_engine(Config(engine="pg", dsn="postgres:///x")),
            PostgresEngine,
        )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
