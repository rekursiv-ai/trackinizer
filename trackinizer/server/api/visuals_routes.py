"""Authenticated catalog of available visual modules."""

from __future__ import annotations

from typing import Protocol, cast

from fastapi import APIRouter, Depends, Request

from trackinizer.server.auth import require_role
from trackinizer.server.visuals.catalog import VisualCatalogBody, Workspace


router = APIRouter()


def visual_catalog(request: Request) -> Workspace:
    """Return the deployment's visual catalog held on the FastAPI app state.

    Args:
      request: Request.

    Returns:
      workspace: The built visual workspace.

    """
    return cast(_App, request.app).state.visual_catalog


@router.get("/api/visuals", dependencies=[Depends(require_role("viewer"))])
async def visuals_route(request: Request) -> VisualCatalogBody:
    """List visual definitions and the default without loading visual data."""
    return visual_catalog(request).catalog()


class _App(Protocol):
    state: _State


class _State(Protocol):
    visual_catalog: Workspace
