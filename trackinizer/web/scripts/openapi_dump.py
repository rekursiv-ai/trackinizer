#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Dump the server's OpenAPI schema to src/api/openapi.json, which types the web app.

Run it after any change to a route, a parameter or a body, then run
`./npm run typecheck` from web/; openapi_drift_test.py fails until the committed
dump matches the in-repo app. The app is built as `python -m
trackinizer.server` serves it (`--web` is the default), so the dump equals
the live /openapi.json at the same commit. Output is normalised (sorted keys,
two-space indent) and the sha256 of the written text is printed; version.json
carries the same hash, so `--url` compares a live server with a build.

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

import httpx2

from trackinizer.lib.custom_json import DictCodec


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


def main() -> int:
    """Write the normalised schema and print its sha256.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    schema = _served_schema(flags.url) if flags.url else web_app_schema()
    text = render(schema)
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


def render(schema: Mapping[str, object]) -> str:
    """Normalise a schema to the committed text: sorted keys, two-space indent."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


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


def _served_schema(url: str) -> Mapping[str, object]:
    response = httpx2.get(f"{url.rstrip('/')}/openapi.json", timeout=30.0)
    _ = response.raise_for_status()
    return DictCodec.coerce(cast(object, json.loads(response.text)), default=None)


class _Flags(Protocol):
    url: str
    output: str


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
