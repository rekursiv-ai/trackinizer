"""The addon contract: one application built on the trax graph.

An addon is a trusted configgle-built object. Its manifest names the parts it
contributes, and each part says where it runs:

- A **server service** runs as a task inside the trackinizer server, beside
  the subscriber push, with the server's own store.
- A **standalone service** runs in its own process, as its own user, and
  reaches trax only through the HTTP API with a profile token. A service that
  holds third-party credentials or spawns agents belongs here.
- A **router** adds HTTP routes, mounted under ``/api/addons/<name>``. Routers
  are HTTP-only: a WebSocket route is refused when the deployment is built.

A deployment (:mod:`trackinizer.addons.deployment`) names the addons it
runs; leaving an addon out switches it off everywhere at once.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from fastapi import APIRouter
from fastapi.routing import APIRoute, APIWebSocketRoute
from pydantic import BaseModel, Field

from trackinizer.client.client import Client
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store


__all__ = [
    "Addon",
    "AddonCatalogBody",
    "AddonDescription",
    "AddonManifest",
    "ServerContext",
    "ServerService",
    "ServiceDescription",
    "StandaloneContext",
    "StandaloneService",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerContext:
    """What a service running inside the trackinizer server may use.

    Attributes:
      store: The server's graph store, opened and bootstrapped.
      inbound: The live-session message queue, as the subscriber push uses it.

    """

    store: Store
    inbound: InboundQueue


@dataclass(frozen=True, slots=True, kw_only=True)
class StandaloneContext:
    """What a service running in its own process may use.

    Attributes:
      client: A trax HTTP client authenticated by the process's trax profile.

    """

    client: Client


class ServerService(Protocol):
    """A long-running task inside the trackinizer server."""

    name: str
    """Stable name within the addon, shown in logs and the catalog."""

    description: str
    """One sentence on what the service keeps doing."""

    async def run(self, context: ServerContext) -> None:
        """Serve until cancelled; a raise is logged and the service restarted."""
        ...


class StandaloneService(Protocol):
    """A long-running task in its own process, outside the trackinizer server."""

    name: str
    """Stable name within the addon, shown in logs and the catalog."""

    description: str
    """One sentence on what the service keeps doing."""

    async def run(self, context: StandaloneContext) -> None:
        """Serve until cancelled; a raise is logged and the service restarted."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonManifest:
    """The parts one addon contributes, by where each runs.

    The addon's name is not part of the manifest: it is the field name the addon
    has in the deployment config, so one addon class can run twice under two names.

    Attributes:
      title: Short human name.
      description: One sentence on what the addon does.
      server_services: Tasks run inside the trackinizer server.
      standalone_services: Tasks run by ``python -m trackinizer.addons run``.
      routers: HTTP routes mounted under ``/api/addons/<name>``.

    """

    title: str
    description: str
    server_services: Sequence[ServerService] = ()
    standalone_services: Sequence[StandaloneService] = ()
    routers: Sequence[APIRouter] = ()

    def describe(self, name: str) -> AddonDescription:
        """Project the manifest to its inert wire description.

        Args:
          name: The addon's field name in the deployment config.

        Returns:
          description: Names, services by placement, and mounted route paths,
            including the routes of every included sub-router.

        Raises:
          TypeError: A router holds a WebSocket route.

        """
        return AddonDescription(
            name=name,
            title=self.title,
            description=self.description,
            services=[
                *(
                    ServiceDescription(
                        name=service.name,
                        description=service.description,
                        placement="server",
                    )
                    for service in self.server_services
                ),
                *(
                    ServiceDescription(
                        name=service.name,
                        description=service.description,
                        placement="standalone",
                    )
                    for service in self.standalone_services
                ),
            ],
            routes=sorted(
                f"/api/addons/{name}{path}"
                for router in self.routers
                for path in _http_paths(router)
            ),
        )


@runtime_checkable
class Addon(Protocol):
    """A configured application that declares its parts without starting them."""

    def manifest(self) -> AddonManifest:
        """Return the addon's parts; must not open connections or read secrets."""
        ...


class ServiceDescription(BaseModel):
    """One service as the catalog shows it."""

    name: str = Field(min_length=1)
    description: str
    placement: Literal["server", "standalone"]


class AddonDescription(BaseModel):
    """One addon as the catalog shows it; holds no config values or secrets."""

    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str
    services: list[ServiceDescription]
    routes: list[str]


class AddonCatalogBody(BaseModel):
    """The addons this deployment runs."""

    addons: list[AddonDescription]


@runtime_checkable
class _IncludeContext(Protocol):
    """How a router was included into another: the part that shifts its paths."""

    prefix: str


@runtime_checkable
class _IncludedRouter(Protocol):
    """FastAPI's route-list entry for ``include_router``, which has no public type.

    FastAPI keeps an included router as one entry that points at the original,
    so a plain read of ``router.routes`` never sees the routes inside it.
    """

    original_router: APIRouter
    include_context: _IncludeContext


def _http_paths(router: APIRouter, prefix: str = "") -> Iterator[str]:
    """Yield the path of every HTTP route under ``router``, sub-routers included."""
    for route in router.routes:
        if isinstance(route, APIWebSocketRoute):
            raise TypeError(
                f"WebSocket route {prefix + route.path!r}: addon routers are HTTP-only.",
            )
        if isinstance(route, APIRoute):
            yield prefix + route.path
        elif isinstance(route, _IncludedRouter):
            yield from _http_paths(
                route.original_router,
                prefix=prefix + route.include_context.prefix,
            )
