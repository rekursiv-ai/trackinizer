"""Routes for the session IR: append records, list parts, read a part.

A session is several FILES -- claude splits on compaction, codex forks -- so
every route names a ``part``: the append resolves one from the file's
basename, ``parts`` lists them, and ``records`` pages through one.

There is deliberately no ``search`` route. Matching records is a filter on the
AgentSession list (``trax agentsession tool_call re bar``), not a second query
surface with its own grammar.

The append requires ``writer``; the reads require ``viewer``. Tenant scope is
derived by joining to ``inquiries``, as every session route does.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from trackinizer.lib.custom_json import DictCodec, json_freeze
from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import require_role
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_ir import SlashCommandRow
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.routes import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from trackinizer.wire.wire_session_ir import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    PartBody,
    ReadPartsResponse,
    ReadRecordsResponse,
    RecordBody,
)


router = APIRouter()


@router.post(
    "/api/sessions/{session_id}/records",
    dependencies=[Depends(require_role("writer"))],
)
async def append_session_records_route(
    session_id: UUID, request: Request, body: AppendRecordsRequest
) -> AppendRecordsResponse:
    """Append one file's records, resolving its part server-side.

    The manifest is upserted BEFORE the records, deliberately: it is what
    assigns the part number the records are keyed by, and it bounds every
    reader (``idx < records``). Writing records first would briefly expose
    rows no manifest accounts for.

    A body naming no file carries only slash commands -- a command is typed
    into the SESSION, and one submitted before the CLI has written a
    transcript has no part to belong to.
    """
    store = get_store(request)
    await _require_session(store, session_id)
    manifest = body.manifest
    part = (
        None
        if manifest is None
        else await store.upsert_session_manifest(
            session_id,
            name=body.name,
            metadata=json_freeze(DictCodec.coerce(manifest.metadata)),
            ir_id=manifest.ir_id,
            format=manifest.format,
            records=manifest.records,
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
    return AppendRecordsResponse(
        part=part, written=written, skipped=skipped, slash_commands=slash
    )


@router.get(
    "/api/sessions/{session_id}/parts",
    dependencies=[Depends(require_role("viewer"))],
)
async def read_session_parts_route(
    session_id: UUID, request: Request
) -> ReadPartsResponse:
    """List the files this session was captured from, in ``part`` order."""
    store = get_store(request)
    await _require_session(store, session_id)
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
        ]
    )


@router.get(
    "/api/sessions/{session_id}/records",
    dependencies=[Depends(require_role("viewer"))],
)
async def read_session_records_route(
    session_id: UUID,
    request: Request,
    *,
    part: int = 0,
    after_idx: int = -1,
    limit: int = DEFAULT_LIST_LIMIT,
    plaintext_only: bool = False,
) -> ReadRecordsResponse:
    """Read one page of a part's records.

    ``after_idx`` is an EXCLUSIVE lower bound rather than an offset, so paging
    is stable while a capture is still appending: an offset would re-window
    every time the part grew.

    ``plaintext_only`` skips the ciphertext splice, which is what a viewer
    wants -- only a replay needs the encrypted half, and it is the largest
    thing on the row.
    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"limit must be in [1, {MAX_LIST_LIMIT}]"
        )
    if part < 0:
        raise HTTPException(status_code=400, detail="part must be >= 0")
    store = get_store(request)
    await _require_session(store, session_id)
    rows = await store.read_session_records(
        session_id,
        part=part,
        after_idx=after_idx,
        limit=limit,
        plaintext_only=plaintext_only,
    )
    return ReadRecordsResponse(part=part, records=[RecordBody.of(row) for row in rows])


async def _require_session(store: Store, session_id: UUID) -> AgentSession:
    """Fetch an AgentSession row or 404; reject a non-session id."""
    row = await store.get_inquiry(session_id)
    if not isinstance(row, AgentSession):
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return row
