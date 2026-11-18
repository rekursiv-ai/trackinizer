"""Writing the records of asks into a session, for the tests that read them back."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from treekle import immutable

from trackinizer.trax.run.asks import (
    Ask,
    Option,
    Resolved,
    Resolver,
    opened_record,
    resolved_record,
)
from trackinizer.types.session_records import SessionRecordRow


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.agent.types.sessions import SessionRecord
    from trackinizer.server.store.core import Store


__all__ = ["command_ask", "ended_ask", "open_ask", "resolve_ask", "write_records"]


def command_ask(ask_id: str) -> Ask:
    """Return a command ask a run would open.

    Args:
      ask_id: The ask's id.

    Returns:
      ask: A claude command ask with one allow option.

    """
    return Ask(
        id=ask_id,
        provider="claude",
        kind="command",
        command="ls",
        options=(Option(id="yes", label="Yes", kind="allow_once"),),
    )


def ended_ask(ask_id: str, *, by: Resolver) -> Resolved:
    """Return how an ask ended, with no option taken.

    Args:
      ask_id: The ask's id.
      by: Who ended it.

    Returns:
      resolved: The end.

    """
    return Resolved(ask_id=ask_id, option="", by=by)


async def write_records(
    store: Store,
    session_id: UUID,
    records: Sequence[SessionRecord],
    *,
    name: str = "run.jsonl",
    bound: int | None = None,
) -> None:
    """Store ``records`` as the next batch of a file, as the append route does.

    Args:
      store: The store.
      session_id: The session.
      records: The records to append after those the file holds.
      name: The file's name, which is its part.
      bound: The live prefix the manifest declares; every record by default.

    """
    manifest = await store.read_session_manifest(session_id, name=name)
    start = 0 if manifest is None else manifest.records
    part = await store.upsert_session_manifest(
        session_id,
        name=name,
        metadata=immutable({}),
        ir_id=uuid4() if manifest is None else manifest.ir_id,
        format="",
        records=start + len(records) if bound is None else bound,
    )
    _ = await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=part,
                idx=start + number,
                record=record,
            )
            for number, record in enumerate(records)
        ],
    )


async def open_ask(store: Store, session_id: UUID, *, ask_id: str) -> None:
    """Store the record a run writes when it asks, after the file's others."""
    await write_records(
        store,
        session_id,
        [opened_record(command_ask(ask_id), at=datetime.now(UTC))],
    )


async def resolve_ask(
    store: Store,
    session_id: UUID,
    *,
    ask_id: str,
    by: Resolver = "chat",
) -> None:
    """Store the record that ends an ask, after the file's others."""
    await write_records(
        store,
        session_id,
        [resolved_record(ended_ask(ask_id, by=by), at=datetime.now(UTC))],
    )
