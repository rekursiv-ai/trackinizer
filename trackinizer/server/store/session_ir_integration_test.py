"""Session record parts against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from treekle import immutable

import pytest

from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stdout
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Session record parts, each read back from the database."""

    async def test_the_parts_of_one_session_each_keep_their_own_lines(
        self,
        integ_store: Store,
    ) -> None:
        """A part is keyed by its name, so runs writing parts of their own lose nothing.

        An agent runner that writes into a session it did not open gives each run a part
        of its own; a store that merged them would drop all but the last run's lines.
        """
        session_id = await integ_store.submit_agentsession(
            SubmitAgentSession(
                title="attached",
                cli="sh",
                account="tester@example.com",
            ),
            audience="open",
        )
        for name, lines in (
            ("a.jsonl", ["first\n", "second\n"]),
            ("b.jsonl", ["third\n"]),
        ):
            part = await integ_store.upsert_session_manifest(
                session_id,
                name=name,
                metadata=immutable({}),
                ir_id=uuid5(NAMESPACE_URL, f"{session_id}:{name}"),
                format="",
                records=len(lines),
            )
            await integ_store.append_session_records(
                session_id,
                [
                    SessionRecordRow.of(
                        session_id=session_id,
                        part=part,
                        idx=idx,
                        record=Stdout(text=line),
                    )
                    for idx, line in enumerate(lines)
                ],
            )

        stored = [
            row.text
            for manifest in await integ_store.read_session_manifests(session_id)
            for row in await integ_store.read_session_records(
                session_id,
                part=manifest.part,
            )
            if row.kind == "Stdout"
        ]

        assert stored == ["first\n", "second\n", "third\n"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
