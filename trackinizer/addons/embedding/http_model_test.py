"""A model file that only calls an embedding server embeds and searches like any."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypedDict
from uuid import uuid4

from configgle import Fig
from treekle import json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.addons.embedding.addon import EmbeddingAddon
from trackinizer.addons.embedding.fakes import HashEmbedder, vector_store
from trackinizer.addons.embedding.search import Nearest
from trackinizer.addons.embedding.sweep import Sweep, SweepState, sweep_once


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import PostgresEngine
    from trackinizer.server.store.core import Store


class _Asked(TypedDict):
    """What the model posts: the texts, and whether they are queries."""

    texts: list[str]
    query: bool


class _Answered(TypedDict):
    """What the server answers: one vector a text."""

    vectors: list[list[float]]


_ASKED: Final[list[_Asked]] = []


async def _answer(request: httpx2.Request) -> httpx2.Response:
    """Stand in for an embedding server: hash vectors for the texts posted."""
    asked = json_loads(request.content.decode(), _Asked)
    _ASKED.append(asked)
    vectors = await HashEmbedder(dim=16).embed_batch(asked["texts"])
    return httpx2.Response(200, json={"vectors": vectors})


class _HttpModel:
    """A model that computes nothing: it posts its texts to an embedding server."""

    class Config(Fig["_HttpModel"]):
        url: str = "http://embedder.test/embed"
        """The server's embedding endpoint."""

    name = "http-hash@16"
    dim = 16

    def __init__(self, config: Config) -> None:
        self._url = config.url
        self._client = httpx2.AsyncClient(transport=httpx2.MockTransport(_answer))

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Post the documents; return the server's vectors."""
        return await self._post(list(texts), query=False)

    async def embed_query(self, text: str) -> list[float]:
        """Post the query; return the server's vector."""
        [vector] = await self._post([text], query=True)
        return vector

    async def _post(self, texts: list[str], *, query: bool) -> list[list[float]]:
        response = await self._client.post(
            self._url,
            json={"texts": texts, "query": query},
        )
        return json_loads(response.text, _Answered)["vectors"]


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> Store:
    """Return a store whose database holds the vector table, every table empty."""
    return await vector_store(integ_engine)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_model_that_only_calls_a_server_embeds_and_searches(
    store: Store,
) -> None:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'http test', 'open')",
            session_id,
        )
        for idx, text in enumerate(["asked by wire", "answered by wire"]):
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
    config.model = _HttpModel.Config()
    manifest = config.make().manifest()
    [sweep] = manifest.server_services
    arm = manifest.vector_search
    assert isinstance(sweep, Sweep)
    assert isinstance(arm, Nearest)
    _ASKED.clear()

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
        query="asked by wire",
        kept=_everything,
        filtered=False,
        limit=1,
    )

    assert hits is not None
    assert [(hit.owner_id, hit.idx) for hit in hits] == [(session_id, 0)]
    assert _ASKED == [
        {"texts": ["answered by wire", "asked by wire"], "query": False},
        {"texts": ["asked by wire"], "query": True},
    ]


def _everything(params: list[object]) -> str:
    """Keep every unit."""
    del params
    return "TRUE"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
