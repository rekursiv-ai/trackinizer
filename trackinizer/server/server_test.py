"""Tests for the trackinizer CLI entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

import argparse
import inspect
import logging
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.server import Server

import pytest
import uvicorn

from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server import server, web
from trackinizer.server.api.app import app
from trackinizer.server.auth import AuthIdentity
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
from trackinizer.server.visuals.catalog import Workspace


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def restore_global_log_levels() -> Iterator[None]:
    """Undo ``_configure_logging``'s process-wide effects after every test.

    ``_configure_logging`` calls ``logging.basicConfig``, which sets the level
    of the ROOT logger -- global to the whole pytest session, not scoped to
    this module. Any test here that raises it to WARNING or above silences a
    later module's ``caplog`` assertion, and the failure surfaces in an
    unrelated file whose order happens to put it second.

    Autouse at module scope rather than per-class: both the direct callers and
    the tests that reach ``_configure_logging`` through ``main`` mutate it.
    """
    root = logging.getLogger()
    root_level, package_level = root.level, logger.level
    yield
    root.setLevel(root_level)
    logger.setLevel(package_level)


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

    def test_configure_logging_returns_resolved_level(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_LOG_LEVEL", raising=False)
        assert _configure_logging("WARNING") == logging.WARNING
        assert _configure_logging(None) is None
        monkeypatch.setenv("TRACKINIZER_LOG_LEVEL", "ERROR")
        assert _configure_logging(None) == logging.ERROR

    def test_parse_args_defaults(self) -> None:
        parser = argparse.ArgumentParser()
        flags, remaining = _parse_args(parser, [])
        assert flags.engine == "pglite"
        assert flags.host == "127.0.0.1"
        assert flags.port == 8765
        assert flags.embedder == "stub"
        assert remaining == []

    def test_session_ttl_env_typo_exits_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bad env TTL must exit like every other config error, not traceback.

        ``config.py`` already rejects this with a clean ``ConfigError``; the CLI
        kept its own bare ``int()`` and so crashed with a raw ``ValueError``
        before argparse could even render ``--help``.
        """
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "abc")

        with pytest.raises(SystemExit):
            _ = _parse_args(argparse.ArgumentParser(), [])

    def test_session_ttl_rejects_non_positive(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero expires every session instantly; the env path already says so."""
        monkeypatch.delenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", raising=False)

        with pytest.raises(SystemExit):
            _ = _parse_args(
                argparse.ArgumentParser(),
                ["--session-max-age-seconds", "0"],
            )

    def test_session_ttl_defaults_to_the_shared_constant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", raising=False)

        flags, _ = _parse_args(argparse.ArgumentParser(), [])

        assert flags.session_max_age_seconds == 30 * 24 * 60 * 60

    def test_session_ttl_reads_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "600")

        flags, _ = _parse_args(argparse.ArgumentParser(), [])

        assert flags.session_max_age_seconds == 600

    @pytest.mark.parametrize(
        ("env", "argv", "auth_disabled"),
        [
            ("", [], False),
            ("", ["--no-auth"], True),
            ("1", [], True),
            ("1", ["--auth"], False),
        ],
    )
    def test_trackinizer_no_auth_is_the_default_a_flag_overrides(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env: str,
        argv: list[str],
        auth_disabled: bool,
    ) -> None:
        # The docs offer TRACKINIZER_NO_AUTH=1 as the alternative to --no-auth,
        # read as Config.from_env reads it.
        monkeypatch.setenv("TRACKINIZER_NO_AUTH", env)

        flags, remaining = _parse_args(argparse.ArgumentParser(), argv)

        assert remaining == []
        assert Config.from_args(flags).auth_disabled is auth_disabled

    def test_parse_args_overrides(self) -> None:
        parser = argparse.ArgumentParser()
        flags, _ = _parse_args(
            parser,
            ["--engine", "pg", "--dsn", "x", "--port", "9000"],
        )
        assert flags.engine == "pg"
        assert flags.dsn == "x"
        assert flags.port == 9000

    def test_app_dir_flag_reaches_web_attach(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``--app-dir`` must reach ``web.attach`` through ``_configure_app``; a
        # directory that does not exist yet is accepted, since a build may land
        # after startup.
        flags, remaining = _parse_args(
            argparse.ArgumentParser(),
            ["--app-dir", str(tmp_path / "not-built-yet")],
        )
        assert remaining == []
        fresh = FastAPI()
        monkeypatch.setattr(server, "app", fresh)
        server._configure_app(flags)
        assert "/app/{path:path}" in registered_paths(fresh)

    def test_the_packaged_build_is_served_unless_app_dir_names_another(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The wheel carries the built app, so `trackinizer` serves it with no
        # flag; production's --app-dir symlink to its own builds still wins.
        _ = _write_build(tmp_path / "web" / "dist", marker="packaged")
        monkeypatch.setattr(server, "_CWD", tmp_path / "server")
        flag = _write_build(tmp_path / "flag", marker="flag")

        assert _served_index(monkeypatch, []) == "packaged"
        assert _served_index(monkeypatch, ["--app-dir", str(flag)]) == "flag"

    def test_without_a_packaged_build_nothing_is_served_at_app(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(server, "_CWD", tmp_path / "server")
        flags, _ = _parse_args(argparse.ArgumentParser(), [])
        fresh = FastAPI()
        monkeypatch.setattr(server, "app", fresh)

        server._configure_app(flags)

        assert not any(path.startswith("/app") for path in registered_paths(fresh))

    def test_the_packaged_build_is_the_built_app_beside_the_package(
        self,
        tmp_path: Path,
    ) -> None:
        # A checkout has one only after `npm run build` in web/; an empty dist/
        # is no build.
        assert server._packaged_app(tmp_path) is None
        (tmp_path / "web" / "dist").mkdir(parents=True)
        assert server._packaged_app(tmp_path) is None
        built = _write_build(tmp_path / "web" / "dist", marker="built")
        assert server._packaged_app(tmp_path) == built

    def test_addon_override_reaches_the_deployments_visuals(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flags, _ = _parse_args(
            argparse.ArgumentParser(),
            argv=["--addon-override", "visuals.default_visual=trax.chat"],
        )
        fresh = FastAPI()
        monkeypatch.setattr(server, "app", fresh)
        server._configure_app(flags)
        catalog: object = getattr(fresh.state, "visual_catalog", None)
        assert isinstance(catalog, Workspace)
        assert catalog.default_visual == "trax.chat"

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["--addons", "no.such.factory"], "Cannot import module"),
            (["--addon-override", "nope=1"], "no field"),
        ],
    )
    def test_a_bad_deployment_exits_with_a_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        message: str,
    ) -> None:
        flags, _ = _parse_args(argparse.ArgumentParser(), argv=argv)
        monkeypatch.setattr(server, "app", FastAPI())
        with pytest.raises(SystemExit, match=message):
            server._configure_app(flags)


class TestCLIHelpers:
    def test_main_invokes_uvicorn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            called["app"] = app
            called.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr("sys.argv", ["trackinizer", "--port", "1234"])
        main()
        assert called["host"] == "127.0.0.1"
        assert called["port"] == 1234
        assert called["timeout_keep_alive"] == 240
        # Force-close on shutdown (don't wait for connections to drain); the
        # zero-task cancel ERROR this causes is suppressed by the log filter.
        assert called["timeout_graceful_shutdown"] == 0


class TestLogLevelReachesUvicorn:
    """``--log-level`` must reach uvicorn, not just this package's logger.

    ``uvicorn.access`` emits one INFO line per request and uvicorn defaults it
    to INFO independently of anything the app configures. The flag therefore
    looked honored while every request kept logging -- 2.4GB of access lines in
    five days, filling the log partition on the deployment that hit it.
    """

    def test_flag_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            del app
            called.update(kwargs)

        monkeypatch.delenv("TRACKINIZER_LOG_LEVEL", raising=False)
        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr("sys.argv", ["trackinizer", "--log-level", "WARNING"])
        main()
        assert called["log_level"] == logging.WARNING

    def test_env_fallback_is_forwarded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            del app
            called.update(kwargs)

        monkeypatch.setenv("TRACKINIZER_LOG_LEVEL", "ERROR")
        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr("sys.argv", ["trackinizer"])
        main()
        assert called["log_level"] == logging.ERROR

    def test_unset_leaves_uvicorn_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No flag, no env: uvicorn keeps its own INFO default, unchanged."""
        called: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            del app
            called.update(kwargs)

        monkeypatch.delenv("TRACKINIZER_LOG_LEVEL", raising=False)
        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr("sys.argv", ["trackinizer"])
        main()
        assert called["log_level"] is None

    def test_numeric_level_silences_access_logger(self) -> None:
        """The forwarded value must be one uvicorn accepts and act on.

        uvicorn indexes ``LOG_LEVELS`` by lowercase NAME for a str, but takes an
        int as-is, then calls ``setLevel`` on ``uvicorn.access``. Passing the
        numeric level keeps the two spellings from drifting.
        """
        config = uvicorn.Config(app, log_level=logging.WARNING)
        config.configure_logging()
        access = logging.getLogger("uvicorn.access")
        assert access.level == logging.WARNING
        assert access.isEnabledFor(logging.INFO) is False


class TestMainAppliesEveryStartupInvariant:
    """One process serves everything, so ``main`` owns every startup step.

    There is no worker fan-out: the inbound queue and the subscriber sweep are
    in-process state, so uvicorn is handed the configured app object rather
    than an import string it would re-import in forked children. Anything not
    applied here is applied nowhere.
    """

    def test_configures_the_app_before_serving(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Uvicorn gets the app object, already carrying its config."""
        served: dict[str, object] = {}

        def fake_run(target: object, **kwargs: object) -> None:
            del kwargs
            served["target"] = target

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            ["trackinizer", "--engine", "pg", "--dsn", "postgres://probe/db"],
        )

        main()

        assert served["target"] is app
        config = app.state.config  # pyright: ignore[reportAny] -- Starlette's `State` exposes every attribute as Any.
        assert isinstance(config, Config)
        assert config.engine == "pg"
        assert config.dsn == "postgres://probe/db"

    def test_rejects_unrecognized_arguments(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["trackinizer", "--bogus-flag"])

        with pytest.raises(SystemExit):
            main()

    def test_rejects_a_worker_count_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A stale unit drop-in passing ``--workers`` must fail loudly.

        Fan-out was removed rather than validated, and systemd's base unit can
        only be rewritten by root -- so an old command line outliving a deploy
        is the observed failure mode, not a hypothetical. Exiting on the
        unknown flag names it; silently ignoring it would run a mode the
        operator's config claims and the server does not have.
        """
        monkeypatch.setattr(
            "sys.argv",
            ["trackinizer", "--engine", "pglite", "--workers", "4"],
        )

        with pytest.raises(SystemExit):
            main()

        assert "--workers" in capsys.readouterr().err

    def test_applies_the_requested_log_level(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--log-level`` must reach this package's logger, not just uvicorn's."""
        monkeypatch.setattr(uvicorn, "run", _ignore_run)
        monkeypatch.delenv("TRACKINIZER_LOG_LEVEL", raising=False)
        monkeypatch.setattr(logger, "level", logging.NOTSET)
        monkeypatch.setattr(
            "sys.argv",
            ["trackinizer", "--engine", "pg", "--dsn", "x", "--log-level", "DEBUG"],
        )

        main()

        assert logger.level == logging.DEBUG

    def test_installs_the_shutdown_noise_filter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shutdown emits the ERROR the filter suppresses.

        Measured on the deployed service before this fix: 24 spurious
        ``Cancel 0 running task(s)`` errors in 30 minutes.
        """
        error_logger = logging.getLogger("uvicorn.error")
        monkeypatch.setattr(uvicorn, "run", _ignore_run)
        monkeypatch.setattr(
            error_logger,
            "filters",
            [f for f in error_logger.filters if not _is_noise_filter(f)],
        )
        monkeypatch.setattr("sys.argv", ["trackinizer", "--engine", "pg", "--dsn", "x"])

        main()

        assert any(_is_noise_filter(f) for f in error_logger.filters)

    def test_installs_the_filter_exactly_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-entry must not stack duplicates onto the process-wide logger."""
        error_logger = logging.getLogger("uvicorn.error")
        monkeypatch.setattr(uvicorn, "run", _ignore_run)
        monkeypatch.setattr(
            error_logger,
            "filters",
            [f for f in error_logger.filters if not _is_noise_filter(f)],
        )
        monkeypatch.setattr("sys.argv", ["trackinizer", "--engine", "pg", "--dsn", "x"])

        main()
        main()

        assert sum(_is_noise_filter(f) for f in error_logger.filters) == 1


def _ignore_run(target: object, **kwargs: object) -> None:
    """Stand in for ``uvicorn.run`` when a test asserts only side effects."""
    del target, kwargs


def _write_build(root: Path, *, marker: str) -> Path:
    """Write a minimal built web app at ``root``: an entry page naming ``marker``."""
    root.mkdir(parents=True, exist_ok=True)
    _ = (root / "index.html").write_text(marker)
    return root


def _served_index(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str:
    """Configure a fresh app from ``argv``; return its ``/app/`` to a signed-in user."""
    flags, _ = _parse_args(argparse.ArgumentParser(), argv)
    fresh = FastAPI()
    monkeypatch.setattr(server, "app", fresh)
    server._configure_app(flags)
    fresh.dependency_overrides[web.optional_identity] = _signed_in
    return TestClient(fresh).get("/app/").text


async def _signed_in() -> AuthIdentity:
    return AuthIdentity(
        user_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        api_key_id=None,
        email="viewer@example.com",
        role="viewer",
    )


def _is_noise_filter(candidate: object) -> bool:
    return isinstance(candidate, _SuppressZeroTaskCancel)


class TestSuppressZeroTaskCancel:
    """The shutdown-noise filter drops only the spurious zero-task cancel."""

    @classmethod
    def _record(cls, message: str, *args: object) -> logging.LogRecord:
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
            "Cancel %s running task(s), timeout graceful shutdown exceeded",
            0,
        )
        assert filt.filter(rec) is False

    def test_keeps_real_cancel(self) -> None:
        filt = _SuppressZeroTaskCancel()
        rec = self._record(
            "Cancel %s running task(s), timeout graceful shutdown exceeded",
            3,
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
        source = inspect.getsource(Server.shutdown)
        assert (
            "Cancel %s running task(s), timeout graceful shutdown exceeded" in source
        ), (
            "uvicorn reworded its shutdown-cancel message; update "
            "_SuppressZeroTaskCancel to match"
        )


class TestMainRejectsBadInvocations:
    """``main`` must exit before uvicorn on any argv the guards reject."""

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


class TestCoverageRoutesAndCli:
    def test_main_web_branch_and_engine_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: dict[str, object] = {}

        def fake_run(passed_app: object, **kwargs: object) -> None:
            called.update(kwargs)
            called["web_attached"] = "/api/web/search" in registered_paths(app)
            del passed_app

        monkeypatch.delenv("TRACKINIZER_LOG_LEVEL", raising=False)
        monkeypatch.setattr("sys.argv", ["trackinizer", "--web", "--port", "9999"])
        monkeypatch.setattr(uvicorn, "run", fake_run)
        main()
        assert called == {
            "host": "127.0.0.1",
            "port": 9999,
            "log_level": None,
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
