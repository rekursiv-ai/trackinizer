"""Tests for engine/embedder construction helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import argparse

import pytest

from trackinizer.lib.postgres import PGliteEngine, PostgresEngine
from trackinizer.server.config import (
    Assistant,
    Config,
    ConfigError,
    ConfigFlags,
    _prune_stale_ephemeral_dirs,
    auth_disabled_from_env,
    build_embedder,
    build_engine,
    parse_assistant,
    parse_engine,
)
from trackinizer.server.embedders.stub import StubEmbedder


if TYPE_CHECKING:
    from pathlib import Path


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
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """PGlite defaults to a Unix socket (no port race) unless TCP is opted in."""
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pglite")
        monkeypatch.setenv("TRACKINIZER_DATADIR", str(tmp_path))
        monkeypatch.delenv("TRACKINIZER_PGLITE_TCP", raising=False)
        engine = build_engine()
        assert isinstance(engine, PGliteEngine)
        assert engine._use_tcp is False

    def testbuild_engine_pglite_tcp_opt_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``TRACKINIZER_PGLITE_TCP=1`` opens PGlite on a TCP port instead."""
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pglite")
        monkeypatch.setenv("TRACKINIZER_DATADIR", str(tmp_path))
        monkeypatch.setenv("TRACKINIZER_PGLITE_TCP", "1")
        engine = build_engine()
        assert isinstance(engine, PGliteEngine)
        assert engine._use_tcp is True

    def test_ephemeral_gets_unique_workdir_per_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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
        assert (
            a._workdir.parent
            == tmp_path / "rekursiv-ai" / "trackinizer" / "pgdata-ephemeral"
        )
        assert a._persist is False

    def test_ephemeral_explicit_datadir_wins(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An explicit --datadir is honored even under --ephemeral."""
        _patch_data_dir(monkeypatch, tmp_path)
        chosen = tmp_path / "explicit"
        engine = build_engine(Config(ephemeral=True, datadir=chosen))
        assert isinstance(engine, PGliteEngine)
        assert engine._workdir == chosen

    def test_persistent_uses_shared_default_datadir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A persistent server keeps the single shared datadir (survives restarts)."""
        _patch_data_dir(monkeypatch, tmp_path)
        engine = build_engine(Config(ephemeral=False))
        assert isinstance(engine, PGliteEngine)
        assert engine._workdir == tmp_path / "rekursiv-ai" / "trackinizer" / "pgdata"
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "60")
        assert Config.from_env().session_max_age_seconds == 60

    def test_from_env_defaults_session_max_age(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", raising=False)
        assert Config.from_env().session_max_age_seconds == 30 * 24 * 60 * 60

    def test_from_args_reads_session_max_age(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", raising=False)
        config = Config.from_args(_server_args(session_max_age_seconds=120))
        assert config.session_max_age_seconds == 120

    def test_from_env_non_integer_raises_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "notanint")
        with pytest.raises(ConfigError):
            Config.from_env()

    def test_from_env_non_positive_raises_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "0")
        with pytest.raises(ConfigError):
            Config.from_env()


class TestSessionEmbedderDim:
    """``session_embedder_dim`` is an optional int from env and CLI."""

    def test_from_env_reads_the_dim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_EMBEDDER_DIM", "512")
        assert Config.from_env().session_embedder_dim == 512

    def test_from_env_absent_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_EMBEDDER_DIM", raising=False)
        assert Config.from_env().session_embedder_dim is None

    def test_from_env_non_integer_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_EMBEDDER_DIM", "big")
        with pytest.raises(ConfigError):
            Config.from_env()

    def test_from_args_reads_the_dim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_EMBEDDER_DIM", raising=False)
        config = Config.from_args(_server_args(session_embedder_dim=256))
        assert config.session_embedder_dim == 256


class TestFromEnv:
    """Every environment variable reaches its own field, and each has its default."""

    _VARIABLES = (
        "TRACKINIZER_ENGINE",
        "TRACKINIZER_DATADIR",
        "TRACKINIZER_EPHEMERAL",
        "TRACKINIZER_PGLITE_TCP",
        "TRACKINIZER_DSN",
        "TRACKINIZER_EMBEDDER",
        "TRACKINIZER_SESSION_EMBEDDER",
        "TRACKINIZER_SESSION_EMBEDDER_DIM",
        "TRACKINIZER_SESSION_EMBEDDERS",
        "TRACKINIZER_WEB",
        "TRACKINIZER_SESSION_SECRET",
        "TRACKINIZER_SESSION_MAX_AGE_SECONDS",
        "TRACKINIZER_NO_AUTH",
        "TRACKINIZER_ASSISTANT",
    )

    def test_unset_environment_is_the_default_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in self._VARIABLES:
            monkeypatch.delenv(name, raising=False)
        assert Config.from_env() == Config()

    def test_every_variable_reaches_its_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        values = {
            "TRACKINIZER_ENGINE": "pg",
            "TRACKINIZER_DATADIR": str(tmp_path),
            "TRACKINIZER_EPHEMERAL": "1",
            "TRACKINIZER_PGLITE_TCP": "1",
            "TRACKINIZER_DSN": "postgresql://h/db",
            "TRACKINIZER_EMBEDDER": "other",
            "TRACKINIZER_SESSION_EMBEDDER": "m@1024",
            "TRACKINIZER_SESSION_EMBEDDER_DIM": "7",
            "TRACKINIZER_SESSION_EMBEDDERS": "a, b ,,c",
            "TRACKINIZER_WEB": "1",
            "TRACKINIZER_SESSION_SECRET": "s3cret",
            "TRACKINIZER_SESSION_MAX_AGE_SECONDS": "99",
            "TRACKINIZER_NO_AUTH": "1",
            "TRACKINIZER_ASSISTANT": "kb=K@x.y",
        }
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        config = Config.from_env()
        assert config.session_secret == values["TRACKINIZER_SESSION_SECRET"]
        assert replace(config, session_secret=None) == Config(
            engine="pg",
            datadir=tmp_path,
            ephemeral=True,
            pglite_tcp=True,
            dsn="postgresql://h/db",
            embedder="other",
            session_embedder="m@1024",
            session_embedder_dim=7,
            session_embedders=("a", "b", "c"),
            web=True,
            session_max_age_seconds=99,
            auth_disabled=True,
            assistant=Assistant(actor="kb", email="k@x.y"),
        )


