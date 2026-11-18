"""What this server provides to run commands on, as its deployment notes it."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel

from trackinizer.server.auth import require_role
from trackinizer.server.config import ComputeNote, Config


__all__ = ["ComputeBody", "router"]


router = APIRouter()


class ComputeBody(BaseModel):
    """The deployment's notes, in the order it gave them; none when it gave none."""

    notes: list[ComputeNote]


@router.get("/api/compute", dependencies=[Depends(require_role("viewer"))])
async def compute_route(request: Request) -> ComputeBody:
    """List the compute this server provides, for the web app's Machines menu."""
    config: object = getattr(cast(FastAPI, request.app).state, "config", None)
    return ComputeBody(notes=list(config.compute if isinstance(config, Config) else ()))
