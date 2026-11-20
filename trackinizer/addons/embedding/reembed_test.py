"""The re-embed: the server's own settings, the backlog in large calls, then done."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import field
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

import argparse
import logging
import sys

from configgle import Fig, Makes

import pytest
import pytest_asyncio

from trackinizer.addons.deployment import Deployment
from trackinizer.addons.embedding import reembed
from trackinizer.addons.embedding.addon import EmbeddingAddon
from trackinizer.addons.embedding.fakes import HashEmbedder, vector_store
from trackinizer.addons.embedding.search import Nearest
from trackinizer.addons.embedding.sweep import SweepState
from trackinizer.conftest import FakeEngine
from trackinizer.server.notify import NOTIFY_CHANNEL


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from trackinizer.lib.postgres import DatabaseEngine, PostgresEngine
    from trackinizer.server.store.core import Store


class _Hash(HashEmbedder):
    """The hash model, built from a config as a site names its model."""

    class Config(Fig["_Hash"]):
        dim: int = 8
        """Vector width."""

    def __init__(self, config: Config) -> None:
        super().__init__(dim=config.dim)


class _Site(Makes["Deployment"], Deployment.Config):
    """A site that runs the embedding add-on on the hash model."""

    embedding: EmbeddingAddon.Config = field(default_factory=EmbeddingAddon.Config)
    """The add-on."""


def site() -> _Site:
    """Return the site, its add-on on an 8-wide hash model, chunks of 20."""
    config = _Site()
    config.embedding.model = _Hash.Config()
    config.embedding.chunk_chars = 20
    config.embedding.overlap_chars = 5
    return config


_FACTORY: Final = f"{__name__}.site"


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> Store:
    """Return a store whose database holds the vector table, every table empty."""
    return await vector_store(integ_engine)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_reembed_fills_the_table_with_the_servers_key(
    store: Store,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every record of the backlog, its units in one large call, under the site's key."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'reembed test', 'open')",
            session_id,
        )
        for idx, text in enumerate(["first", "c" * 40, "third"]):
            await conn.execute(
                "INSERT INTO session_records (session_id, part, idx, kind, payload, "
                "text) VALUES ($1, 0, $2, 'UserMessage', '{}'::json, $3)",
                session_id,
                idx,
                text,
            )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', 3)",
            session_id,
        )
    arm = reembed.vector_arm(_FACTORY, overrides=["embedding.slice_units=2"])
    server = site().make().manifest("embedding").vector_search
    assert isinstance(arm.model, _Hash)

    with caplog.at_level(logging.INFO):
        written = await reembed.reembed(store.engine, arm, slice_units=64)

    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT idx, chunk, settings FROM embedding.vectors ORDER BY idx, chunk",
        )
        index = await conn.fetchval(
            "SELECT indexname FROM pg_indexes WHERE indexname = $1",
            f"vectors_hnsw_{arm.settings.key}",
        )
    assert written == 5
    assert [(row["idx"], row["chunk"]) for row in rows] == [
        (0, 0),
        (1, 0),
        (1, 1),
        (1, 2),
        (2, 0),
    ]
    assert {row["settings"] for row in rows} == {arm.settings.key}
    assert isinstance(server, Nearest)
    assert arm.settings == server.settings
    assert [len(call) for call in arm.model.calls] == [5]
    assert index == f"vectors_hnsw_{arm.settings.key}"
    assert "re-embedded 5 units" in caplog.text


@pytest.mark.asyncio
async def test_the_rounds_run_until_one_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    arm = reembed.vector_arm(_FACTORY, overrides=[])
    states = [SweepState(worked=True, written=3), SweepState(worked=False)]
    asked: list[tuple[SweepState, int]] = []

    async def round_(
        engine: object,
        *,
        model: object,
        settings: object,
        state: SweepState,
        slice_units: int,
    ) -> SweepState:
        assert (model, settings) == (arm.model, arm.settings)
        del engine
        asked.append((state, slice_units))
        return states[len(asked) - 1]

    monkeypatch.setattr(reembed, "sweep_once", round_)
    engine = FakeEngine()

    with caplog.at_level(logging.INFO):
        written = await reembed.reembed(
            cast("DatabaseEngine", engine),
            arm,
            slice_units=5,
        )

    assert written == 3
    assert asked == [(SweepState(), 5), (states[0], 5)]
    assert [str(call.args[0]) for call in engine.conn.execute.await_args_list] == [
        (
            f"CREATE INDEX IF NOT EXISTS vectors_hnsw_{arm.settings.key} "
            "ON embedding.vectors USING hnsw ((embedding::halfvec(8)) halfvec_cosine_ops) "
            f"WHERE settings = '{arm.settings.key}'"
        ),
    ]
    assert f"embedding settings {arm.settings.key}" in caplog.text
    assert "re-embedded 3 units, " in caplog.text


def test_a_site_without_the_addon_is_refused() -> None:
    with pytest.raises(SystemExit, match="runs no embedding add-on"):
        _ = reembed.vector_arm("", overrides=[])


def test_a_bad_factory_exits_with_its_error() -> None:
    with pytest.raises(SystemExit, match="not a dotted path"):
        _ = reembed.vector_arm("nodots", overrides=[])


def test_the_flags_take_the_servers_factory_and_overrides() -> None:
    parser = argparse.ArgumentParser()

    flags = reembed._parse_args(
        parser,
        [
            "postgresql://h/db",
            "--addons",
            _FACTORY,
            "--override",
            "embedding.model.device=cuda:0",
            "--override",
            "jobs.hosts=true",
        ],
    )

    assert (flags.dsn, flags.addons, flags.override, flags.slice_units) == (
        "postgresql://h/db",
        _FACTORY,
        ["embedding.model.device=cuda:0", "jobs.hosts=true"],
        65_536,
    )


def test_the_flags_default_to_the_base_deployment_and_large_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `argparse` wraps help at the terminal's width; one wide enough wraps none of it.
    monkeypatch.setenv("COLUMNS", "200")
    parser = argparse.ArgumentParser()

    flags = reembed._parse_args(parser, ["dsn", "--slice-units", "7"])

    assert (flags.addons, flags.override, flags.slice_units) == ("", [], 7)
    assert all(
        said in parser.format_help()
        for said in (
            "The server's Postgres DSN.",
            "--addons FACTORY",
            "The server's deployment factory, as its --addons names it.",
            "--override PATH=VALUE",
            "An override on the deployment config, as the server's. Repeatable.",
            "Sources per page and texts per model call (default: 65536).",
        )
    )


def test_main_re_embeds_the_dsn_with_the_servers_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, int, int]] = []

    async def run(dsn: str, arm: Nearest, *, slice_units: int) -> int:
        asked.append((dsn, arm.settings.dim, slice_units))
        return 0

    monkeypatch.setattr(reembed, "_reembed_at", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["reembed", "postgresql://h/db", "--addons", _FACTORY, "--slice-units", "9"],
    )
    # The operator reads its progress: the root logger, bare here, gets INFO lines.
    monkeypatch.setattr(logging.root, "handlers", [])
    monkeypatch.setattr(logging.root, "level", logging.WARNING)

    assert reembed.main() == 0
    assert asked == [("postgresql://h/db", 8, 9)]
    [handler] = logging.root.handlers
    assert logging.root.level == logging.INFO
    assert handler.formatter is not None
    assert handler.formatter._fmt == "%(asctime)s %(message)s"


def test_the_help_is_the_docstring_past_its_shell_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["reembed", "--help"])

    with pytest.raises(SystemExit):
        reembed.main()

    shown = capsys.readouterr().out
    assert "Re-embed every session record, inquiry text and edge note now" in shown
    assert "--override embedding.model.device=cuda:0" in shown
    assert "exec uv" not in shown


def test_the_help_without_docstrings_names_the_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``python -OO`` strips the docstring; the help still lists every flag."""
    monkeypatch.setattr(reembed, "__doc__", None)
    monkeypatch.setattr(sys, "argv", ["reembed", "--help"])

    with pytest.raises(SystemExit):
        reembed.main()

    assert "--slice-units" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_dsn_opens_the_engine_the_re_embed_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, str]] = []
    ran: list[tuple[object, int]] = []
    arm = reembed.vector_arm(_FACTORY, overrides=[])

    @asynccontextmanager
    async def engine(*, dsn: str, listen_channel: str) -> AsyncGenerator[str]:
        opened.append((dsn, listen_channel))
        yield "the engine"

    async def run(engine: object, ran_arm: Nearest, *, slice_units: int) -> int:
        assert ran_arm is arm
        ran.append((engine, slice_units))
        return 3

    monkeypatch.setattr(reembed, "PostgresEngine", engine)
    monkeypatch.setattr(reembed, "reembed", run)

    assert await reembed._reembed_at("postgresql://h/db", arm, slice_units=4) == 3
    assert opened == [("postgresql://h/db", NOTIFY_CHANNEL)]
    assert ran == [("the engine", 4)]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
