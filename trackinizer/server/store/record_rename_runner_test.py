"""The record rename: each row's kind and tags move to the classes' new names."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import json
import uuid

from treekle import PlainTree, mutable

import pytest

from trackinizer.sagent.journal.types import renamed_records
from trackinizer.server.store.record_rename_runner import (
    rename_shard,
    renamed_text,
)
from trackinizer.types.session_records import legacy_tool_call, served_payload


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped."""
    return integ_store


def _tagged(name: str) -> dict[str, object]:
    return {"py/object": name, "sender": "a", "content": "hi"}


def _call(held: dict[str, object]) -> dict[str, object]:
    """Return a call whose arguments, plain data, hold ``held``."""
    return {"py/object": "ToolCall", "call_id": "c", "arguments": {"py/dict": held}}


def _rows(old: str, new: str) -> list[tuple[str, str, str]]:
    """Return rows that name ``old``: each kind, payload text, and that text renamed."""
    quoted = f'says "py/object": "{old}" in words'
    said = json.dumps({"py/object": "UserMessage", "content": quoted})
    longer = json.dumps({"py/object": f"{old}Longer", "call_id": "c"})
    held = json.dumps(_call({"x": _tagged(old)}))

    def window(name: str) -> str:
        plain = _call({"x": _tagged(old)})
        history = [_tagged(name), plain, _tagged(name), plain, _tagged(name)]
        return json.dumps({"py/object": "ContextClear", "history": history})

    return [
        # Its own row: the kind and the tag.
        (old, json.dumps(_tagged(old)), json.dumps(_tagged(new))),
        # Held by another record, under another kind, and spaced another way.
        (
            "ContextState",
            json.dumps({"py/object": "ContextState", "held": [_tagged(old)]}),
            json.dumps({"py/object": "ContextState", "held": [_tagged(new)]}),
        ),
        (
            "BashResult",
            '{"py/object" :  "' + old + '", "call_id": "c"}',
            '{"py/object" :  "' + new + '", "call_id": "c"}',
        ),
        # Said in a text, and part of a longer name: neither is a tag of this class.
        ("UserMessage", said, said),
        ("ToolCall", longer, longer),
        # A call's arguments are plain data, as the server serves them: a tag in them
        # names no record, alone or in a window beside a record that is renamed.
        ("ToolCall", held, held),
        ("ContextClear", window(old), window(new)),
    ]


def test_a_tag_in_plain_data_is_left_as_the_server_serves_it() -> None:
    for old, new in renamed_records().items():
        for kind, before, after in _rows(old, new):
            assert renamed_text(before) == after
            # The stored tree as it is: ``json_loads`` would read ``py/dict``.
            stored = cast(Mapping[str, PlainTree], json.loads(before))
            # A legacy call's row is served converted, as no rename converts it.
            if not legacy_tool_call(kind, stored):
                assert json.loads(after) == mutable(served_payload(kind, stored))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_each_shard_moves_each_kind_and_tag_of_its_range_and_no_other_byte(
    store: Store,
) -> None:
    # The first id of the key space, the first of the middle shard, and the last.
    sessions = [
        uuid.UUID(int=0),
        uuid.UUID(int=(1 << 128) // 3),
        uuid.UUID(int=~0 & ((1 << 128) - 1)),
    ]
    named = sorted(renamed_records().items())
    stored = [
        (kind, payload) for old, new in named for kind, payload, _ in _rows(old, new)
    ]
    async with store.engine.acquire() as conn:
        for session_id in sessions:
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, "
                "agentsession_audience) VALUES ($1, 'AgentSession', "
                "nextval('seq_agentsession'), 'active', 'tester@example.com', "
                "'renamed', 'open')",
                session_id,
            )
            for idx, (kind, payload) in enumerate(stored):
                await conn.execute(
                    "INSERT INTO session_records "
                    "(session_id, part, idx, kind, payload, text) "
                    "VALUES ($1, 0, $2, $3, $4::json, 'words')",
                    session_id,
                    idx,
                    kind,
                    payload,
                )
    read = (
        "SELECT kind, payload::text AS payload, changed_xid::text AS stamp "
        "FROM session_records WHERE session_id = $1 ORDER BY idx"
    )

    async def rows() -> list[list[tuple[object, ...]]]:
        async with store.engine.acquire() as conn:
            return [
                [tuple(row) for row in await conn.fetch(read, session_id)]
                for session_id in sessions
            ]

    before = await rows()
    renamed = [await rename_shard(store.engine, shards=3, shard=s) for s in range(3)]
    after = await rows()
    again = [await rename_shard(store.engine, shards=3, shard=s) for s in range(3)]
    replayed = await rows()

    expected = [
        (renamed_records().get(kind, kind), after)
        for old, new in named
        for kind, _, after in _rows(old, new)
    ]
    # Of each name's seven rows, three name it in no tag of a record.
    assert [stats.rows for stats in renamed] == [4 * len(renamed_records())] * 3
    for rows_after, rows_before in zip(after, before, strict=True):
        assert [(kind, payload) for kind, payload, _ in rows_after] == expected
        # No text changed, so no row is restamped, and nothing is embedded again.
        assert [stamp for *_, stamp in rows_after] == [
            stamp for *_, stamp in rows_before
        ]
    assert [stats.rows for stats in again] == [0, 0, 0]
    assert replayed == after


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_shard_outside_the_count_is_refused(store: Store) -> None:
    with pytest.raises(ValueError, match=r"^shard 3 outside \[0, 3\)$"):
        _ = await rename_shard(store.engine, shards=3, shard=3)
    with pytest.raises(ValueError, match=r"^shard -1 outside \[0, 3\)$"):
        _ = await rename_shard(store.engine, shards=3, shard=-1)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
