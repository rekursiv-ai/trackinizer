"""Routes for the session IR: append records, list parts, read a part.

A session is several FILES -- claude splits on compaction, codex forks -- so
every route names a ``part``: the append resolves one from the file's
basename, ``parts`` lists them, and ``records`` pages through one.

There is deliberately no ``search`` route. Matching records is a filter on the
AgentSession list (``trax agentsession tool_call re bar``), not a second query
surface with its own grammar.

The append requires ``writer``, and for a science chat the key that opened the part
it writes, or for a new part a key the chat takes a run from; the reads require
``viewer``. Each route also answers 403 ``session_audience`` to a caller
who neither opened the session nor reads it. A read takes an ``asker`` and a ``chat``:
the configured assistant reads for that user, on the user's rights and never its own,
into the chat it answers them in (see ``require_session_access``), and anyone else's
``asker`` and ``chat`` are ignored. Tenant scope is derived by joining to
``inquiries``, as every session route does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from treekle import from_plain, immutable

from trackinizer.server.api._deps import get_assistant, get_inbound, get_store
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.session_access import (
    is_a_science_chat,
    require_part_writer,
    require_session,
    require_session_access,
    require_session_write_access,
    resolve_asker,
)
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.session_reaper import revive_if_reaped
from trackinizer.server.store.session_ir import SlashCommandRow
from trackinizer.server.visuals.science_chats import counts_in_chat
from trackinizer.wire.routes import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from trackinizer.wire.wire_science_chat import chat_conversation_id
from trackinizer.wire.wire_session_ir import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    PartBody,
    ReadPartsResponse,
    ReadRecentTurnsResponse,
    ReadRecordsResponse,
    RecentTurnBody,
    RecordBody,
)


if TYPE_CHECKING:
    from collections.abc import Sequence


router = APIRouter()


@router.post(
    "/api/sessions/{session_id}/records",
)
async def append_session_records_route(
    session_id: UUID,
    request: Request,
    body: AppendRecordsRequest,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> AppendRecordsResponse:
    """Append one file's records, resolving its part server-side.

    The manifest is upserted BEFORE the records, deliberately: it is what
    assigns the part number the records are keyed by, and it bounds every
    reader (``idx < records``). Writing records first would briefly expose
    rows no manifest accounts for.

    A body naming no file carries only slash commands -- a command is typed
    into the SESSION, and one submitted before the CLI has written a
    transcript has no part to belong to.

    A science chat holds one part per run that answered in it, and the key whose
    first write opened a part alone writes it after: another key's write is 403,
    so one run never rewrites another's lines.

    Args:
      session_id: AgentSession ID to append to.
      request: FastAPI request (carries auth and store).
      body: Records, manifest, and slash commands to append.
      identity: Authenticated writer or viewer who opened this session.

    Returns:
      result: AppendRecordsResponse with row counts.

    """
    store = get_store(request)
    session = await require_session(store, session_id)
    require_session_write_access(identity, session)
    await require_session_access(request, identity, session=session, mode="write")
    await require_part_writer(request, identity, session=session, name=body.name)
    # A run closed for silence that uploads again was alive all along; reopen
    # its session first, or the append is refused as a write to an ended one.
    if session.ended is not None:
        _ = await revive_if_reaped(store, session_id=session_id)
    manifest = body.manifest
    part = (
        None
        if manifest is None
        else await store.upsert_session_manifest(
            session_id,
            name=body.name,
            metadata=immutable(from_plain(manifest.metadata, dict[str, object])),
            ir_id=manifest.ir_id,
            format=manifest.format,
            records=manifest.records,
            api_key_id=identity.api_key_id,
            exclusive=is_a_science_chat(session),
        )
    )
    written, skipped, slash = await store.append_session_records(
        session_id,
        [record.row(session_id, part or 0) for record in body.records],
        restart=body.restart,
        slash_commands=[
            SlashCommandRow(
                timestamp=command.timestamp,
                command=command.command,
                args=command.args,
            )
            for command in body.slash_commands
        ],
    )
    await store.record_session_seen(
        session_id,
        at=datetime.now(UTC),
        polled=False,
    )
    # A chat's line its assistant took from the queue is counted as posted until here,
    # where only a session whose lines the chat counts records it.
    conversation = chat_conversation_id(session.cli_session_id)
    senders = _posters(body.records)
    if conversation is not None and senders:
        async with engine_of(request).acquire() as conn:
            counted = await counts_in_chat(
                conn,
                assistant=get_assistant(request),
                session_id=session_id,
                conversation_id=conversation,
            )
        if counted:
            get_inbound(request).recorded(conversation, senders=senders)
    return AppendRecordsResponse(
        part=part,
        written=written,
        skipped=skipped,
        slash_commands=slash,
    )


@router.get("/api/sessions/{session_id}/parts")
async def read_session_parts_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    asker: str | None = None,
    chat: UUID | None = None,
) -> ReadPartsResponse:
    """List the files this session was captured from, in ``part`` order.

    Args:
      session_id: AgentSession ID to list parts from.
      request: FastAPI request (carries auth and store).
      identity: Authenticated reader.
      asker: The account the configured assistant reads for.
      chat: The conversation the configured assistant reads into.

    Returns:
      result: ReadPartsResponse with manifest info for each part.

    """
    store = get_store(request)
    await require_session_access(
        request,
        identity,
        session=await require_session(store, session_id),
        mode="read",
        asker=await resolve_asker(request, identity, email=asker),
        chat=chat,
    )
    manifests = await store.read_session_manifests(session_id)
    return ReadPartsResponse(
        parts=[
            PartBody(
                part=m.part,
                name=m.name,
                format=m.format,
                records=m.records,
                # Carried so a resume can rewrite the file byte-exactly; see
                # ``PartBody.metadata``.
                metadata=m.metadata,
                ir_id=m.ir_id,
            )
            for m in manifests
        ],
    )


@router.get("/api/sessions/{session_id}/records")
async def read_session_records_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    part: int = 0,
    after_idx: int = -1,
    limit: int = DEFAULT_LIST_LIMIT,
    plaintext_only: bool = False,
    asker: str | None = None,
    chat: UUID | None = None,
) -> ReadRecordsResponse:
    """Read one page of a part's records.

    ``after_idx`` is an EXCLUSIVE lower bound rather than an offset, so paging
    is stable while a capture is still appending: an offset would re-window
    every time the part grew.

    ``plaintext_only`` skips the ciphertext splice, which is what a viewer
    wants -- only a replay needs the encrypted half, and it is the largest
    thing on the row.

    Args:
      session_id: AgentSession ID to read from.
      request: FastAPI request (carries auth and store).
      identity: Authenticated reader.
      part: Which file, as ``GET .../parts`` numbers it; ``-1`` holds turns
        backfilled from the retired event log.
      after_idx: Exclusive lower bound on idx (cursor-based paging).
      limit: Max rows per page.
      plaintext_only: Omit ciphertext.
      asker: The account the configured assistant reads for.
      chat: The conversation the configured assistant reads into.

    Returns:
      result: ReadRecordsResponse with matching records.

    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be in [1, {MAX_LIST_LIMIT}]",
        )
    # ``part`` is not range-checked. The listing names the reserved ``-1``, and a
    # reader fetches every part it names, so refusing one blanks the transcript.
    # A number no file resolved to has no manifest and reads as empty.
    store = get_store(request)
    await require_session_access(
        request,
        identity,
        session=await require_session(store, session_id),
        mode="read",
        asker=await resolve_asker(request, identity, email=asker),
        chat=chat,
    )
    rows = await store.read_session_records(
        session_id,
        part=part,
        after_idx=after_idx,
        limit=limit,
        plaintext_only=plaintext_only,
    )
    return ReadRecordsResponse(part=part, records=[RecordBody.of(row) for row in rows])


