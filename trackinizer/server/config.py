"""Runtime config + engine / embedder construction."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import argparse
import os
import shutil
import time
import uuid

from trackinizer.lib.postgres import DatabaseEngine, PGliteEngine, PostgresEngine
from trackinizer.lib.userdirs import data_dir
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.core import StubEmbedder
from trackinizer.types.embedder import Embedder


__all__ = [
    "Config",
    "ConfigError",
    "build_embedder",
    "build_engine",
    "default_datadir",
    "parse_engine",
]


class ConfigError(Exception):
    """A configuration value is invalid (bad engine, DSN, TTL, embedder).

    A plain :class:`Exception`, not :class:`SystemExit`: these helpers run
    inside the app lifespan, where a ``BaseException`` would slip past the
    normal ``except Exception`` handling and tear down the event loop
    abruptly. The CLI entrypoint catches this and translates it to a clean
    process exit.
    """


_DEFAULT_SESSION_MAX_AGE_SECONDS: int = 30 * 24 * 60 * 60


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Runtime config assembled from CLI flags or environment.

    Attributes:
      engine: ``pglite`` for local dev, ``pg`` against a real cluster.
      datadir: pglite data directory; ignored under ``pg``. ``None``
        resolves to :func:`default_datadir`.
      ephemeral: When true, pglite discards on shutdown.
      dsn: Postgres DSN when ``engine == 'pg'``.
      embedder: Embedder backend name.
      web: Mount the SPA when true.
      oauth_google_client_id: Google OAuth client id. ``None`` makes the
        OAuth routes 503; bearer auth is unaffected.
      oauth_google_client_secret: Google OAuth client secret; same
        503-on-missing behavior.
      oauth_redirect_uri: Public callback URL registered with Google.
        ``None`` disables OAuth.
      session_secret: HMAC key for the session and OAuth-state cookies.
        ``None`` disables session login. Must be stable across processes
        sharing the cookie -- rotating it logs everyone out.
      session_max_age_seconds: Session cookie TTL; defaults to 30 days.
      auth_disabled: Bypass auth -- every request becomes a synthetic
        admin. Local demos only; never in production.

    """

    engine: Literal["pglite", "pg"] = "pglite"
    datadir: Path | None = None
    ephemeral: bool = False
    pglite_tcp: bool = False
    """Open PGlite on a TCP port instead of its default Unix socket. Off by
    default (the Unix socket has no port to race). Opt in only when the DB must
    be reached over a port -- a non-co-located client or a TCP healthcheck."""
    dsn: str = ""
    embedder: str = "stub"
    web: bool = False
    oauth_google_client_id: str | None = None
    oauth_google_client_secret: str | None = None
    oauth_redirect_uri: str | None = None
    session_secret: str | None = None
    session_max_age_seconds: int = _DEFAULT_SESSION_MAX_AGE_SECONDS
    auth_disabled: bool = False

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            engine=parse_engine(os.environ.get("TRACKINIZER_ENGINE", "pglite")),
            datadir=Path(env_datadir)
            if (env_datadir := os.environ.get("TRACKINIZER_DATADIR"))
            else None,
            ephemeral=os.environ.get("TRACKINIZER_EPHEMERAL") == "1",
            pglite_tcp=os.environ.get("TRACKINIZER_PGLITE_TCP") == "1",
            dsn=os.environ.get("TRACKINIZER_DSN", ""),
            embedder=os.environ.get("TRACKINIZER_EMBEDDER", "stub"),
            web=os.environ.get("TRACKINIZER_WEB") == "1",
            oauth_google_client_id=os.environ.get("TRACKINIZER_GOOGLE_CLIENT_ID")
            or None,
            oauth_google_client_secret=os.environ.get(
                "TRACKINIZER_GOOGLE_CLIENT_SECRET"
            )
            or None,
            oauth_redirect_uri=os.environ.get("TRACKINIZER_OAUTH_REDIRECT_URI") or None,
            session_secret=os.environ.get("TRACKINIZER_SESSION_SECRET") or None,
            session_max_age_seconds=_session_max_age_from_env(),
            auth_disabled=os.environ.get("TRACKINIZER_NO_AUTH") == "1",
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Self:
        return cls(
            engine=parse_engine(args.engine),
            datadir=Path(args.datadir) if args.datadir else None,
            ephemeral=args.ephemeral,
            pglite_tcp=args.pglite_tcp,
            dsn=args.dsn,
            embedder=args.embedder,
            web=args.web,
            # OAuth secrets come from the environment only, never CLI flags.
            oauth_google_client_id=os.environ.get("TRACKINIZER_GOOGLE_CLIENT_ID")
            or None,
            oauth_google_client_secret=os.environ.get(
                "TRACKINIZER_GOOGLE_CLIENT_SECRET"
            )
            or None,
            oauth_redirect_uri=os.environ.get("TRACKINIZER_OAUTH_REDIRECT_URI") or None,
            session_secret=os.environ.get("TRACKINIZER_SESSION_SECRET") or None,
            session_max_age_seconds=args.session_max_age_seconds,
            auth_disabled=args.no_auth,
        )


def _session_max_age_from_env() -> int:
    """Read ``TRACKINIZER_SESSION_MAX_AGE_SECONDS``; reject a non-positive int.

    A missing or blank value falls back to the 30-day default. A value that
    isn't a positive integer is an operator typo, not a silent degrade, so
    it raises ``SystemExit`` rather than fall back -- a zero/negative TTL
    would expire every session instantly.
    """
    raw = os.environ.get("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_SESSION_MAX_AGE_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        raise ConfigError(
            f"TRACKINIZER_SESSION_MAX_AGE_SECONDS must be an integer, got {raw!r}"
        ) from None
    if seconds < 1:
        raise ConfigError(
            f"TRACKINIZER_SESSION_MAX_AGE_SECONDS must be >= 1, got {seconds}"
        )
    return seconds


