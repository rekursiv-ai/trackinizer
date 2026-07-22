"""Tests for engine/embedder construction helpers."""

from __future__ import annotations

from pathlib import Path

import argparse

import pytest

from trackinizer.lib.postgres import PGliteEngine, PostgresEngine
from trackinizer.server.config import (
    Config,
    ConfigError,
    build_embedder,
    build_engine,
    parse_engine,
)
from trackinizer.server.store.core import StubEmbedder


class TestPureFunctions:
    def testbuild_embedder_stub(self) -> None:
        emb = build_embedder("stub")
        assert isinstance(emb, StubEmbedder)

    def testbuild_embedder_unknown(self) -> None:
        # A bad config value raises ConfigError (a plain Exception), not
        # SystemExit: these helpers run inside the app lifespan, where a
        # BaseException would bypass normal error handling (API-18).
        with pytest.raises(ConfigError):
            build_embedder("nope")

    def testbuild_engine_pglite(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pglite")
        monkeypatch.setenv("TRACKINIZER_DATADIR", str(tmp_path))
        engine = build_engine()
        assert isinstance(engine, PGliteEngine)

    def testbuild_engine_pglite_defaults_to_unix_socket(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PGlite defaults to a Unix socket (no port race) unless TCP is opted in."""
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pglite")
        monkeypatch.setenv("TRACKINIZER_DATADIR", str(tmp_path))
        monkeypatch.delenv("TRACKINIZER_PGLITE_TCP", raising=False)
        engine = build_engine()
        assert isinstance(engine, PGliteEngine)
        assert engine._use_tcp is False

    def testbuild_engine_pglite_tcp_opt_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``TRACKINIZER_PGLITE_TCP=1`` opens PGlite on a TCP port instead."""
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pglite")
        monkeypatch.setenv("TRACKINIZER_DATADIR", str(tmp_path))
        monkeypatch.setenv("TRACKINIZER_PGLITE_TCP", "1")
        engine = build_engine()
        assert isinstance(engine, PGliteEngine)
        assert engine._use_tcp is True

    def test_ephemeral_gets_unique_workdir_per_call(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two ephemeral servers (no --datadir) never share a workdir.

        Sharing one pgdata dir corrupts concurrent PGlite boots; a unique dir per
        process makes the collision impossible (the original demo failure).
        """
        _patch_data_dir(monkeypatch, tmp_path)
        a = build_engine(Config(ephemeral=True))
        b = build_engine(Config(ephemeral=True))
        assert isinstance(a, PGliteEngine)
        assert isinstance(b, PGliteEngine)
        assert a._workdir != b._workdir
        assert a._workdir.parent == tmp_path / "pgdata-ephemeral"
        assert a._persist is False

    def test_ephemeral_explicit_datadir_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit --datadir is honored even under --ephemeral."""
        _patch_data_dir(monkeypatch, tmp_path)
        chosen = tmp_path / "explicit"
        engine = build_engine(Config(ephemeral=True, datadir=chosen))
        assert isinstance(engine, PGliteEngine)
        assert engine._workdir == chosen

    def test_persistent_uses_shared_default_datadir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A persistent server keeps the single shared datadir (survives restarts)."""
        _patch_data_dir(monkeypatch, tmp_path)
        engine = build_engine(Config(ephemeral=False))
        assert isinstance(engine, PGliteEngine)
        assert engine._workdir == tmp_path / "pgdata"
        assert engine._persist is True

    def testbuild_engine_pg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pg")
        monkeypatch.setenv("TRACKINIZER_DSN", "postgresql:///x")
        engine = build_engine()
        assert isinstance(engine, PostgresEngine)

    def testbuild_engine_pg_missing_dsn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pg")
        monkeypatch.setenv("TRACKINIZER_DSN", "")
        with pytest.raises(ConfigError):
            build_engine()

    def testbuild_engine_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "bogus")
        with pytest.raises(ConfigError):
            build_engine()

    def test_parse_engine_unknown_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            parse_engine("bogus")


class TestSessionMaxAge:
    """``session_max_age_seconds`` is configurable from env and CLI."""

    def test_from_env_reads_session_max_age(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "60")
        assert Config.from_env().session_max_age_seconds == 60

    def test_from_env_defaults_session_max_age(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", raising=False)
        assert Config.from_env().session_max_age_seconds == 30 * 24 * 60 * 60

    def test_from_args_reads_session_max_age(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", raising=False)
        config = Config.from_args(_server_args(session_max_age_seconds=120))
        assert config.session_max_age_seconds == 120

    def test_from_env_non_integer_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "notanint")
        with pytest.raises(ConfigError):
            Config.from_env()

    def test_from_env_non_positive_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "0")
        with pytest.raises(ConfigError):
            Config.from_env()


def _patch_data_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Redirect ``config.data_dir`` at ``root`` so workdir tests stay in tmp."""

    def _fake_data_dir(_name: str) -> Path:
        return root

    monkeypatch.setattr("trackinizer.server.config.data_dir", _fake_data_dir)


def _server_args(**overrides: object) -> argparse.Namespace:
    """Build a ``server._parse_args``-shaped Namespace with sane defaults."""
    base: dict[str, object] = {
        "engine": "pglite",
        "datadir": None,
        "ephemeral": False,
        "pglite_tcp": False,
        "dsn": "",
        "embedder": "stub",
        "web": False,
        "no_auth": False,
        "session_max_age_seconds": 30 * 24 * 60 * 60,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
