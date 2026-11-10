"""Tests for engine/embedder construction helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import argparse
import os
import re
import time

import pytest

from trackinizer.lib.postgres import PGliteEngine, PostgresEngine
from trackinizer.server.config import (
    Assistant,
    ComputeNote,
    Config,
    ConfigError,
    ConfigFlags,
    ServiceKeys,
    _prune_stale_ephemeral_dirs,
    auth_disabled_from_env,
    build_embedder,
    build_engine,
    parse_assistant,
    parse_chat_orgs,
    parse_chat_streaming,
    parse_compute,
    parse_engine,
    parse_service_keys,
    parse_staff,
    parse_terminal_stun,
    session_secret_from_file,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.wire.wire_terminals import MAX_ICE_SERVERS


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
        assert a._own_workdir is True
        assert b._own_workdir is True

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
        assert engine._own_workdir is False

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
        assert engine._own_workdir is False

    def testbuild_engine_pg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pg")
        monkeypatch.setenv("TRACKINIZER_DSN", "postgresql:///x")
        engine = build_engine()
        assert isinstance(engine, PostgresEngine)
        assert engine._dsn == "postgresql:///x"
        assert engine.listen_channel == NOTIFY_CHANNEL

    def testbuild_engine_pg_missing_dsn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "pg")
        monkeypatch.setenv("TRACKINIZER_DSN", "")
        with pytest.raises(
            ConfigError,
            match=r"^--engine pg requires --dsn \(or \$TRACKINIZER_DSN\)\.$",
        ):
            build_engine()

    def testbuild_engine_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_ENGINE", "bogus")
        with pytest.raises(ConfigError):
            build_engine()

    def test_parse_engine_unknown_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="bogus"):
            parse_engine("bogus")

    @pytest.mark.parametrize("name", ["pglite", "pg"])
    def test_parse_engine_accepts_each_engine(self, name: str) -> None:
        assert parse_engine(name) == name


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
        "TRACKINIZER_CHAT_STREAMING",
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
            "TRACKINIZER_ASSISTANT": "kb=KB@Service.Invalid",
            "TRACKINIZER_CHAT_STREAMING": "off",
        }
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        config = Config.from_env()
        assert config.session_secret == values["TRACKINIZER_SESSION_SECRET"]
        assert replace(config, session_secret="") == Config(
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
            assistant=Assistant(actor="kb", email="kb@service.invalid"),
            chat_streaming=False,
        )

    def test_every_flag_reaches_its_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        for name in self._VARIABLES:
            monkeypatch.delenv(name, raising=False)
        assert Config.from_args(_server_args()).session_secret == ""
        monkeypatch.setenv("TRACKINIZER_SESSION_SECRET", "s3cret")
        flags = _server_args(
            engine="pg",
            datadir=tmp_path,
            ephemeral=True,
            pglite_tcp=True,
            dsn="postgresql://h/db",
            embedder="other",
            session_embedder="m@1024",
            session_embedder_dim=7,
            session_embedders="a, b ,,c",
            web=True,
            session_max_age_seconds=99,
            auth=False,
            assistant="kb=KB@Service.Invalid",
            staff="a@x.y",
        )
        config = Config.from_args(flags)
        assert config.session_secret == os.environ["TRACKINIZER_SESSION_SECRET"]
        assert replace(config, session_secret="") == Config(
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
            assistant=Assistant(actor="kb", email="kb@service.invalid"),
            staff=("a@x.y",),
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

    def test_pruning_reclaims_only_old_dirs_of_dead_owners(
        self,
        tmp_path: Path,
    ) -> None:
        # Past any pid_max, so no process owns it; kill(2) answers ESRCH.
        dead = 2**30
        old = time.time() - 2 * 60 * 60
        names = {
            "dead_old": f"{dead}-a",
            "dead_fresh": f"{dead}-b",
            "alive_old": f"{os.getpid()}-c",
            "no_pid_old": "scratch-d",
        }
        for key, name in names.items():
            (tmp_path / name).mkdir()
            if key != "dead_fresh":
                os.utime(tmp_path / name, (old, old))
        (tmp_path / "loose-file").write_text("")

        _prune_stale_ephemeral_dirs(tmp_path)

        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
            [
                names["dead_fresh"],
                names["alive_old"],
                names["no_pid_old"],
                "loose-file",
            ],
        )

    def test_a_dir_owned_by_another_users_process_is_kept(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(pid: int, signal: int) -> None:
            del pid, signal
            raise PermissionError

        monkeypatch.setattr(os, "kill", refuse)
        owned = tmp_path / "4242-e"
        owned.mkdir()
        old = time.time() - 2 * 60 * 60
        os.utime(owned, (old, old))

        _prune_stale_ephemeral_dirs(tmp_path)

        assert owned.is_dir()


class TestMaintainedEmbedders:
    def test_an_explicit_list_wins_over_the_serving_embedder(self) -> None:
        config = Config(session_embedder="a@1024", session_embedders=("b", "c"))
        assert config.maintained_embedders() == ("b", "c")

    def test_the_serving_embedder_alone_is_maintained_by_default(self) -> None:
        assert Config(session_embedder="a@1024").maintained_embedders() == ("a@1024",)

    def test_no_serving_embedder_maintains_nothing(self) -> None:
        assert Config().maintained_embedders() == ()


class TestChatOrgs:
    """``$TRACKINIZER_CHAT_ORGS`` says who else continues a science chat."""

    def test_a_server_is_individual_unless_it_says_otherwise(
        self,
    ) -> None:
        assert parse_chat_orgs("") == "individual"
        assert parse_chat_orgs(" single ") == "single"
        assert parse_chat_orgs("domain") == "domain"
        assert parse_chat_orgs("individual ") == "individual"
        assert Config().chat_orgs == "individual"

    def test_a_server_without_the_variable_set_reads_individual(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mode that reads least: a forgotten setting shares nothing."""
        monkeypatch.delenv("TRACKINIZER_CHAT_ORGS", raising=False)
        assert Config.from_env().chat_orgs == "individual"
        assert Config.from_args(_server_args(assistant="")).chat_orgs == "individual"

    def test_anything_else_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="single, domain or individual"):
            parse_chat_orgs("company")

    def test_both_ways_to_build_a_config_read_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_CHAT_ORGS", "single")
        assert Config.from_env().chat_orgs == "single"
        assert Config.from_args(_server_args(assistant="")).chat_orgs == "single"