@router.get("/api/sessions/{session_id}/turns")
async def read_recent_session_turns_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    limit: int = 20,
    asker: str | None = None,
    chat: UUID | None = None,
) -> ReadRecentTurnsResponse:
    """Read recent conversation turns without transferring intervening tools.

    Args:
      session_id: AgentSession ID to read from.
      request: FastAPI request carrying auth and store.
      identity: Authenticated reader.
      limit: Maximum user and assistant turns, from 1 through 100.
      asker: The account the configured assistant reads for.
      chat: The conversation the configured assistant reads into.

    Returns:
      result: The newest turns in display order across session parts.

    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be in [1, 100]")
    store = get_store(request)
    await require_session_access(
        request,
        identity,
        session=await require_session(store, session_id),
        mode="read",
        asker=await resolve_asker(request, identity, email=asker),
        chat=chat,
    )
    turns = await store.read_recent_session_turns(session_id, limit=limit)
    return ReadRecentTurnsResponse(
        turns=[
            RecentTurnBody(
                part=turn.part,
                idx=turn.idx,
                kind=turn.kind,
                content=turn.content,
            )
            for turn in turns
        ],
    )


def _posters(records: Sequence[RecordBody]) -> list[str]:
    """Name the poster of each person's line among ``records``, in order."""
    return [sender for record in records if (sender := _poster(record)) is not None]


# The loader's rule for a person's line: the assistant records one as an
# ``AgentToAgentMessage`` with its sender, and a CLI run as a ``UserMessage`` that
# the agent runner stamped ``$inbound`` with the attested sender on delivery, or that carries
# an ``author``. Reading the first shape alone missed every line a CLI part records,
# which then kept counting as on its way into the chat until its hand-out expired.
def _poster(record: RecordBody) -> str | None:
    """Name who posted ``record`` if it is a person's line, from its stored payload."""
    payload = record.payload
    if not isinstance(payload, dict):
        return None
    if record.kind == "AgentToAgentMessage":
        sender = payload.get("sender")
    elif record.kind == "UserMessage" and isinstance(
        extra := payload.get("extra"),
        dict,
    ):
        stamp = extra.get("$inbound")
        sender = stamp.get("source") if isinstance(stamp, dict) else extra.get("author")
    else:
        return None
    return sender if isinstance(sender, str) else None
