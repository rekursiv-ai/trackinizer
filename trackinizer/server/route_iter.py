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

from collections.abc import Iterator
from typing import TYPE_CHECKING

from fastapi.routing import _IncludedRouter, _RouterIncludeContext
from starlette.routing import Mount, Route, WebSocketRoute


if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.routing import BaseRoute


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
    yield from _walk(app.router.routes, context=None)


def registered_paths(app: FastAPI) -> set[str]:
    """All registered route path templates of ``app``."""
    return {path for path, _ in iter_routes(app)}


def registered_path_methods(app: FastAPI) -> set[tuple[str, str]]:
    """All registered ``(path, method)`` pairs of ``app``."""
    return {(path, method) for path, methods in iter_routes(app) for method in methods}


def _walk(
    routes: list[BaseRoute],
    *,
    context: _RouterIncludeContext | None,
) -> Iterator[tuple[str, frozenset[str]]]:
    """Recurse the route tree, composing include-prefixes as it descends."""
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
        methods = frozenset(getattr(route, "methods", None) or ())
        yield (path, methods)
