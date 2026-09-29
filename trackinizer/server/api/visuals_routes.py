"""Authenticated catalog of available visual modules."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from trackinizer.server.auth import require_role
from trackinizer.server.visuals.catalog import (
    VisualCatalogBody,
    default_catalog,
)


router = APIRouter()


@router.get("/api/visuals", dependencies=[Depends(require_role("viewer"))])
async def visuals_route() -> VisualCatalogBody:
    """List visual definitions and the default without loading visual data."""
    return default_catalog()
