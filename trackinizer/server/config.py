"""Runtime config + engine / embedder construction."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Self

import os
import shutil
import time
import uuid

from trackinizer.lib.postgres import DatabaseEngine, PGliteEngine, PostgresEngine
from trackinizer.lib.userdirs import data_dir
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL


if TYPE_CHECKING:
    from trackinizer.types.embedder import Embedder


__all__ = [
    "Assistant",
    "ChatOrgs",
    "Config",
    "ConfigError",
    "ConfigFlags",
    "build_embedder",
    "build_engine",
    "parse_assistant",
    "parse_chat_orgs",
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


_DEFAULT_SESSION_MAX_AGE_SECONDS: int = (
    30 * 24 * 60 * 60
)  # house-ignore[globals] -- Shared default; threading would duplicate across the Config field default and the env-parse fallback.


type ChatOrgs = Literal["single", "domain"]
"""Who a science chat's starter shares an organisation with: everyone (``single``), or
only those whose verified email has the starter's domain (``domain``)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Assistant:
    """An account whose live sessions every signed-in user may talk to.

    An actor handle is first come, first served, so a session is the assistant's
    only when the API key that opened it also belongs to ``email``.
    """

    actor: str
    email: str


class ConfigFlags(Protocol):
    """The parsed CLI flags :meth:`Config.from_args` reads."""

    engine: str
    datadir: Path | None
    ephemeral: bool
    pglite_tcp: bool
    dsn: str
    embedder: str
    session_embedder: str
    session_embedder_dim: int | None
    session_embedders: str
    web: bool
    session_max_age_seconds: int
    auth: bool
    assistant: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Runtime config assembled from CLI flags or environment.

    Attributes:
      engine: ``pglite`` for local dev, ``pg`` against a real cluster.
      datadir: pglite data directory; ignored under ``pg``. ``None``
        resolves to ``data_dir() / "rekursiv-ai" / "trackinizer" / "pgdata"``.
      ephemeral: When true, pglite discards on shutdown.
      pglite_tcp: Open PGlite on a TCP port instead of its default Unix socket.
        Off by default (the Unix socket has no port to race). Opt in only when
        the DB must be reached over a port -- a non-co-located client or a TCP
        healthcheck.
      dsn: Postgres DSN when ``engine == 'pg'``.
      embedder: Embedder backend name (inquiry_embeddings, 384-dim).
      session_embedder: Embedder backend name for session_embeddings
        semantic search (1024-dim); empty disables the semantic arm.
      web: Mount the web routes: ``/api/web``, the login page and, with
        ``--app-dir``, the web app.
      session_secret: HMAC key for the session and OAuth-state cookies.
        Empty disables session login. Must be stable across processes
        sharing the cookie -- rotating it logs everyone out.
      secrets: Secret-variable backend: ``file`` (under the user data
        directory), ``file:/abs/path``, or ``none`` to refuse secret
        variables. Environment only (``TRACKINIZER_SECRETS``).
      session_max_age_seconds: Session cookie TTL; defaults to 30 days.
      auth_disabled: Bypass auth -- every request becomes a synthetic
        admin. Local demos only; never in production.
      assistant: The Chat partner of every canvas; none when unset.
      chat_orgs: ``domain`` (the default) gives each verified email domain its own
        organisation, consumer domains none: a user outside the starter's forks the
        chat instead. ``single`` makes every user one organisation, so a science chat
        is always continued by typing in it; only a server whose users are all one
        organisation sets it. Environment only (``TRACKINIZER_CHAT_ORGS``).

    """

    engine: Literal["pglite", "pg"] = "pglite"
    datadir: Path | None = None
    ephemeral: bool = False
    pglite_tcp: bool = False
    dsn: str = ""
    embedder: str = "stub"
    # The session-search embedder used to embed QUERIES (the serving default),
    # separate from ``embedder`` (inquiry_embeddings' 384-dim). Its value is the
    # embedder's full stored identity (``qwen3-embedding-4b@1024``, not a bare
    # ``qwen3-embedding-4b``) -- one canonical spelling that is the config knob,
    # the registry key, ``session_embeddings.model``, and the partial-index
    # WHERE, all at once. Empty degrades the route to full-text only rather than
    # loading a model on a keyword search. See ``embedders.registry``.
    session_embedder: str = ""
    # An optional output-dim override for the serving embedder, applied when it is
    # a bare slug (a dim that isn't a choice needn't be spelled). ``None`` uses the
    # model's registered default. A Matryoshka model accepts any dim in its range;
    # the registry validates and mints the ``slug@dim`` identity. Redundant when
    # ``session_embedder`` already carries an ``@dim`` suffix (they must agree).
    # Env: ``TRACKINIZER_SESSION_EMBEDDER_DIM``.
    session_embedder_dim: int | None = None
    # The set of embedders the sweep/backfill MAINTAINS (writes rows for), as
    # opposed to the single one that serves queries. Several models coexist in
    # ``session_embeddings`` (keyed by model), so the corpus can carry vectors
    # for a challenger while ``session_embedder`` still serves the incumbent.
    # Defaults to just the serving embedder; set a superset to backfill A/B
    # candidates. Env: comma-separated ``TRACKINIZER_SESSION_EMBEDDERS``.
    session_embedders: tuple[str, ...] = ()
    web: bool = False
    session_secret: str = ""
    secrets: str = "file"
    session_max_age_seconds: int = _DEFAULT_SESSION_MAX_AGE_SECONDS
    auth_disabled: bool = False
    assistant: Assistant | None = None
    chat_orgs: ChatOrgs = "domain"

    @classmethod
    def from_env(cls) -> Self:
        """Build from environment variables.

        Returns:
          result: Config with settings from TRACKINIZER_* env vars.

        """
        return cls(
            engine=parse_engine(os.environ.get("TRACKINIZER_ENGINE", "pglite")),
            datadir=Path(env_datadir)
            if (env_datadir := os.environ.get("TRACKINIZER_DATADIR", ""))
            else None,
            ephemeral=os.environ.get("TRACKINIZER_EPHEMERAL") == "1",
            pglite_tcp=os.environ.get("TRACKINIZER_PGLITE_TCP") == "1",
            dsn=os.environ.get("TRACKINIZER_DSN", ""),
            embedder=os.environ.get("TRACKINIZER_EMBEDDER", "stub"),
            session_embedder=os.environ.get("TRACKINIZER_SESSION_EMBEDDER", ""),
            session_embedder_dim=_parse_optional_dim(
                os.environ.get("TRACKINIZER_SESSION_EMBEDDER_DIM", ""),
            ),
            session_embedders=_parse_session_embedders(
                os.environ.get("TRACKINIZER_SESSION_EMBEDDERS", ""),
            ),
            web=os.environ.get("TRACKINIZER_WEB") == "1",
            session_secret=os.environ.get("TRACKINIZER_SESSION_SECRET", ""),
            secrets=secrets_from_env(),
            session_max_age_seconds=session_max_age_from_env(),
            auth_disabled=auth_disabled_from_env(),
            assistant=parse_assistant(os.environ.get("TRACKINIZER_ASSISTANT", "")),
            chat_orgs=parse_chat_orgs(os.environ.get("TRACKINIZER_CHAT_ORGS", "")),
        )

    @classmethod
    def from_args(cls, flags: ConfigFlags) -> Self:
        """Build from parsed CLI flags.

        Args:
          flags: Parsed arguments with engine, datadir, ephemeral, etc. fields.

        Returns:
          result: Config with settings from flags; the session secret from the
            environment only.

        """
        return cls(
            engine=parse_engine(flags.engine),
            datadir=flags.datadir,
            ephemeral=flags.ephemeral,
            pglite_tcp=flags.pglite_tcp,
            dsn=flags.dsn,
            embedder=flags.embedder,
            session_embedder=flags.session_embedder,
            session_embedder_dim=flags.session_embedder_dim,
            session_embedders=_parse_session_embedders(flags.session_embedders),
            web=flags.web,
            # Secrets come from the environment only, never CLI flags.
            session_secret=os.environ.get("TRACKINIZER_SESSION_SECRET", ""),
            secrets=secrets_from_env(),
            session_max_age_seconds=flags.session_max_age_seconds,
            auth_disabled=not flags.auth,
            assistant=parse_assistant(flags.assistant),
            # Like the secrets, a deployment property that no flag overrides.
            chat_orgs=parse_chat_orgs(os.environ.get("TRACKINIZER_CHAT_ORGS", "")),
        )

    def maintained_embedders(self) -> tuple[str, ...]:
        """Return the embedders the sweep/backfill should maintain.

        An explicit :attr:`session_embedders` wins; otherwise the serving
        :attr:`session_embedder` is the sole maintained model (and an unset
        serving embedder maintains nothing).

        Returns:
          names: The stored embedder identities whose rows the sweep keeps.

        """
        if self.session_embedders:
            return self.session_embedders
        return (self.session_embedder,) if self.session_embedder else ()


def session_max_age_from_env() -> int:
    """Read ``TRACKINIZER_SESSION_MAX_AGE_SECONDS``; reject a non-positive int.

    A missing or blank value falls back to the 30-day default. A value that
    isn't a positive integer is an operator typo, not a silent degrade, so
    it raises rather than fall back -- a zero/negative TTL would expire every
    session instantly.

    Returns:
      result: Session TTL in seconds; always > 0 or raises ConfigError.

    Raises:
        ConfigError: The value is set but is not a positive integer. Callers
            in a CLI translate this to a clean process exit.

    """
    raw = os.environ.get("TRACKINIZER_SESSION_MAX_AGE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_SESSION_MAX_AGE_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        raise ConfigError(
            f"TRACKINIZER_SESSION_MAX_AGE_SECONDS must be an integer, got {raw!r}",
        ) from None
    if seconds < 1:
        raise ConfigError(
            f"TRACKINIZER_SESSION_MAX_AGE_SECONDS must be >= 1, got {seconds}",
        )
    return seconds


def secrets_from_env() -> str:
    """Read ``TRACKINIZER_SECRETS``; unset keeps the :class:`Config` default."""
    return os.environ.get("TRACKINIZER_SECRETS", Config().secrets)


def auth_disabled_from_env() -> bool:
    """Whether ``TRACKINIZER_NO_AUTH=1`` asks for single-user local mode."""
    return os.environ.get("TRACKINIZER_NO_AUTH") == "1"


def parse_assistant(value: str) -> Assistant | None:
    """Parse ``--assistant`` / ``$TRACKINIZER_ASSISTANT``, ``ACTOR=EMAIL``.

    Args:
      value: The flag or variable; blank means no assistant.

    Returns:
      assistant: The assistant, or None when blank.

    Raises:
      ConfigError: The value is not ``ACTOR=EMAIL``.

    """
    if not value.strip():
        return None
    actor, _, email = value.partition("=")
    if not actor.strip() or not email.strip():
        raise ConfigError(f"--assistant must be ACTOR=EMAIL, got {value!r}")
    # users.email is stored lowercase, so only a lowercase one can match it.
    return Assistant(actor=actor.strip(), email=email.strip().lower())


def parse_chat_orgs(value: str) -> ChatOrgs:
    """Parse ``$TRACKINIZER_CHAT_ORGS``: ``single`` or ``domain``; blank is ``domain``.

    Blank is the safe value: a server that never set the variable must not let a
    person from another organisation type into a chat that is not theirs.

    Args:
      value: The variable.

    Returns:
      orgs: How science chats group their users.

    Raises:
      ConfigError: The value is neither.

    """
    match value.strip():
        case "" | "domain":
            return "domain"
        case "single":
            return "single"
        case other:
            raise ConfigError(
                f"TRACKINIZER_CHAT_ORGS must be single or domain, got {other!r}",
            )


def parse_engine(value: str) -> Literal["pglite", "pg"]:
    """Parse the engine name."""
    if value == "pglite":
        return "pglite"
    if value == "pg":
        return "pg"
    raise ConfigError(f"unknown engine {value!r}")


def build_engine(config: Config | None = None) -> DatabaseEngine:
    """Build the database engine.

    Args:
      config: Config object; if None, loads from environment via Config.from_env().

    Returns:
      result: SQLAlchemy engine configured per the Config settings.

    """
    if config is None:
        config = Config.from_env()
    if config.engine == "pglite":
        # An explicit ``--datadir`` always wins (the operator picked it). Otherwise
        # an ephemeral server gets a unique, engine-owned scratch workdir so
        # concurrent ``--ephemeral`` boots never share one (which corrupts
        # startup); only a persistent server falls back to the single shared
        # persistent datadir.
        ephemeral_workdir = config.ephemeral and config.datadir is None
        if config.datadir is not None:
            workdir = config.datadir
        elif config.ephemeral:
            workdir = _ephemeral_workdir()
        else:
            workdir = data_dir() / "rekursiv-ai" / "trackinizer" / "pgdata"
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
    """Build the embedder backend."""
    if name == "stub":
        return StubEmbedder()
    raise ConfigError(f"unknown embedder {name!r}")


def _parse_session_embedders(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated embedder list, trimming blanks."""
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def _parse_optional_dim(raw: str) -> int | None:
    """Parse an optional integer dim env value; empty/blank is ``None``."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        raise ConfigError(
            f"TRACKINIZER_SESSION_EMBEDDER_DIM must be an integer, got {raw!r}",
        ) from None


# ``stale_seconds``: an ephemeral workdir older than this is assumed abandoned by a
# crashed server and pruned on the next ephemeral boot. Comfortably exceeds any real
# boot so a live sibling is never reclaimed; graceful shutdown removes a server's own
# dir via the engine's ``__aexit__`` (``own_workdir=True``), and this only sweeps dirs a
# hard crash (kill -9) left behind.
#
# Each ephemeral server normally rmtree's its own dir on graceful shutdown via the
# engine's ``__aexit__`` (``own_workdir=True``; see :mod:`trackinizer.lib.postgres` --
# ``atexit`` is NOT used, as it does not fire when uvicorn exits on a signal). A ``kill
# -9`` skips that, leaking the dir (and its PGlite ``dataDir``). A leaked dir is
# reclaimed only when its owning process is dead -- the owner pid is the dir-name prefix
# (``<pid>-<uuid>``), so ``os.kill(pid, 0)`` is the liveness check. Mtime is NOT a safe
# signal on its own: PGlite only bumps a dataDir's mtime on writes, so a read-heavy
# server alive past the stale window would be wrongly swept, corrupting a live peer's
# database. The stale window is a secondary guard against pid reuse: only a dir whose
# pid is dead AND that is older than the window is removed. Best effort.
def _prune_stale_ephemeral_dirs(root: Path, *, stale_seconds: int = 60 * 60) -> None:
    """Remove ephemeral workdirs left behind by hard-killed servers."""
    with suppress(OSError):
        for child in root.iterdir():
            if not child.is_dir():
                continue
            with suppress(OSError):
                if _owner_dead(child.name) and (
                    time.time() - child.stat().st_mtime > stale_seconds
                ):
                    shutil.rmtree(child, ignore_errors=True)


# A dir whose name does not start with a parseable pid is treated as owner-unknown ->
# not dead (never reclaimed by liveness; the stale window still bounds truly-orphaned
# junk only when paired with this).
def _owner_dead(dir_name: str) -> bool:
    """Whether the ``<pid>-<uuid>`` workdir's owning process is gone."""
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
        return False  # Alive, owned by another user.
    return False  # Signal delivered -> process is alive.


# Ephemeral servers must NOT share a workdir: PGlite rewrites ``pglite_manager.js`` and
# opens its ``dataDir`` in place, so two concurrent ``--ephemeral`` boots on the same
# dir corrupt each other's startup -- a sibling's ``pglite_manager.js`` rewrite or
# ``dataDir`` lock surfaces as ``PGlite process died during startup`` / ``No output`` on
# the loser. A unique per-process dir makes the collision impossible without any cross-
# process coordination. The engine removes it on graceful shutdown (``own_workdir=True``
# in :func:`build_engine`); this prunes any sibling a hard ``kill -9`` leaked.
def _ephemeral_workdir() -> Path:
    """Allocate a unique, engine-owned PGlite workdir for one ephemeral server."""
    root = data_dir() / "rekursiv-ai" / "trackinizer" / "pgdata-ephemeral"
    root.mkdir(parents=True, exist_ok=True)
    _prune_stale_ephemeral_dirs(root)
    return root / f"{os.getpid()}-{uuid.uuid4().hex}"
