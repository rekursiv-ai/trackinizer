"""Server entry point: arg parsing, logging, and uvicorn launch.

The ``types/`` package is the design contract; this module starts the
storage + HTTP realization on top of it, backed by Postgres (real or
PGlite via py-pglite). ``server/__main__.py`` is the runnable wrapper
that just calls :func:`main`.

Usage::

    python -m trackinizer.server                   # PGlite
    python -m trackinizer.server --engine pg --dsn ...
"""

from __future__ import annotations

from pathlib import Path
from typing import override

import argparse
import logging
import os

import uvicorn

from trackinizer.lib.userdirs import data_dir
from trackinizer.server import web
from trackinizer.server.api.app import app
from trackinizer.server.config import (
    Config,
    ConfigError,
)


logger = logging.getLogger("trackinizer")


class _SuppressZeroTaskCancel(logging.Filter):
    """Drop uvicorn's spurious "Cancel 0 running task(s)" shutdown ERROR.

    With ``timeout_graceful_shutdown=0`` uvicorn's shutdown always trips its
    ``asyncio.TimeoutError`` branch and logs ``Cancel N running task(s),
    timeout graceful shutdown exceeded`` -- even on a clean shutdown with N=0,
    where nothing was actually cancelled. That zero-task case is noise on every
    normal exit, so suppress exactly it; a real cancel (N>0) still logs.
    """

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != (
            "Cancel 0 running task(s), timeout graceful shutdown exceeded"
        )


def main() -> None:
    """Parse args, configure the app, and run uvicorn."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    args, remaining = _parse_args(parser)
    if remaining:
        parser.error(f"unrecognized arguments: {' '.join(remaining)}")
    # PGlite is single-process; two engines on one workdir corrupt the
    # catalog, so worker fan-out is only safe on Postgres.
    if args.workers > 1 and args.engine == "pglite":
        parser.error(
            "PGlite is single-process; --workers > 1 corrupts the workdir. "
            "Use --workers 1 or run on --engine pg."
        )
    _configure_logging(args.log_level)
    try:
        app.state.config = Config.from_args(args)
    except ConfigError as err:
        # Library code raises ConfigError (a plain Exception); the CLI is
        # the one place that turns a bad config into a clean process exit.
        raise SystemExit(str(err)) from err
    if args.web:
        web.attach(app, static_dir=args.static_dir)
    # Silence uvicorn's spurious zero-task cancel ERROR on every clean shutdown
    # (a consequence of timeout_graceful_shutdown=0); a real N>0 cancel still logs.
    logging.getLogger("uvicorn.error").addFilter(_SuppressZeroTaskCancel())
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=args.workers,
        timeout_keep_alive=args.timeout_keep_alive,
        # Force-close connections immediately on shutdown instead of waiting for
        # them to drain. Without this, uvicorn's default (wait indefinitely) made
        # SIGTERM hang on a held connection (e.g. an open Web UI tab) until the
        # 240s keep-alive window -- the "Waiting for connections to close" stall.
        # In-flight DB writes are atomic (a force-closed asyncpg/PGlite tx rolls
        # back), so there is nothing to drain for; 0 = don't wait, tear down now.
        timeout_graceful_shutdown=0,
    )


def _parse_args(
    parser: argparse.ArgumentParser,
    argv: list[str] | None = None,
) -> tuple[argparse.Namespace, list[str]]:
    parser.add_argument(
        "--engine",
        default="pglite",
        choices=["pglite", "pg"],
        help="Database substrate (default: pglite).",
    )
    parser.add_argument(
        "--datadir",
        type=Path,
        default=None,
        help=(
            "PGlite working directory (default: "
            f"{data_dir('rekursiv-ai') / 'trackinizer' / 'pgdata'})."
        ),
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="Run PGlite in-memory.",
    )
    parser.add_argument(
        "--pglite-tcp",
        action="store_true",
        help=(
            "Open PGlite on a TCP port instead of its default Unix socket. "
            "Only needed when the DB must be reachable over a port (a "
            "non-co-located client or TCP healthcheck); the Unix-socket "
            "default has no port to race."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("TRACKINIZER_DSN", ""),
        help="Postgres DSN (when --engine pg).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of uvicorn workers. Must stay at 1 for --engine pglite "
            "(workdir corruption otherwise). Postgres can run more."
        ),
    )
    # Server idle must exceed the upstream proxy's idle timeout so the
    # proxy owns eviction; otherwise it holds dead sockets and surfaces
    # non-idempotent POSTs as 502. 240s = 2x caddy's 2m default.
    parser.add_argument(
        "--timeout-keep-alive",
        type=int,
        default=240,
        help=(
            "Seconds an idle keep-alive connection is held open before the "
            "server closes it. Must exceed the upstream proxy's idle timeout "
            "(caddy default 120s) so the proxy controls eviction (default: 240)."
        ),
    )
    parser.add_argument(
        "--embedder",
        default="stub",
        choices=["stub"],
        help="Embedding backend (default: stub).",
    )
    parser.add_argument(
        "--web",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help=(
            "Serve /static from this runtime directory instead of the SPA's "
            "bundled assets/static. Lets an operator publish files written "
            "after deploy (e.g. a generated report) without writing into the "
            "source tree. Unset keeps the bundled assets."
        ),
    )
    parser.add_argument(
        "--session-max-age-seconds",
        type=int,
        default=int(
            os.environ.get(
                "TRACKINIZER_SESSION_MAX_AGE_SECONDS", str(30 * 24 * 60 * 60)
            )
        ),
        help=(
            "Session-cookie TTL in seconds (default: 30 days, or "
            "$TRACKINIZER_SESSION_MAX_AGE_SECONDS)."
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Disable bearer/session auth; every request resolves to a "
            "synthetic admin identity. For ephemeral / local-only demos "
            "(example.sh). NEVER enable in production: anyone who can "
            "reach the port can edit everything."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_known_args(argv)


def _configure_logging(level: str | None) -> None:
    """Set the package log level from the flag or ``TRACKINIZER_LOG_LEVEL``."""
    raw = level or os.environ.get("TRACKINIZER_LOG_LEVEL")
    if not raw:
        return
    value = getattr(logging, raw.upper(), None)
    if not isinstance(value, int):
        raise SystemExit(f"invalid log level {raw!r}")
    logging.basicConfig(
        level=value,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(value)
