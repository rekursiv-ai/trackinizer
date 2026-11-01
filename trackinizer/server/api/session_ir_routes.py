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

from trackinizer.server.api._deps import (
    get_store,
)
from trackinizer.server.api.session_access import (
    require_session,
    require_session_access,
    require_session_write_access,
    resolve_asker,
)
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.session_reaper import revive_if_reaped
from trackinizer.server.store.session_ir import SlashCommandRow
from trackinizer.wire.routes import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from trackinizer.wire.wire_session_ir import (
    CLIENT_OUTDATED,
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
    _refuse_outdated_records(body.records)
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
        asker=await resolve_asker(request, identity, email=asker, chat=chat),
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
        asker=await resolve_asker(request, identity, email=asker, chat=chat),
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
        asker=await resolve_asker(request, identity, email=asker, chat=chat),
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


# A client older than short class tags names each class by its import path, which no
# reader of the stored record accepts. Its encoder tags the record itself that way, so
# the top-level tag alone tells it apart: a dotted tag deeper down may be provider JSON.
# 426 rather than 400 or 422: a client takes those as one record it can never store and
# sends a stand-in in its place, while any other refusal keeps its run capturing to a
# local file and shows the person this message.
def _refuse_outdated_records(records: Sequence[RecordBody]) -> None:
    """Refuse a batch any of whose records names its class by an import path."""
    if any(
        "." in tag
        for record in records
        if isinstance(tag := record.payload.get("py/object"), str)
    ):
        raise HTTPException(
            status_code=426,
            detail={
                "code": CLIENT_OUTDATED,
                "message": (
                    "Update this client and trax (uv tool upgrade --all): it writes "
                    "session records in a retired format"
                ),
            },
        )
