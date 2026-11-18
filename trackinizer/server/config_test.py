"""Tests for engine/embedder construction helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import argparse

import pytest

from trackinizer.lib.postgres import PGliteEngine, PostgresEngine
from trackinizer.server.config import (
    Assistant,
    ComputeNote,
    Config,
    ConfigError,
    ConfigFlags,
    _prune_stale_ephemeral_dirs,
    auth_disabled_from_env,
    build_embedder,
    build_engine,
    parse_assistant,
    parse_chat_orgs,
    parse_chat_streaming,
    parse_compute,
    parse_engine,
    parse_staff,
    parse_terminal_stun,
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
            "TRACKINIZER_ASSISTANT": "kb=K@x.y",
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
            assistant=Assistant(actor="kb", email="k@x.y"),
            chat_streaming=False,
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
        with pytest.raises(ConfigError, match="TRACKINIZER_TERMINAL_STUN"):
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
    """``$TRACKINIZER_COMPUTE``: what this server provides, for the Machines menu."""

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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
