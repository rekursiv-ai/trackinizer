"""Inquiry create routes: one per kind plus a batch endpoint.

A single create route handles every inquiry kind, with the kind carried
as the lowercase URL token. The batch route fans the same per-kind
dispatch over many items in one all-or-nothing transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import (
    AuthIdentity,
    assert_account_active,
    require_role,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.submit import SUBMIT_METHOD
from trackinizer.wire.bodies import (
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBase,
    SubmitBatch,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitItem,
    SubmitPaper,
    SubmitWebResult,
    SubmitWebSearch,
)


router = APIRouter()

# Each body class keyed by its PascalCase kind discriminator.
_BODY_BY_KIND: dict[str, type[SubmitBase]] = {
    cast(str, body.model_fields["kind"].default): body
    for body in (
        SubmitIssue,
        SubmitArtifact,
        SubmitExperiment,
        SubmitPaper,
        SubmitBelief,
        SubmitCodeChange,
        SubmitWebResult,
        SubmitWebSearch,
        SubmitAgentSession,
    )
}

# Lowercase URL kind token (the {kind} path segment) -> submit body. The
# token is just ``kind.lower()`` -- one canonical spelling per kind.
SUBMIT_BODY: dict[str, type[SubmitBase]] = {
    kind.lower(): body for kind, body in _BODY_BY_KIND.items()
}


# Register before /api/inquiries/{kind} so the static "batch" segment
# wins; Starlette matches routes in registration order.
@router.post("/api/inquiries/batch")
async def submit_batch_route(
    req: SubmitBatch,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Submit many inquiries in one all-or-nothing transaction.

    Every item commits together or none does: a single failure rolls the
    whole batch back, so no partial state is left behind. On success the
    response is ``{"ids": [...]}`` with the server-minted ids in input
    order. A failure propagates as the same HTTP error a single submit of
    that item would raise (e.g. 409 on a conflict, 422 on bad input).
    """
    store = get_store(request)
    # Resolve each item's account, then validate the DISTINCT set before the
    # all-or-nothing transaction: a single inactive account fails the whole
    # batch (matching the single-submit contract) without writing a partial
    # row, while a batch sharing one account costs one probe, not N.
    items: list[SubmitItem] = [
        item.model_copy(update={"account": _resolve_account(item, identity)})
        for item in req.items
    ]
    for account in {cast(str, item.account) for item in items}:
        await assert_account_active(store.engine, account)
    # Gate every edge endpoint that names an EXISTING row by UUID on
    ids = await store.submit_batch(
        items,
        edges=req.edges,
        api_key_id=identity.api_key_id,
        actor=identity.email,
    )
    return cast(MutableJSON, {"ids": [str(row_id) for row_id in ids]})


class _SubmitMethod(Protocol):
    def __call__(
        self,
        req: SubmitBase,
        *,
        api_key_id: UUID | None,
        actor: str,
    ) -> Awaitable[UUID]: ...


def _resolve_actor(req: SubmitBase, identity: AuthIdentity) -> str:
    """Pick the audit actor: the request override, else the caller's email."""
    return req.actor or identity.email


def _resolve_account(req: SubmitBase, identity: AuthIdentity) -> str:
    """Pick the attributed account: the body override, else the creator.

    The default is the authenticated ``identity.email`` -- never the
    spoofable ``actor`` override -- so an unspecified account always
    attributes the row to the real submitter.
    """
    return req.account or identity.email


async def _submit_one(
    store: Store,
    req: SubmitBase,
    identity: AuthIdentity,
) -> UUID:
    account = _resolve_account(req, identity)
    await assert_account_active(store.engine, account)
    method = cast(_SubmitMethod, getattr(store, SUBMIT_METHOD[type(req)]))
    return await method(
        req.model_copy(update={"account": account}),
        api_key_id=identity.api_key_id,
        actor=_resolve_actor(req, identity),
    )


@router.post("/api/inquiries/{kind}", status_code=201)
async def submit_route(
    kind: str,
    payload: MutableJSON,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Create one inquiry of the URL-token ``kind``; ``201`` on success.

    The body is validated against the matching ``SubmitX`` model. The
    ``kind`` discriminator is injected from the URL token, so callers
    never restate it and a mismatching body ``kind`` can't smuggle in a
    different model.
    """
    body_cls = SUBMIT_BODY.get(kind)
    if body_cls is None:
        raise HTTPException(status_code=404, detail=f"unknown inquiry kind {kind!r}")
    discriminator = cast(str, body_cls.model_fields["kind"].default)
    try:
        req = body_cls.model_validate({**payload, "kind": discriminator})
    except ValidationError as err:
        # ``include_context=False`` drops the per-error ``ctx`` (which carries
        # the raw ``ValueError`` for a custom field validator) so the detail is
        # JSON-serializable; without it any field-validator ``ValueError`` --
        # ``title``/``account``/``subscribers`` -- would 500 on response
        # encoding instead of returning the intended 422.
        raise HTTPException(
            status_code=422, detail=err.errors(include_context=False)
        ) from err
    return {"id": str(await _submit_one(get_store(request), req, identity))}
