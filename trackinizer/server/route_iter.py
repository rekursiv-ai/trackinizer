"""Flatten a FastAPI app's route tree back into ``(path, methods)`` leaves.

FastAPI 0.137 made ``app.include_router`` lazy: instead of cloning each
child route onto ``app.routes`` with the prefix baked in, it stores a single
``_IncludedRouter`` node holding the original (un-prefixed) router. Iterating
``app.routes`` therefore no longer yields the registered ``/api/...`` paths --
it yields opaque tree nodes whose ``path`` is ``None`` (the regression that
broke every route-introspection test here). The release notes are explicit
that ``router.routes`` is now "an internal implementation detail."

This module re-derives the flat view the tests need: a recursive walk that
descends each ``_IncludedRouter``, composing prefixes via the router's own
include context so each leaf carries its fully-qualified path and verbs. It is
the single place that reaches into FastAPI's route-tree internals; every drift
and attach test consumes :func:`iter_routes` instead of touching ``app.routes``
directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi.params import Depends
from fastapi.routing import APIRoute, _IncludedRouter, _RouterIncludeContext
from starlette.routing import Mount, Route, WebSocketRoute


if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from starlette.routing import BaseRoute


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiRouteLeaf:
    """One HTTP route of an app, as a request reaches it.

    Attributes:
      path: The fully-prefixed path template (``/api/admin/users``).
      route: The route; its ``dependant`` holds its own dependencies.
      include_dependencies: What the routers it was included through add
        (``include_router(..., dependencies=...)``), which run before the route's
        own and which its ``dependant`` does not hold.

    """

    path: str
    route: APIRoute
    include_dependencies: tuple[Depends, ...]


def iter_routes(app: FastAPI) -> Iterator[tuple[str, frozenset[str]]]:
    """Yield ``(path, methods)`` for every registered route leaf of ``app``.

    Descends the ``_IncludedRouter`` tree introduced in FastAPI 0.137 so each
    leaf's path is the fully-prefixed template (``/api/web/search``), exactly
    what iterating ``app.routes`` yielded before the lazy-include change.
    Multiplicity is preserved -- a path registered twice yields twice -- so
    idempotency checks remain meaningful.

    Args:
      app: The application (or sub-app) whose route tree to flatten.

    Yields:
      route: A ``(path, methods)`` pair. ``methods`` is the route's HTTP verb
        set (empty for mounts and other verb-less routes).

    """
    for path, route in iter_route_leaves(app):
        yield (path, frozenset(getattr(route, "methods", None) or ()))


def iter_route_leaves(app: FastAPI) -> Iterator[tuple[str, BaseRoute]]:
    """Yield every route of ``app`` with its fully-prefixed path: HTTP, socket and mount.

    Args:
      app: The application whose route tree to flatten.

    Yields:
      leaf: A ``(path, route)`` pair, the route the one a request that reaches it
        carries as its scope's ``route``.

    """
    for path, route, _context in _walk(app.router.routes, context=None):
        yield path, route


def iter_api_routes(app: FastAPI) -> Iterator[ApiRouteLeaf]:
    """Yield every HTTP route of ``app``, fully prefixed; no socket and no mount.

    Args:
      app: The application whose route tree to flatten.

    Yields:
      leaf: Each route, with its path and what its includes add.

    """
    for path, route, context in _walk(app.router.routes, context=None):
        if isinstance(route, APIRoute):
            yield ApiRouteLeaf(
                path=path,
                route=route,
                include_dependencies=()
                if context is None
                else tuple(context.dependencies),
            )


def registered_paths(app: FastAPI) -> set[str]:
    """All registered route path templates of ``app``.

    Args:
      app: App.

    Returns:
      result: The set[str].

    """
    return {path for path, _ in iter_routes(app)}


def registered_path_methods(app: FastAPI) -> set[tuple[str, str]]:
    """All registered ``(path, method)`` pairs of ``app``.

    Args:
      app: App.

    Returns:
      result: The set[tuple[str, str]].

    """
    return {(path, method) for path, methods in iter_routes(app) for method in methods}


def _walk(
    routes: list[BaseRoute],
    *,
    context: _RouterIncludeContext | None,
) -> Iterator[tuple[str, BaseRoute, _RouterIncludeContext | None]]:
    """Recurse the route tree, composing include contexts as it descends."""
    for route in routes:
        if isinstance(route, _IncludedRouter):
            child = route.include_context
            yield from _walk(
                route.original_router.routes,
                context=child if context is None else context.combine(child),
            )
            continue
        if not isinstance(route, (Route, WebSocketRoute, Mount)):
            continue
        path = route.path if context is None else context.path_for(route)
        yield (path, route, context)
