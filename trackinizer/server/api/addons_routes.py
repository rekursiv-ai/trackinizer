"""The addons this server's deployment runs, and their mounted routes."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, FastAPI, Request

from trackinizer.addons.addon import AddonCatalogBody
from trackinizer.addons.deployment import Deployment
from trackinizer.server.auth import require_role


__all__ = ["attach_deployment", "deployment_of", "router"]


router = APIRouter()


@router.get("/api/addons", dependencies=[Depends(require_role("viewer"))])
async def addons_route(request: Request) -> AddonCatalogBody:
    """Describe every addon of this deployment without touching its services."""
    return deployment_of(cast(FastAPI, request.app)).catalog()


def deployment_of(app: FastAPI) -> Deployment:
    """Return the deployment attached to ``app``, or the one with no addons."""
    deployment: object = getattr(app.state, "deployment", None)
    if isinstance(deployment, Deployment):
        return deployment
    return Deployment.Config().make()


def attach_deployment(app: FastAPI, deployment: Deployment) -> None:
    """Make ``deployment`` the app's: mount its routers and adopt its visuals.

    Each addon's routers mount under ``/api/addons/<name>``, viewers only. The
    prefix keeps an addon from shadowing a core route, and the viewer dependency
    keeps a router that forgot its own auth from serving anyone who can reach the
    port. A route that writes adds its own stricter role.

    Idempotent: a second call is a no-op, so the first deployment wins.
    ``server._configure_app`` attaches the module-global app, and a test that
    reuses it must not stack a second copy of every router.

    Args:
      app: The app to attach to, before it starts serving.
      deployment: The deployment whose routers to mount and whose visual catalog
        the visual routes read from ``app.state.visual_catalog``.

    """
    if getattr(app.state, "deployment_attached", False):
        return
    app.state.deployment_attached = True
    app.state.deployment = deployment
    app.state.visual_catalog = deployment.visuals
    for name, manifest in deployment.manifests.items():
        for addon_router in manifest.routers:
            app.include_router(
                addon_router,
                prefix=f"/api/addons/{name}",
                dependencies=[Depends(require_role("viewer"))],
            )