class TestParsers:
    def test_parse_engine_names_the_engine_or_refuses_it(self) -> None:
        assert parse_engine("pglite") == "pglite"
        assert parse_engine("pg") == "pg"
        with pytest.raises(ConfigError, match="unknown engine 'mysql'"):
            parse_engine("mysql")

    def test_blank_dim_is_none_and_padding_is_trimmed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_EMBEDDER_DIM", "   ")
        assert Config.from_env().session_embedder_dim is None
        monkeypatch.setenv("TRACKINIZER_SESSION_EMBEDDER_DIM", " 5 ")
        assert Config.from_env().session_embedder_dim == 5

    def test_a_bad_dim_says_which_variable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_EMBEDDER_DIM", "big")
        with pytest.raises(
            ConfigError,
            match="TRACKINIZER_SESSION_EMBEDDER_DIM must be an integer, got 'big'",
        ):
            Config.from_env()

    def test_no_auth_is_read_from_its_own_variable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_NO_AUTH", "1")
        assert auth_disabled_from_env() is True
        monkeypatch.setenv("TRACKINIZER_NO_AUTH", "0")
        assert auth_disabled_from_env() is False
        monkeypatch.delenv("TRACKINIZER_NO_AUTH")
        assert auth_disabled_from_env() is False

    def test_pruning_a_missing_root_is_a_no_op(self, tmp_path: Path) -> None:
        _prune_stale_ephemeral_dirs(tmp_path / "missing")


class TestAssistant:
    """``--assistant ACTOR=EMAIL`` names the one shared partner."""

    @pytest.mark.parametrize("raw", ["scout", "=a@b.c", "kb=", " = "])
    def test_from_args_rejects_a_malformed_assistant(self, raw: str) -> None:
        with pytest.raises(ConfigError, match="ACTOR=EMAIL"):
            Config.from_args(_server_args(assistant=raw))

    def test_blank_means_no_assistant(self) -> None:
        assert parse_assistant("") is None
        assert parse_assistant("   ") is None
        assert Config.from_args(_server_args(assistant="")).assistant is None

    def test_assistant_email_is_lowercased_like_stored_users(self) -> None:
        """users.email is stored lowercase, so the configured one must match it."""
        assert parse_assistant("scout=Ada@Example.COM") == Assistant(
            actor="scout",
            email="ada@example.com",
        )
        config = Config.from_args(_server_args(assistant="kb=Kb@X.Y"))
        assert config.assistant == Assistant(actor="kb", email="kb@x.y")

    def test_the_first_equals_splits_and_whitespace_is_trimmed(self) -> None:
        assert parse_assistant(" kb = a=b@x.y ") == Assistant(
            actor="kb",
            email="a=b@x.y",
        )

    def test_environment_supplies_the_assistant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_ASSISTANT", "a=x@y.z")
        assert Config.from_env().assistant == Assistant(actor="a", email="x@y.z")

    def test_unset_environment_means_no_assistant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_ASSISTANT", raising=False)
        assert Config.from_env().assistant is None

    def test_a_malformed_environment_value_is_a_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_ASSISTANT", "scout")
        with pytest.raises(ConfigError, match="ACTOR=EMAIL"):
            Config.from_env()


def _patch_data_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Redirect ``config.data_dir`` at ``root`` so workdir tests stay in tmp."""

    def _fake_data_dir() -> Path:
        return root

    monkeypatch.setattr("trackinizer.server.config.data_dir", _fake_data_dir)


def _server_args(**overrides: object) -> ConfigFlags:
    """Build a ``server._parse_args``-shaped Namespace with sane defaults."""
    base: dict[str, object] = {
        "engine": "pglite",
        "datadir": None,
        "ephemeral": False,
        "pglite_tcp": False,
        "dsn": "",
        "embedder": "stub",
        "session_embedder": "",
        "session_embedder_dim": None,
        "session_embedders": "",
        "web": False,
        "auth": True,
        "session_max_age_seconds": 30 * 24 * 60 * 60,
        "assistant": "",
    }
    base.update(overrides)
    return cast(ConfigFlags, argparse.Namespace(**base))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
