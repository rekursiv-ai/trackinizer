"""A site's own model of another width needs only its model file and its config."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from configgle import Fig

import pytest
import pytest_asyncio

from trackinizer.addons.embedding.addon import EmbeddingAddon
from trackinizer.addons.embedding.fakes import HashEmbedder, vector_store
from trackinizer.addons.embedding.search import Nearest
from trackinizer.addons.embedding.sweep import Sweep, SweepState, sweep_once


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PostgresEngine
    from trackinizer.server.store.core import Store


class _Wide384(HashEmbedder):
    """A model of 384 dims, as a site would name its own."""

    class Config(Fig["_Wide384"]):
        pass

    def __init__(self, config: Config) -> None:
        del config
        super().__init__(dim=384)


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> Store:
    """Return a store whose database holds the vector table, every table empty."""
    return await vector_store(integ_engine)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_384_wide_model_is_embedded_and_searched(store: Store) -> None:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'wide test', 'open')",
            session_id,
        )
        for idx, text in enumerate(["the narrow road", "the wide sea"]):
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
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', 2)",
            session_id,
        )
    config = EmbeddingAddon.Config()
    config.model = _Wide384.Config()
    manifest = config.make().manifest()
    [sweep] = manifest.server_services
    arm = manifest.vector_search
    assert isinstance(sweep, Sweep)
    assert isinstance(arm, Nearest)

    state = SweepState()
    while (
        state := await sweep_once(
            store.engine,
            model=sweep.model,
            settings=sweep.settings,
            state=state,
            slice_units=8,
        )
    ).worked:
        pass
    sweep.loaded.set()
    hits = await arm.nearest(
        store.engine,
        query="the wide sea",
        kept=_everything,
        filtered=False,
        limit=1,
    )

    async with store.engine.acquire() as conn:
        widths = await conn.fetch(
            "SELECT DISTINCT vector_dims(embedding) AS dims FROM embedding.vectors",
        )
    assert [row["dims"] for row in widths] == [384]
    assert hits is not None
    assert [(hit.owner_id, hit.idx) for hit in hits] == [(session_id, 1)]


def _everything(params: list[object]) -> str:
    """Keep every unit."""
    del params
    return "TRUE"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
