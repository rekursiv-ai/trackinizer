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
from typing import TYPE_CHECKING, override

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
    session_max_age_from_env,
)


if TYPE_CHECKING:
    from fastapi import FastAPI


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


APP_FACTORY_TARGET: str = f"{__name__}:build_app"
"""Import string uvicorn re-imports in each forked worker.

Multi-worker uvicorn spawns children that import the app themselves, so it
rejects a constructed app object -- it logs "You must pass the application as
an import string" and exits 3. Naming this module's factory is what lets
``--workers N`` fan out at all.
"""


def build_app() -> FastAPI:
    """Build a fully configured app from ``sys.argv``.

    The entry point for a forked uvicorn worker. A worker is a fresh
    interpreter that re-imports this module, so nothing the parent set up
    survives -- not ``app.state``, not the logging configuration, not the
    filters on the process-wide loggers. Every startup step is therefore
    re-derived here from the argv the child inherited, which is what keeps
    one source of truth with :func:`main`.
    """
    args, _log_level = _start_process()
    _configure_app(args)
    return app


def main(*, workers: int = 1) -> None:
    """Parse args, configure the app, and run uvicorn.

    NOTE: multi-worker is currently unsupported, see ``docs/private/workers.md``.
    """
    args, log_level = _start_process()
    # One worker runs the app in THIS process, so hand uvicorn the object it
    # is already holding. More than one forks children that re-import, which
    # only an import string can name -- passing the object there makes
    # uvicorn refuse the fan-out and exit 3.
    single_worker = workers <= 1
    target: FastAPI | str = APP_FACTORY_TARGET
    if single_worker:
        _configure_app(args)
        target = app
    uvicorn.run(
        target,
        factory=not single_worker,
        host=args.host,
        port=args.port,
        workers=workers,
        # Also sets the level of ``uvicorn.access``, which logs one INFO line
        # per request. Without this the flag configures only this package's
        # logger and uvicorn keeps its own INFO default, so an operator who
        # asked for WARNING still gets an access line per request -- behind a
        # proxy that is a duplicate of the proxy's log, with the proxy's IP
        # instead of the caller's, and it fills the log partition.
        log_level=log_level,
        timeout_keep_alive=args.timeout_keep_alive,
        # Force-close connections immediately on shutdown instead of waiting for
        # them to drain. Without this, uvicorn's default (wait indefinitely) made
        # SIGTERM hang on a held connection (e.g. an open Web UI tab) until the
        # 240s keep-alive window -- the "Waiting for connections to close" stall.
        # In-flight DB writes are atomic (a force-closed asyncpg/PGlite tx rolls
        # back), so there is nothing to drain for; 0 = don't wait, tear down now.
        timeout_graceful_shutdown=0,
    )


def _start_process() -> tuple[argparse.Namespace, int | None]:
    """Parse argv and apply every invariant a fresh server process needs.

    Called by BOTH entry points. ``main`` runs in the supervisor; ``build_app``
    runs in each forked worker, which is a fresh interpreter that inherited
    only argv. Anything written into just one of them applies to only half the
    deployment -- and the half that serves requests is the worker.

    Returns:
        args: The parsed arguments.
        log_level: Resolved numeric level to hand uvicorn, or None when
            neither the flag nor the environment named one.

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    args, remaining = _parse_args(parser)
    if remaining:
        parser.error(f"unrecognized arguments: {' '.join(remaining)}")
    log_level = _configure_logging(args.log_level)
    # Silence uvicorn's spurious zero-task cancel ERROR on every clean shutdown
    # (a consequence of timeout_graceful_shutdown=0); a real N>0 cancel still logs.
    # Guarded because the logger is process-wide: uvicorn's reloader and any
    # re-entry would otherwise stack a second identical filter.
    error_logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _SuppressZeroTaskCancel) for f in error_logger.filters):
        error_logger.addFilter(_SuppressZeroTaskCancel())
    return args, log_level


def _configure_app(args: argparse.Namespace) -> None:
    """Attach config (and the web UI) to the module-level app."""
    try:
        app.state.config = Config.from_args(args)
    except ConfigError as err:
        # Library code raises ConfigError (a plain Exception); the CLI is
        # the one place that turns a bad config into a clean process exit.
        raise SystemExit(str(err)) from err
    if args.web:
        web.attach(app, static_dir=args.static_dir)


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
            f"{data_dir() / 'rekursiv-ai' / 'trackinizer' / 'pgdata'})."
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
        # Reuse config's reader rather than a second `int(os.environ...)`: it
        # owns the default and rejects a non-integer or non-positive TTL with a
        # clean error. A bare int() here duplicated the default and turned an
        # operator typo into a raw ValueError traceback before --help rendered.
        default=_session_ttl_default(),
        type=_positive_session_ttl,
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


def _session_ttl_default() -> int:
    """Read the env TTL, turning a bad value into a clean process exit."""
    try:
        return session_max_age_from_env()
    except ConfigError as err:
        # Same translation _configure_app performs: library code raises
        # ConfigError, and the CLI is the one place that turns it into an exit.
        raise SystemExit(str(err)) from err


def _positive_session_ttl(value: str) -> int:
    """Parse a ``--session-max-age-seconds`` value; reject non-positive."""
    seconds = int(value)
    if seconds < 1:
        raise argparse.ArgumentTypeError(
            f"session TTL must be >= 1 second, got {seconds}"
        )
    return seconds


def _configure_logging(level: str | None) -> int | None:
    """Set the package log level from the flag or ``TRACKINIZER_LOG_LEVEL``.

    Args:
        level: Level name from ``--log-level``, or None to consult the
            environment.

    Returns:
        resolved: The numeric level the caller should also hand uvicorn, or
            None when neither the flag nor the environment named one.

    """
    raw = level or os.environ.get("TRACKINIZER_LOG_LEVEL")
    if not raw:
        return None
    value = getattr(logging, raw.upper(), None)
    if not isinstance(value, int):
        raise SystemExit(f"invalid log level {raw!r}")
    logging.basicConfig(
        level=value,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(value)
    return value