class TestChatStreaming:
    """``$TRACKINIZER_CHAT_STREAMING`` holds a chat's answer in writing; on unless off."""

    @pytest.mark.parametrize(("raw", "streaming"), [("", True), (" on ", True)])
    def test_a_server_streams_unless_it_says_off(
        self,
        raw: str,
        streaming: bool,
    ) -> None:
        assert parse_chat_streaming(raw) is streaming
        assert parse_chat_streaming("off") is False
        assert Config().chat_streaming is True

    @pytest.mark.parametrize("raw", ["no", "1", "On", "false"])
    def test_anything_else_is_a_config_error(self, raw: str) -> None:
        with pytest.raises(ConfigError, match="CHAT_STREAMING must be on or off"):
            parse_chat_streaming(raw)

    def test_both_ways_to_build_a_config_read_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_CHAT_STREAMING", "off")
        assert Config.from_env().chat_streaming is False
        assert Config.from_args(_server_args()).chat_streaming is False
        monkeypatch.delenv("TRACKINIZER_CHAT_STREAMING")
        assert Config.from_args(_server_args()).chat_streaming is True


class TestAssistant:
    """``--assistant ACTOR=EMAIL`` names the one shared partner."""

    @pytest.mark.parametrize("raw", ["scout", "=a@b.c", "kb=", " = "])
    def test_from_args_rejects_a_malformed_assistant(self, raw: str) -> None:
        with pytest.raises(ConfigError, match="ACTOR=EMAIL"):
            Config.from_args(_server_args(assistant=raw))

    @pytest.mark.parametrize(
        "raw",
        ["scout=ada@example.com", "kb=kb@service.invalid.example.com"],
    )
    def test_a_person_is_never_the_assistant(
        self,
        raw: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every key of the account is the assistant's, so it must be a program's."""
        with pytest.raises(
            ConfigError,
            match=(
                r"^--assistant \(TRACKINIZER_ASSISTANT\) must name a service account, "
                r"NAME@service\.invalid, got "
            ),
        ):
            Config.from_args(_server_args(assistant=raw))
        monkeypatch.setenv("TRACKINIZER_ASSISTANT", raw)
        with pytest.raises(ConfigError, match="TRACKINIZER_ASSISTANT"):
            Config.from_env()

    def test_blank_means_no_assistant(self) -> None:
        assert parse_assistant("") is None
        assert parse_assistant("   ") is None
        assert Config.from_args(_server_args(assistant="")).assistant is None

    def test_assistant_email_is_lowercased_like_stored_users(self) -> None:
        """users.email is stored lowercase, so the configured one must match it."""
        assert parse_assistant("scout=Scout@Service.INVALID") == Assistant(
            actor="scout",
            email="scout@service.invalid",
        )
        config = Config.from_args(_server_args(assistant="kb=Kb@Service.Invalid"))
        assert config.assistant == Assistant(actor="kb", email="kb@service.invalid")

    def test_the_first_equals_splits_and_whitespace_is_trimmed(self) -> None:
        assert parse_assistant(" kb = a=b@service.invalid ") == Assistant(
            actor="kb",
            email="a=b@service.invalid",
        )

    def test_environment_supplies_the_assistant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_ASSISTANT", "a=x@service.invalid")
        assert Config.from_env().assistant == Assistant(
            actor="a",
            email="x@service.invalid",
        )

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


class TestStaff:
    """``--staff PATTERNS`` names who holds the ``staff`` audience; unset names nobody."""

    def test_nothing_set_means_nobody_is_staff(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_STAFF", raising=False)
        assert Config().staff == ()
        assert Config.from_env().staff == ()
        assert Config.from_args(_server_args()).staff == ()

    def test_blank_means_nobody(self) -> None:
        assert parse_staff("") == ()
        assert parse_staff("  ,  ") == ()

    def test_patterns_are_split_trimmed_and_lowercased(self) -> None:
        assert parse_staff(" Ada@Example.com, *@Corp.EXAMPLE ") == (
            "ada@example.com",
            "*@corp.example",
        )

    def test_a_repeated_pattern_is_kept_once(self) -> None:
        assert parse_staff("a@x.y,A@X.Y") == ("a@x.y",)

    @pytest.mark.parametrize(
        "raw",
        ["staff", "*", "@x.y", "a@", "*@", "a*@x.y", "*@*.y", "a@b@c.d", "a b@x.y"],
    )
    def test_a_pattern_that_is_not_an_email_or_a_domain_is_a_config_error(
        self,
        raw: str,
    ) -> None:
        with pytest.raises(ConfigError, match="--staff"):
            parse_staff(raw)

    def test_the_flag_and_the_environment_supply_the_patterns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_STAFF", "*@x.y")
        assert Config.from_env().staff == ("*@x.y",)
        assert Config.from_args(_server_args(staff="a@x.y")).staff == ("a@x.y",)

    def test_a_malformed_environment_value_is_a_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_STAFF", "*")
        with pytest.raises(ConfigError, match="--staff"):
            Config.from_env()


class TestSecrets:
    """``secrets`` names the secret backend and comes from the environment only."""

    def test_the_default_is_the_file_backend(self) -> None:
        assert Config().secrets == "file"

    def test_from_env_reads_the_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_SECRETS", "none")
        assert Config.from_env().secrets == "none"

    def test_from_env_absent_is_the_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SECRETS", raising=False)
        assert Config.from_env().secrets == "file"

    def test_from_args_reads_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SECRETS", "file:/srv/secrets")
        assert Config.from_args(_server_args()).secrets == "file:/srv/secrets"


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
        "staff": "",
    }
    base.update(overrides)
    return cast(ConfigFlags, argparse.Namespace(**base))


class TestTerminalStun:
    """``$TRACKINIZER_TERMINAL_STUN`` names the STUN servers a direct link may use."""

    def test_the_default_is_no_server_and_a_list_is_comma_separated(self) -> None:
        assert parse_terminal_stun("") == ()
        assert parse_terminal_stun(" stun:a.test:3478 , stun:b.test ") == (
            "stun:a.test:3478",
            "stun:b.test",
        )
        assert Config().terminal_stun == ()

    def test_the_maximum_number_of_stun_servers_is_allowed(self) -> None:
        servers = tuple(f"stun:host{index}" for index in range(MAX_ICE_SERVERS))
        assert parse_terminal_stun(", ".join(servers)) == servers

    @pytest.mark.parametrize(
        "value",
        [
            "turn:relay.test",
            "a.test:3478",
            "stun:a b",
            "stun:a,stun:b,stun:c,stun:d,stun:e",
        ],
    )
    def test_anything_but_a_few_stun_urls_is_a_config_error(self, value: str) -> None:
        expected = (
            f"TRACKINIZER_TERMINAL_STUN takes at most {MAX_ICE_SERVERS} "
            f"comma separated stun:HOST[:PORT] URLs, got {value!r}"
        )
        with pytest.raises(ConfigError, match=f"^{re.escape(expected)}$"):
            parse_terminal_stun(value)

    def test_both_ways_to_build_a_config_read_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_TERMINAL_STUN", "stun:a.test:3478")
        assert Config.from_env().terminal_stun == ("stun:a.test:3478",)
        assert Config.from_args(_server_args(assistant="")).terminal_stun == (
            "stun:a.test:3478",
        )


class TestCompute:
    """``$TRACKINIZER_COMPUTE``: what this server provides, for the Hosts menu."""

    def test_unset_or_an_empty_list_means_no_notes(self) -> None:
        assert parse_compute("") == ()
        assert parse_compute("  ") == ()
        assert parse_compute("[]") == ()
        assert Config().compute == ()

    def test_a_list_of_names_and_abouts_reads_in_order_and_trimmed(self) -> None:
        raw = (
            '[{"name": " GPU jobs ", "about": "Campaigns run on a shared GPU."},'
            ' {"name": "Shell", "about": "Each chat\'s \\"own\\" box."}]'
        )

        assert parse_compute(raw) == (
            ComputeNote(name="GPU jobs", about="Campaigns run on a shared GPU."),
            ComputeNote(name="Shell", about='Each chat\'s "own" box.'),
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "GPU",
            "{}",
            '{"name": "a", "about": "b"}',
            '["a"]',
            '[{"name": "a"}]',
            '[{"name": "a", "about": "b", "url": "c"}]',
            '[{"name": 1, "about": "b"}]',
            '[{"name": " ", "about": "b"}]',
            '[{"name": "a", "about": ""}]',
        ],
    )
    def test_anything_else_is_a_config_error(self, raw: str) -> None:
        with pytest.raises(
            ConfigError,
            match=r'TRACKINIZER_COMPUTE must be a JSON list of \{"name", "about"\}',
        ):
            parse_compute(raw)

    def test_both_ways_to_build_a_config_read_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_COMPUTE", '[{"name": "a", "about": "b"}]')
        notes = (ComputeNote(name="a", about="b"),)
        assert Config.from_env().compute == notes
        assert Config.from_args(_server_args()).compute == notes
        monkeypatch.setenv("TRACKINIZER_COMPUTE", "not json")
        with pytest.raises(ConfigError, match="TRACKINIZER_COMPUTE"):
            Config.from_env()


class TestServiceKeys:
    """``$TRACKINIZER_SERVICE_KEYS`` names the service accounts the server keys."""

    def test_pairs_are_read_in_order_with_their_directory(self, tmp_path: Path) -> None:
        assert parse_service_keys(
            " chat:writer, ,gpu-broker:viewer ",
            f" {tmp_path} ",
        ) == ServiceKeys(
            accounts=(("chat", "writer"), ("gpu-broker", "viewer")),
            directory=tmp_path,
        )

    def test_none_named_needs_no_directory(self) -> None:
        assert parse_service_keys(" , ", "") is None
        assert Config().service_keys is None

    @pytest.mark.parametrize(
        ("accounts", "says"),
        [
            ("chat", "'chat' is not NAME:viewer or NAME:writer"),
            ("chat:admin", "'chat:admin' is not NAME:viewer or NAME:writer"),
            ("Chat:writer", "'Chat:writer' is not NAME:viewer or NAME:writer"),
            ("chat:writer,chat:viewer", "'chat' is named twice"),
        ],
    )
    def test_a_malformed_pair_is_a_config_error(self, accounts: str, says: str) -> None:
        expected = f"TRACKINIZER_SERVICE_KEYS: {says}"
        with pytest.raises(ConfigError, match=f"^{re.escape(expected)}$"):
            parse_service_keys(accounts, "/srv/keys")

    def test_accounts_without_a_directory_are_a_config_error(self) -> None:
        expected = (
            "TRACKINIZER_SERVICE_KEYS names accounts; set TRACKINIZER_SERVICE_KEY_DIR"
        )
        with pytest.raises(ConfigError, match=f"^{re.escape(expected)}$"):
            parse_service_keys("chat:writer", "  ")

    def test_both_ways_to_build_a_config_read_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SERVICE_KEYS", "chat:writer")
        monkeypatch.setenv("TRACKINIZER_SERVICE_KEY_DIR", str(tmp_path))
        keys = ServiceKeys(accounts=(("chat", "writer"),), directory=tmp_path)
        assert Config.from_env().service_keys == keys
        assert Config.from_args(_server_args()).service_keys == keys
        monkeypatch.delenv("TRACKINIZER_SERVICE_KEY_DIR")
        with pytest.raises(ConfigError, match=r"set TRACKINIZER_SERVICE_KEY_DIR$"):
            Config.from_env()
        with pytest.raises(ConfigError, match=r"set TRACKINIZER_SERVICE_KEY_DIR$"):
            Config.from_args(_server_args())


class TestSessionSecretFile:
    """``$TRACKINIZER_SESSION_SECRET_FILE``: a key the first start makes and later ones keep."""

    def test_the_first_start_makes_it_and_a_later_one_reads_it(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "session-secret"

        made = session_secret_from_file(str(path))

        assert re.fullmatch(r"[0-9a-f]{64}", made)
        assert path.stat().st_mode & 0o777 == 0o600
        assert list(tmp_path.iterdir()) == [path]
        assert session_secret_from_file(str(path)) == made

    def test_a_stage_a_crashed_start_left_does_not_stop_the_next(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "session-secret"
        left = tmp_path / f".session-secret.{os.getpid()}"
        left.write_text("partial\n")

        made = session_secret_from_file(str(path))

        assert re.fullmatch(r"[0-9a-f]{64}", made)
        assert sorted(tmp_path.iterdir()) == [left, path]

    def test_a_start_that_loses_the_race_keeps_the_winners(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "session-secret"
        staged: list[Path] = []

        def link(source: str, target: Path) -> None:
            # Another first start links its own secret in between.
            staged.append(Path(source))
            target.write_text("won\n")
            raise FileExistsError(target)

        monkeypatch.setattr(os, "link", link)

        assert session_secret_from_file(str(path)) == "won"
        assert [(s.parent, s.name.startswith(".session-secret.")) for s in staged] == [
            (tmp_path, True),
        ]
        assert list(tmp_path.iterdir()) == [path]

    def test_a_kept_one_is_read_as_written(self, tmp_path: Path) -> None:
        path = tmp_path / "session-secret"
        path.write_text(" kept \n")

        assert session_secret_from_file(f" {path} ") == "kept"

    def test_none_named_is_none(self) -> None:
        assert session_secret_from_file("  ") == ""

    def test_both_ways_to_build_a_config_read_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("TRACKINIZER_SESSION_SECRET", raising=False)
        monkeypatch.setenv("TRACKINIZER_SESSION_SECRET_FILE", str(tmp_path / "s"))

        made = Config.from_env().session_secret

        assert made == (tmp_path / "s").read_text().strip()
        assert Config.from_args(_server_args()).session_secret == made

    def test_the_secret_and_its_file_together_are_a_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_SESSION_SECRET", "typed")
        monkeypatch.setenv("TRACKINIZER_SESSION_SECRET_FILE", str(tmp_path / "s"))
        expected = "set TRACKINIZER_SESSION_SECRET or TRACKINIZER_SESSION_SECRET_FILE, not both"

        with pytest.raises(ConfigError, match=f"^{re.escape(expected)}$"):
            Config.from_env()
        with pytest.raises(ConfigError, match=f"^{re.escape(expected)}$"):
            Config.from_args(_server_args())
        assert not (tmp_path / "s").exists()


def test_a_host_holds_16_tool_connections_each_dialed_back_in_20_seconds() -> None:
    config = Config()

    assert (
        config.tool_connections_per_host,
        config.tool_dial_back_sec,
        config.tool_sweep_sec,
    ) == (16, 20.0, 60.0)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
