#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 -P "$0" "$@"
Dump the server's OpenAPI schema to src/api/openapi.json, which types the web app.

Run it after any change to a route, a parameter or a body, then run
`./npm run typecheck` from web/; openapi_drift_test.py fails until the committed
dump matches the in-repo app. The app is built as `python -m
trackinizer.server` serves it (`--web` is the default), so the dump equals
the live /openapi.json at the same commit, less its addons. A deployment's addons
add routes under /api/addons/<name>/, which the web app types against each
addon's own schema, so the dump leaves them out, with the component schemas only
they use. Output is normalised (sorted keys, one-space indent) and the sha256 of
the written text is printed; version.json carries the same hash, so `--url`
compares a live server with a build whatever addons the server runs.

Examples:
  ./openapi_dump.py                                  # rewrite src/api/openapi.json
  ./openapi_dump.py --url http://127.0.0.1:8765 --output -  # a live server's schema

'''
# fmt: on

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

import argparse
import hashlib
import json
import sys

from treekle import MutablePlainTree, from_plain, json_loads

import httpx2


if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI

    from trackinizer.server import web
    from trackinizer.server.api import app
else:
    from wrapt import lazy_import

    # ~350 ms together; `--url` reads a running server and never builds the app.
    FastAPI = lazy_import("fastapi", "FastAPI")
    web = lazy_import("trackinizer.server.web")
    # The module, not its attributes: an attribute proxy of a tuple cannot be
    # iterated, while an attribute read through a module proxy is the real value.
    app = lazy_import("trackinizer.server.api.app")


_CWD: Final = Path(__file__).resolve().parent

OPENAPI_JSON: Final = _CWD.parent / "src" / "api" / "openapi.json"
"""The committed dump the web app's client is generated from."""

ADDON_ROUTES: Final = "/api/addons/"
"""Where a deployment mounts its addons' routes, one directory per addon. The route
listing the addons, ``/api/addons``, is the server's own."""

_COMPONENT: Final = "#/components/schemas/"


def main() -> int:
    """Write the normalised schema and print its sha256.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 2)[2] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    schema = (
        _served_schema(flags.url)
        if flags.url
        else from_plain(web_app_schema(), dict[str, MutablePlainTree])
    )
    text = render(core_schema(schema))
    if flags.output == "-":
        sys.stdout.write(text)
    else:
        Path(flags.output).write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    print(f"openapi_sha256 {digest}", file=sys.stderr)
    return 0


def web_app_schema() -> Mapping[str, object]:
    """Return the schema of the app ``python -m trackinizer.server`` serves.

    ``--web`` (the default) attaches the read API under ``/api/web``; page routes,
    ``/static`` and ``--app-dir`` stay out of the schema, so no directory matters.
    It builds its own app from ``ROUTERS``, the routers the module-level app is
    built from, instead of copying that app's routes: the module-level app is
    shared by every server test in the process, and a test that runs
    ``_configure_app`` leaves the web routes attached to it, so a copy would
    attach them twice.

    Returns:
      schema: The OpenAPI document, as ``GET /openapi.json`` would return it.

    """
    served = FastAPI(title=app.app.title, version=app.app.version)
    for router in app.ROUTERS:
        served.include_router(router)
    web.attach(served)
    return served.openapi()


def core_schema(schema: Mapping[str, MutablePlainTree]) -> dict[str, MutablePlainTree]:
    """Return ``schema`` without its addon routes and the components only they use.

    A component an addon route uses stays when anything else uses it too, and one
    nothing uses stays as it is, so the server's own schema comes back unchanged.

    Args:
      schema: An OpenAPI document, as ``GET /openapi.json`` returns it.

    Returns:
      core: The document the web app is typed against.

    """
    paths = _object(schema.get("paths"))
    addon = {path: op for path, op in paths.items() if path.startswith(ADDON_ROUTES)}
    kept_paths: dict[str, MutablePlainTree] = {
        path: op for path, op in paths.items() if path not in addon
    }
    core: dict[str, MutablePlainTree] = {**schema, "paths": kept_paths}
    if "components" not in schema:
        return core
    components = _object(schema.get("components"))
    named = _object(components.get("schemas"))
    by_addons = _closure(_references(addon), named=named)
    outside: dict[str, MutablePlainTree] = {
        key: value for key, value in core.items() if key != "components"
    }
    kept = _closure(_references(outside) | (named.keys() - by_addons), named=named)
    schemas: dict[str, MutablePlainTree] = {
        name: value for name, value in named.items() if name in kept
    }
    return {**core, "components": {**components, "schemas": schemas}}


def render(schema: Mapping[str, object]) -> str:
    """Normalise a schema to the committed text: sorted keys, one-space indent."""
    return json.dumps(schema, indent=1, sort_keys=True) + "\n"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    parser.add_argument(
        "--url",
        default="",
        help="Read /openapi.json from this server instead of the in-repo app.",
    )
    parser.add_argument(
        "--output",
        default=str(OPENAPI_JSON),
        help="Where to write the normalised schema; '-' writes to stdout.",
    )


def _object(value: MutablePlainTree) -> dict[str, MutablePlainTree]:
    """Return ``value`` if it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _closure(names: set[str], *, named: Mapping[str, MutablePlainTree]) -> set[str]:
    """Return ``names`` and every component schema they reach through ``$ref``."""
    reached: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name not in reached:
            reached.add(name)
            todo.extend(_references(named.get(name)))
    return reached


def _references(value: MutablePlainTree) -> set[str]:
    """Return the component schemas ``value`` names by ``$ref``, at any depth."""
    found: set[str] = set()
    todo = [value]
    while todo:
        item = todo.pop()
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and ref.startswith(_COMPONENT):
                found.add(ref.removeprefix(_COMPONENT))
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return found


def _served_schema(url: str) -> Mapping[str, MutablePlainTree]:
    response = httpx2.get(f"{url.rstrip('/')}/openapi.json", timeout=30.0)
    _ = response.raise_for_status()
    return json_loads(response.text, dict[str, MutablePlainTree])


class _Flags(Protocol):
    url: str
    output: str


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