def parse_engine(value: str) -> Literal["pglite", "pg"]:
    if value == "pglite":
        return "pglite"
    if value == "pg":
        return "pg"
    raise ConfigError(f"unknown engine {value!r}")


def default_datadir() -> Path:
    return data_dir("trackinizer") / "pgdata"


_EPHEMERAL_DIRNAME = "pgdata-ephemeral"
"""Parent dir for per-process ephemeral PGlite workdirs (see :func:`_ephemeral_workdir`)."""

_EPHEMERAL_STALE_SECONDS = 60 * 60
"""An ephemeral workdir older than this is assumed abandoned by a crashed server
and pruned on the next ephemeral boot. Comfortably exceeds any real boot so a
live sibling is never reclaimed; graceful shutdown removes a server's own dir via
the engine's ``__aexit__`` (``own_workdir=True``), and this only sweeps dirs a
hard crash (kill -9) left behind."""


def _ephemeral_root() -> Path:
    """Parent directory holding every server's per-process ephemeral workdir."""
    return data_dir("trackinizer") / _EPHEMERAL_DIRNAME


def _prune_stale_ephemeral_dirs(root: Path) -> None:
    """Remove ephemeral workdirs left behind by hard-killed servers.

    Each ephemeral server normally rmtree's its own dir on graceful shutdown via
    the engine's ``__aexit__`` (``own_workdir=True``; see :mod:`trackinizer.lib.postgres`
    -- ``atexit`` is NOT used, as it does not fire when uvicorn exits on a
    signal). A ``kill -9`` skips that, leaking the dir (and its PGlite
    ``dataDir``). A leaked dir is reclaimed only when its owning
    process is dead -- the owner pid is the dir-name prefix (``<pid>-<uuid>``),
    so ``os.kill(pid, 0)`` is the liveness check. Mtime is NOT a safe signal on
    its own: PGlite only bumps a dataDir's mtime on writes, so a read-heavy
    server alive past the stale window would be wrongly swept, corrupting a live
    peer's database. The stale window is a secondary guard against pid reuse:
    only a dir whose pid is dead AND that is older than the window is removed.
    Best effort.
    """
    with suppress(OSError):
        for child in root.iterdir():
            if not child.is_dir():
                continue
            with suppress(OSError):
                if _owner_dead(child.name) and (
                    time.time() - child.stat().st_mtime > _EPHEMERAL_STALE_SECONDS
                ):
                    shutil.rmtree(child, ignore_errors=True)


def _owner_dead(dir_name: str) -> bool:
    """Whether the ``<pid>-<uuid>`` workdir's owning process is gone.

    A dir whose name does not start with a parseable pid is treated as
    owner-unknown -> not dead (never reclaimed by liveness; the stale window
    still bounds truly-orphaned junk only when paired with this).
    """
    pid_str, _, _ = dir_name.partition("-")
    try:
        pid = int(pid_str)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive, owned by another user
    return False  # signal delivered -> process is alive


def _ephemeral_workdir() -> Path:
    """Allocate a unique, engine-owned PGlite workdir for one ephemeral server.

    Ephemeral servers must NOT share a workdir: PGlite rewrites
    ``pglite_manager.js`` and opens its ``dataDir`` in place, so two concurrent
    ``--ephemeral`` boots on the same dir corrupt each other's startup -- a
    sibling's ``pglite_manager.js`` rewrite or ``dataDir`` lock surfaces as
    ``PGlite process died during startup`` / ``No output`` on the loser. A unique
    per-process dir makes the collision impossible without any cross-process
    coordination. The engine removes it on graceful shutdown (``own_workdir=True``
    in :func:`build_engine`); this prunes any sibling a hard ``kill -9`` leaked.
    """
    root = _ephemeral_root()
    root.mkdir(parents=True, exist_ok=True)
    _prune_stale_ephemeral_dirs(root)
    return root / f"{os.getpid()}-{uuid.uuid4().hex}"


def build_engine(config: Config | None = None) -> DatabaseEngine:
    if config is None:
        config = Config.from_env()
    if config.engine == "pglite":
        # An explicit ``--datadir`` always wins (the operator picked it). Otherwise
        # an ephemeral server gets a unique, engine-owned scratch workdir so
        # concurrent ``--ephemeral`` boots never share one (which corrupts
        # startup); only a persistent server falls back to the single shared
        # ``default_datadir``.
        ephemeral_workdir = config.ephemeral and config.datadir is None
        if config.datadir is not None:
            workdir = config.datadir
        elif config.ephemeral:
            workdir = _ephemeral_workdir()
        else:
            workdir = default_datadir()
        return PGliteEngine(
            workdir=workdir,
            persist=not config.ephemeral,
            use_tcp=config.pglite_tcp,
            own_workdir=ephemeral_workdir,
        )
    if not config.dsn:
        raise ConfigError("--engine pg requires --dsn (or $TRACKINIZER_DSN).")
    return PostgresEngine(dsn=config.dsn, listen_channel=NOTIFY_CHANNEL)


def build_embedder(name: str) -> Embedder:
    if name == "stub":
        return StubEmbedder()
    raise ConfigError(f"unknown embedder {name!r}")
