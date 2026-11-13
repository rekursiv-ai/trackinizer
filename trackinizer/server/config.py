"""Runtime config + engine / embedder construction."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Self

import os
import re
import secrets
import shutil
import tempfile
import time
import uuid

from trackinizer.lib.userdirs import data_dir
from trackinizer.server.embedders.stub import StubEmbedder


# Registry only needs ConfigError; eager engines/wire models cost ~200 ms.
if TYPE_CHECKING:
    from treekle import json_loads

    from trackinizer.lib.postgres import substrate
    from trackinizer.lib.postgres.substrate import DatabaseEngine
    from trackinizer.server import notify
    from trackinizer.types.embedder import Embedder
    from trackinizer.wire import wire_service_accounts, wire_terminals
else:
    from wrapt import lazy_import

    substrate = lazy_import("trackinizer.lib.postgres.substrate")
    notify = lazy_import("trackinizer.server.notify")
    wire_service_accounts = lazy_import("trackinizer.wire.wire_service_accounts")
    wire_terminals = lazy_import("trackinizer.wire.wire_terminals")
    json_loads = lazy_import("treekle", "json_loads")


__all__ = [
    "Assistant",
    "ChatOrgs",
    "ComputeNote",
    "Config",
    "ConfigError",
    "ConfigFlags",
    "RemoteAgentService",
    "ServiceKeys",
    "build_embedder",
    "build_engine",
    "parse_assistant",
    "parse_chat_orgs",
    "parse_compute",
    "parse_default_remote_agent_service",
    "parse_engine",
    "parse_remote_agent_services",
    "parse_service_keys",
    "parse_staff",
    "parse_terminal_stun",
    "session_secret_from_env",
    "session_secret_from_file",
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


type ChatOrgs = Literal["single", "domain", "individual"]
"""What an organisation is on this server, as a chat canvas has it: everyone
(``single``), the people whose verified email has one domain (``domain``), or each
person alone (``individual``). A person reads the open sessions and science chats of
their organisation besides their own and the chats shared with them."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Assistant:
    """An account whose live sessions every signed-in user may talk to.

    An actor handle is first come, first served, so a session is the assistant's
    only when the API key that opened it also belongs to ``email``. Every key of that
    account acts as the assistant, so it is a service account (:func:`parse_assistant`
    refuses any other).
    """

    actor: str
    email: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeNote:
    """One thing this server provides to run commands on, as its deployment says.

    Attributes:
      name: What it is called, a few words.
      about: A sentence or two on what it is and how a person gets to use it.

    """

    name: str
    about: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceKeys:
    """The service accounts a deployment names, and where their keys are kept.

    Attributes:
      accounts: Each account's name and role, in the order named.
      directory: Where each account's key lives, in a file named for it.

    """

    accounts: tuple[tuple[str, wire_service_accounts.ServiceRole], ...]
    directory: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoteAgentService:
    """A service that runs remote agents: agents for accounts other than its own.

    It is enrolled as a host, whose daemon confines each agent it starts.

    Attributes:
      name: The name it is enrolled under, as a host's.
      account: The service account it is enrolled under, which owns it.
      admits: Who may start an agent on it or use its login, as patterns of the kind
        ``staff`` holds.

    """

    name: str
    account: str
    admits: tuple[str, ...] = ()


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
    staff: str


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
        sharing the cookie -- rotating it logs everyone out. Environment only:
        ``TRACKINIZER_SESSION_SECRET``, or the file
        ``TRACKINIZER_SESSION_SECRET_FILE`` names, which the first start makes.
      secrets: Secret-variable backend: ``file`` (under the user data
        directory), ``file:/abs/path``, or ``none`` to refuse secret
        variables. Environment only (``TRACKINIZER_SECRETS``).
      session_max_age_seconds: Session cookie TTL; defaults to 30 days.
      auth_disabled: Bypass auth -- every request becomes a synthetic
        admin. Local demos only; never in production.
      assistant: The service account that acts for an ``asker``; none when unset.
      staff: Who holds the ``staff`` audience: patterns, each ``user@host`` or
        ``*@domain``, lowercase. A domain pattern also needs Google's ``hd`` claim
        to name the domain. Empty means nobody is staff and no staff session can
        be opened. Deployment property: ``--staff`` or ``TRACKINIZER_STAFF``.
      chat_orgs: The organisation mode: who reads an ``open`` session and an open
        science chat besides its owner, a chat's starter and the people they shared
        it with, and, for a session that is not a chat, the people who hold
        ``staff`` or are admins. ``individual`` (the default) makes each person an
        organisation of one, so nobody else reads what they do not share.
        ``domain`` gives each email domain Google names a person a member of its
        own organisation, and service accounts none, so a session or chat is read
        by its organisation. ``single`` makes every user one organisation, so
        every caller reads every open session and every person every open chat;
        only a server whose users are all one organisation sets it. Environment
        only (``TRACKINIZER_CHAT_ORGS``).
      terminal_stun: STUN servers the browser and a daemon use to reach each
        other direct, as ``stun:HOST[:PORT]`` URLs. Empty (the default) means a
        direct link forms on one network only. Environment only
        (``TRACKINIZER_TERMINAL_STUN``, comma separated).
      compute: What this server provides to run commands on, which the web app's
        Hosts menu shows beside the person's own hosts. Empty (the default)
        shows none. Environment only (``TRACKINIZER_COMPUTE``, a JSON list of
        ``{"name", "about"}`` objects).
      service_keys: The service accounts this server keeps one live key each for,
        at every start, and where the key files live; none when unset.
        Environment only (``TRACKINIZER_SERVICE_KEYS``, comma separated
        ``NAME:ROLE`` pairs, and ``TRACKINIZER_SERVICE_KEY_DIR``).
      tool_connections_per_host: The most tool connections one host holds at once; the
        next agent is refused.
      tool_dial_back_sec: How long an offered tool connection waits for its host to dial
        back before it ends.
      tool_sweep_sec: How often each tool connection's rule is asked again, so a revoked
        key or a demoted asker loses the host within it.
      tool_line_bytes: The longest line the server reads from an agent's tool connection;
        a longer one ends the tool connection. The default fits a ``Write`` of 8 MiB. Each line
        is parsed inline, on the one process, so a line this long stalls it for a
        moment; only a writer's or an admin's key can send one.
      remote_agent_services: The services that run agents for accounts other than
        their own, and whom each admits. Environment only
        (``TRACKINIZER_REMOTE_AGENT_SERVICES``, a JSON list of
        ``{"name", "account", "admits"}`` objects).
      default_remote_agent_service: The remote agent service an agent runs on when
        its start names no host; empty when this server names none, and such a
        start finds no place. Environment only
        (``TRACKINIZER_DEFAULT_REMOTE_AGENT_SERVICE``).

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
    staff: tuple[str, ...] = ()
    chat_orgs: ChatOrgs = "individual"
    terminal_stun: tuple[str, ...] = ()
    compute: tuple[ComputeNote, ...] = ()
    service_keys: ServiceKeys | None = None
    tool_connections_per_host: int = 16
    tool_dial_back_sec: float = 20.0
    tool_sweep_sec: float = 60.0
    tool_line_bytes: int = 16 << 20
    remote_agent_services: tuple[RemoteAgentService, ...] = ()
    default_remote_agent_service: str = ""

    @classmethod
    def from_env(cls) -> Self:
        """Build from environment variables.

        Returns:
          result: Config with settings from TRACKINIZER_* env vars.

        """
        remote_agent_services = parse_remote_agent_services(
            os.environ.get("TRACKINIZER_REMOTE_AGENT_SERVICES", ""),
        )
        return cls(
            engine=parse_engine(os.environ.get("TRACKINIZER_ENGINE", "pglite")),
            datadir=Path(env_datadir)
            if (
                env_datadir := os.environ.get(
                    "TRACKINIZER_DATADIR",
                    "",  # pragma: no mutate[truthiness]
                )
            )
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
            session_secret=session_secret_from_env(),
            secrets=secrets_from_env(),
            session_max_age_seconds=session_max_age_from_env(),
            auth_disabled=auth_disabled_from_env(),
            assistant=parse_assistant(os.environ.get("TRACKINIZER_ASSISTANT", "")),
            staff=parse_staff(os.environ.get("TRACKINIZER_STAFF", "")),
            chat_orgs=parse_chat_orgs(os.environ.get("TRACKINIZER_CHAT_ORGS", "")),
            terminal_stun=parse_terminal_stun(
                os.environ.get("TRACKINIZER_TERMINAL_STUN", ""),
            ),
            compute=parse_compute(os.environ.get("TRACKINIZER_COMPUTE", "")),
            service_keys=parse_service_keys(
                os.environ.get("TRACKINIZER_SERVICE_KEYS", ""),
                os.environ.get("TRACKINIZER_SERVICE_KEY_DIR", ""),
            ),
            remote_agent_services=remote_agent_services,
            default_remote_agent_service=parse_default_remote_agent_service(
                os.environ.get("TRACKINIZER_DEFAULT_REMOTE_AGENT_SERVICE", ""),
                services=remote_agent_services,
            ),
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
        remote_agent_services = parse_remote_agent_services(
            os.environ.get("TRACKINIZER_REMOTE_AGENT_SERVICES", ""),
        )
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
            session_secret=session_secret_from_env(),
            secrets=secrets_from_env(),
            session_max_age_seconds=flags.session_max_age_seconds,
            auth_disabled=not flags.auth,
            assistant=parse_assistant(flags.assistant),
            staff=parse_staff(flags.staff),
            # Like the secrets, deployment properties that no flag overrides.
            chat_orgs=parse_chat_orgs(os.environ.get("TRACKINIZER_CHAT_ORGS", "")),
            terminal_stun=parse_terminal_stun(
                os.environ.get("TRACKINIZER_TERMINAL_STUN", ""),
            ),
            compute=parse_compute(os.environ.get("TRACKINIZER_COMPUTE", "")),
            service_keys=parse_service_keys(
                os.environ.get("TRACKINIZER_SERVICE_KEYS", ""),
                os.environ.get("TRACKINIZER_SERVICE_KEY_DIR", ""),
            ),
            remote_agent_services=remote_agent_services,
            default_remote_agent_service=parse_default_remote_agent_service(
                os.environ.get("TRACKINIZER_DEFAULT_REMOTE_AGENT_SERVICE", ""),
                services=remote_agent_services,
            ),
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


def session_secret_from_env() -> str:
    """Read the session secret from its variable, or from the file another names.

    ``TRACKINIZER_SESSION_SECRET`` holds the secret itself, and
    ``TRACKINIZER_SESSION_SECRET_FILE`` names a file :func:`session_secret_from_file`
    keeps it in.

    Returns:
      secret: The session secret; empty when neither is set.

    Raises:
      ConfigError: Both are set.

    """
    secret = os.environ.get(
        "TRACKINIZER_SESSION_SECRET",
        "",  # pragma: no mutate[truthiness]
    )
    path = os.environ.get("TRACKINIZER_SESSION_SECRET_FILE", "")
    if secret and path.strip():
        raise ConfigError(
            "set TRACKINIZER_SESSION_SECRET or TRACKINIZER_SESSION_SECRET_FILE, not both",
        )
    return secret or session_secret_from_file(path)


# A box's server makes its own: the first start writes a random key, and each later one, a
# restart or a deploy, reads it, so sessions outlive both. The key is staged beside the file
# and linked into place, which fails when another start linked first, so two starts at once
# keep one key.
def session_secret_from_file(path: str) -> str:
    """Return the session secret kept at ``path``, made at the first start.

    Args:
      path: The file; blank names none.

    Returns:
      secret: The key in the file, or empty when no file is named.

    """
    if not path.strip():
        return ""
    target = Path(path.strip())
    if not target.exists():
        # Staged under a name no other start shares, as write_private stages, then linked
        # into place, so two first starts keep one secret and a crash leaves no name taken.
        fd, staged = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        secret = secrets.token_hex(32)  # pragma: no mutate -- None is 32 bytes too.
        try:
            with os.fdopen(fd, "w") as staged_file:
                staged_file.write(secret + "\n")
            with suppress(FileExistsError):
                os.link(staged, target)
        finally:
            Path(staged).unlink()
    return target.read_text(
        encoding="utf-8",  # pragma: no mutate[case-insensitive]
    ).strip()


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
      ConfigError: The value is not ``ACTOR=EMAIL``, or ``EMAIL`` is not a service
        account's.

    """
    if not value.strip():
        return None
    actor, _, email = value.partition("=")
    if not actor.strip() or not email.strip():
        raise ConfigError(f"--assistant must be ACTOR=EMAIL, got {value!r}")
    # A person's address would make every key of theirs the assistant: their own runs
    # would be refused their chats, and their keys would act for any asker.
    if not wire_service_accounts.is_service_address(email):
        raise ConfigError(
            "--assistant (TRACKINIZER_ASSISTANT) must name a service account, "
            f"NAME@{wire_service_accounts.SERVICE_DOMAIN}, got {email.strip()!r}",
        )
    # users.email is stored lowercase, so only a lowercase one can match it.
    return Assistant(actor=actor.strip(), email=email.strip().lower())


def parse_staff(value: str) -> tuple[str, ...]:
    """Parse ``--staff`` / ``$TRACKINIZER_STAFF``: comma-separated staff patterns.

    Each pattern is ``user@host`` or ``*@domain``, the allowlist's grammar, so a bare
    word or a lone ``*`` is a typo that would make everyone staff or no one.

    Args:
      value: The flag or variable; blank means nobody is staff.

    Returns:
      patterns: The patterns, lowercase and without repeats, in the order given.

    Raises:
      ConfigError: A pattern is not ``user@host`` or ``*@domain``.

    """
    patterns: dict[str, None] = {}
    for raw in value.split(","):
        pattern = raw.strip().lower()
        if not pattern:
            continue
        if re.fullmatch(r"(?:[^@\s*]+|\*)@[^@\s*]+", pattern) is None:
            raise ConfigError(
                f"--staff takes user@host or *@domain patterns, got {raw.strip()!r}",
            )
        patterns[pattern] = None
    return tuple(patterns)


def parse_chat_orgs(value: str) -> ChatOrgs:
    """Parse ``$TRACKINIZER_CHAT_ORGS``: ``single``, ``domain`` or ``individual``.

    Blank is ``individual``, the mode that reads least: a server that never set the
    variable lets no one read a session or a chat that is neither theirs nor shared
    with them.

    Args:
      value: The variable.

    Returns:
      orgs: How science chats group their users.

    Raises:
      ConfigError: The value is neither.

    """
    match value.strip():
        case "domain":
            return "domain"
        case "single":
            return "single"
        case "" | "individual":
            return "individual"
        case other:
            raise ConfigError(
                "TRACKINIZER_CHAT_ORGS must be single, domain or individual, "
                f"got {other!r}",
            )


def parse_terminal_stun(value: str) -> tuple[str, ...]:
    """Parse ``$TRACKINIZER_TERMINAL_STUN``: up to four ``stun:HOST[:PORT]`` URLs.

    Args:
      value: The variable, comma separated; blank means none.

    Returns:
      servers: The URLs, in order.

    Raises:
      ConfigError: An entry is not a ``stun:`` URL, or there are too many.

    """
    servers = tuple(each.strip() for each in value.split(",") if each.strip())
    if len(servers) > wire_terminals.MAX_ICE_SERVERS or any(
        re.fullmatch(r"stun:\S{1,200}", each) is None for each in servers
    ):
        raise ConfigError(
            "TRACKINIZER_TERMINAL_STUN takes at most "
            f"{wire_terminals.MAX_ICE_SERVERS} comma separated stun:HOST[:PORT] URLs, "
            f"got {value!r}",
        )
    return servers


def parse_compute(value: str) -> tuple[ComputeNote, ...]:
    """Parse ``$TRACKINIZER_COMPUTE``: a JSON list of ``{"name", "about"}`` objects.

    Args:
      value: The variable; blank means none.

    Returns:
      notes: The notes, in order, each field trimmed.

    Raises:
      ConfigError: The value is not such a list, or a name or an about is blank.

    """
    if not value.strip():
        return ()
    shape = 'TRACKINIZER_COMPUTE must be a JSON list of {"name", "about"} objects'
    try:
        read = json_loads(value, tuple[ComputeNote, ...])
    except ValueError as error:
        raise ConfigError(f"{shape}: {error}") from None
    notes = tuple(
        ComputeNote(name=each.name.strip(), about=each.about.strip()) for each in read
    )
    if blank := [
        index for index, each in enumerate(notes) if "" in (each.name, each.about)
    ]:
        raise ConfigError(f"{shape}, each with text in both: entry {blank[0]} has none")
    return notes


def parse_remote_agent_services(value: str) -> tuple[RemoteAgentService, ...]:
    """Parse ``$TRACKINIZER_REMOTE_AGENT_SERVICES``: a JSON list of services.

    Each is ``{"name", "account", "admits"}``, and is enrolled under a service
    account, since a person's host that took other accounts' sessions would hand them
    its owner. ``admits`` lists patterns of the kind ``$TRACKINIZER_STAFF`` holds,
    read by :func:`parse_staff`.

    Args:
      value: The variable; blank means none.

    Returns:
      services: The services, in order, each name trimmed and each account
        lowercase.

    Raises:
      ConfigError: The value is not such a list, a name or an account is blank or
        names no service account, a name is given twice, or a pattern is malformed.

    """
    if not value.strip():
        return ()
    shape = (
        "TRACKINIZER_REMOTE_AGENT_SERVICES must be a JSON list of "
        '{"name", "account", "admits"} objects'
    )
    try:
        read = json_loads(value, tuple[RemoteAgentService, ...])
    except ValueError as error:
        raise ConfigError(f"{shape}: {error}") from None
    services = tuple(
        RemoteAgentService(
            name=each.name.strip(),
            account=each.account.strip().lower(),
            admits=parse_staff(",".join(each.admits)),
        )
        for each in read
    )
    for each in services:
        if not each.name or not wire_service_accounts.is_service_address(each.account):
            raise ConfigError(
                f"{shape}, each named and enrolled under a service account, "
                f"NAME@{wire_service_accounts.SERVICE_DOMAIN}: got {each!r}",
            )
    if len({each.name for each in services}) != len(services):
        raise ConfigError("TRACKINIZER_REMOTE_AGENT_SERVICES names a service twice")
    return services


def parse_default_remote_agent_service(
    value: str,
    *,
    services: tuple[RemoteAgentService, ...],
) -> str:
    """Parse ``$TRACKINIZER_DEFAULT_REMOTE_AGENT_SERVICE``: where an agent runs by default.

    Args:
      value: The variable; blank means none.
      services: The remote agent services the server names.

    Returns:
      name: The service's name, trimmed; empty for none.

    Raises:
      ConfigError: It names no remote agent service.

    """
    name = value.strip()
    if name and name not in {each.name for each in services}:
        raise ConfigError(
            f"TRACKINIZER_DEFAULT_REMOTE_AGENT_SERVICE names {name!r}, which "
            "TRACKINIZER_REMOTE_AGENT_SERVICES does not name",
        )
    return name


def parse_service_keys(accounts: str, directory: str) -> ServiceKeys | None:
    """Parse ``$TRACKINIZER_SERVICE_KEYS`` and ``$TRACKINIZER_SERVICE_KEY_DIR``.

    Args:
      accounts: ``NAME:ROLE`` pairs, comma separated; blank names none.
      directory: Where the key files live; needed once an account is named.

    Returns:
      service_keys: The accounts and their directory, or None when none is named.

    Raises:
      ConfigError: A pair is malformed, its role is not ``viewer`` or ``writer``, a
        name is given twice, or accounts are named with no directory.

    """
    named: dict[str, wire_service_accounts.ServiceRole] = {}
    for pair in filter(None, (part.strip() for part in accounts.split(","))):
        # pragma: no mutate start -- a name holds no colon, so rpartition refuses alike.
        name, _, role = pair.partition(":")
        # pragma: no mutate end
        if not re.fullmatch(wire_service_accounts.NAME_PATTERN, name) or role not in (
            "viewer",
            "writer",
        ):
            raise ConfigError(
                f"TRACKINIZER_SERVICE_KEYS: {pair!r} is not NAME:viewer or NAME:writer",
            )
        if name in named:
            raise ConfigError(f"TRACKINIZER_SERVICE_KEYS: {name!r} is named twice")
        named[name] = role
    if not named:
        return None
    if not directory.strip():
        raise ConfigError(
            "TRACKINIZER_SERVICE_KEYS names accounts; set TRACKINIZER_SERVICE_KEY_DIR",
        )
    return ServiceKeys(accounts=tuple(named.items()), directory=Path(directory.strip()))


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
        return substrate.PGliteEngine(
            workdir=workdir,
            persist=not config.ephemeral,
            use_tcp=config.pglite_tcp,
            own_workdir=ephemeral_workdir,
        )
    if not config.dsn:
        raise ConfigError("--engine pg requires --dsn (or $TRACKINIZER_DSN).")
    return substrate.PostgresEngine(
        dsn=config.dsn,
        listen_channel=notify.NOTIFY_CHANNEL,
    )


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
