"""Routes of the code store: file a record's code once per owner, read it, diff it.

Every write, and the question of which blobs are missing, names an ``owner``: the
caller's own email, or any for a caller who holds ``staff``. A read answers 404 for a
snapshot the caller may not read, as for one that does not exist, so a private
snapshot is never revealed; :mod:`~trackinizer.server.store.code_store` says who
reads which.
"""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trackinizer.server.api._deps import get_store
from trackinizer.server.api.session_access import session_reader
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.store.code_store import (
    SnapshotOwnerError,
    diff_snapshots,
    missing_blobs,
    put_blobs,
    put_snapshot,
    read_file,
    read_snapshot,
)
from trackinizer.wire.audience import STAFF
from trackinizer.wire.wire_code import (
    CODE_BLOBS_PATH,
    CODE_DIFF_PATH,
    CODE_FILE_PATH,
    CODE_MISSING_PATH,
    CODE_SNAPSHOT_PATH,
    DEFAULT_DIFF_CONTEXT,
    MAX_DIFF_CONTEXT,
    CodeBlobsPut,
    CodeBlobsStored,
    CodeDiff,
    CodeFile,
    CodeMissing,
    CodeMissingRequest,
    CodeSnapshot,
    CodeSnapshotHead,
    CodeSnapshotPut,
)


__all__ = [
    "code_diff_route",
    "code_file_route",
    "code_missing_route",
    "code_put_blobs_route",
    "code_put_snapshot_route",
    "code_snapshot_route",
    "router",
]


router = APIRouter()


@router.post(CODE_MISSING_PATH, response_model=CodeMissing)
async def code_missing_route(
    body: CodeMissingRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> CodeMissing:
    """Return which of the hashes the store lacks for the owner, in the order asked.

    Args:
      body: The owner and the hashes, at most ``MAX_MISSING_HASHES``.
      request: Request carrying the store.
      identity: Writer filing code under ``body.owner``.

    Returns:
      missing: The hashes not stored for the owner, each once.

    """
    _require_owner(identity, owner=body.owner)
    return CodeMissing(
        missing=await missing_blobs(
            get_store(request).engine,
            owner=body.owner,
            hashes=body.sha256,
        ),
    )


@router.post(CODE_BLOBS_PATH, response_model=CodeBlobsStored)
async def code_put_blobs_route(
    body: CodeBlobsPut,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> CodeBlobsStored:
    """Store blobs for the owner after checking each hash against its text.

    Args:
      body: The owner and the blobs.
      request: Request carrying the store.
      identity: Writer filing code under ``body.owner``.

    Returns:
      stored: How many blobs were new.

    """
    _require_owner(identity, owner=body.owner)
    return CodeBlobsStored(
        stored=await put_blobs(
            get_store(request).engine,
            owner=body.owner,
            blobs=body.blobs,
        ),
    )


@router.put(CODE_SNAPSHOT_PATH, response_model=CodeSnapshotHead)
async def code_put_snapshot_route(
    record_id: uuid.UUID,
    body: CodeSnapshotPut,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> CodeSnapshotHead:
    """File a record's code, replacing the snapshot it had.

    Args:
      record_id: The record the code belongs to.
      body: The snapshot.
      request: Request carrying the store.
      identity: Writer filing code under ``body.owner``.

    Returns:
      head: The snapshot as stored.

    Raises:
      HTTPException: 403 when the owner is not the caller's and the caller does not
        hold ``staff``, or the record's snapshot is another owner's.

    """
    _require_owner(identity, owner=body.owner)
    try:
        return await put_snapshot(
            get_store(request).engine,
            record_id=record_id,
            body=body,
            created_by=identity.user_id,
            replace_others=STAFF in identity.read_audiences,
        )
    except SnapshotOwnerError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get(CODE_SNAPSHOT_PATH, response_model=CodeSnapshot)
async def code_snapshot_route(
    record_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CodeSnapshot:
    """Return a record's snapshot and its files, sorted by path.

    Args:
      record_id: The record.
      request: Request carrying the store.
      identity: Viewer reading it.

    Returns:
      snapshot: The snapshot, without its owner's email.

    """
    snapshot = await read_snapshot(
        get_store(request).engine,
        record_id=record_id,
        reader=session_reader(request, identity),
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No code snapshot for this record.")
    return snapshot


@router.get(CODE_FILE_PATH, response_model=CodeFile)
async def code_file_route(
    record_id: uuid.UUID,
    path: Annotated[str, Query()],
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CodeFile:
    """Return one file of a record's snapshot, with its text.

    Args:
      record_id: The record.
      path: The file's path in the snapshot.
      request: Request carrying the store.
      identity: Viewer reading it.

    Returns:
      file: Its path, hash and text.

    """
    found = await read_file(
        get_store(request).engine,
        record_id=record_id,
        path=path,
        reader=session_reader(request, identity),
    )
    if found is None:
        raise HTTPException(status_code=404, detail="No such file in a code snapshot.")
    return found


@router.get(CODE_DIFF_PATH, response_model=CodeDiff)
async def code_diff_route(
    from_record: Annotated[uuid.UUID, Query(alias="from")],
    to_record: Annotated[uuid.UUID, Query(alias="to")],
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    context: Annotated[int, Query(ge=0, le=MAX_DIFF_CONTEXT)] = DEFAULT_DIFF_CONTEXT,
) -> CodeDiff:
    """Return how one record's code differs from another's, path by path.

    Args:
      from_record: The record whose snapshot is the old side.
      to_record: The record whose snapshot is the new side.
      request: Request carrying the store.
      identity: Viewer reading both.
      context: Lines of context around each change.

    Returns:
      diff: Each path that differs, sorted, with its unified diff.

    """
    diff = await diff_snapshots(
        get_store(request).engine,
        from_id=from_record,
        to_id=to_record,
        context=context,
        reader=session_reader(request, identity),
    )
    if diff is None:
        raise HTTPException(status_code=404, detail="No code snapshot for a record.")
    return diff


def _require_owner(identity: AuthIdentity, *, owner: str) -> None:
    """Refuse filing code under another's email by a caller who does not hold staff."""
    if owner != identity.email.lower() and STAFF not in identity.read_audiences:
        raise HTTPException(
            status_code=403,
            detail="Code is filed under the caller's own email, or by staff under any",
        )
