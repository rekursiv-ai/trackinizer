"""CLI dispatcher: peel top-level flags, then route to one ``Command``.

Also owns ``connect``, which builds a ``Client`` from the flags, env vars,
and saved profile. It lives here rather than on ``Client`` so ``client.py``
stays pure HTTP transport with no dependency on the profile store.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, override
from urllib.parse import urlparse

import argparse
import atexit
import functools
import sys
import threading

from trackinizer.client.client import Client, server_url
from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import env
from trackinizer.trax.grammar import VALID_KINDS, ListQuery
from trackinizer.trax.profile import (
    Profile,
    Profiles,
    load_profile,
    read_profile,
)
from trackinizer.trax.render import SHOW_IDS, echo
from trackinizer.trax.verbs import (
    Blocked,
    Board,
    Cost,
    Graph,
    Id,
    Kind,
    Next,
    Recent,
    Search,
    Send,
    Version,
    run_list_query,
)


if TYPE_CHECKING:
    from trackinizer.trax.run.session import main as run_main
else:
    from wrapt import lazy_import

    # ``trax run`` is the only verb that needs the PTY/tail/adapter machinery
    # (importing ``trax.run.session`` costs ~324ms), so bind it lazily: the
    # proxy resolves on first call, which only happens inside the ``run`` branch.
    run_main = lazy_import("trackinizer.trax.run.session", "main")


def connect_flags(parser: argparse.ArgumentParser) -> None:
    """Register ``--profile``, ``--host``, and ``--port``."""
    parser.add_argument("--profile", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)


def connect(args: argparse.Namespace) -> Client:
    """Return a Client for the flags, environment, and saved profile.

    Resolution order, highest precedence first:
      1. ``--host`` / ``--port`` flags.
      2. ``--profile <name>`` (a saved profile; missing on disk is an error).
      3. ``$TRACKINIZER_URL`` (a raw URL).
      4. ``$TRACKINIZER_PROFILE`` (a profile name; missing is an error).
      5. The ``current`` file written by ``trax profile current NAME``.
      6. The ``default`` profile.
      7. ``http://127.0.0.1:8765``, when nothing above is set.

    Clients are shared per resolved identity. A one-shot CLI run builds
    exactly one either way, but the daemon serves thousands of invocations
    from one process: a fresh ``Client`` per request would open a new
    connection pool each time -- discarding the keep-alive the transport
    exists to provide -- and accumulate sockets for the daemon's whole life.
    """
    return _shared_client(_resolve_target(args))


@dataclass(frozen=True, kw_only=True, slots=True)
class _Target:
    """The resolved connection identity a ``Client`` is keyed on."""

    url: str
    author: str
    api_key: str


def _resolve_target(args: argparse.Namespace) -> _Target:
    """Resolve flags, environment, and profile into one connection identity."""
    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if name := getattr(args, "profile", None):
        profile = read_profile(name)
    elif env_url := env("TRACKINIZER_URL"):
        profile = Profile(url=server_url(env_url, "TRACKINIZER_URL"), author="")
    else:
        profile = load_profile()
    url = profile.url
    if host is not None or port is not None:
        parsed = urlparse(profile.url)
        scheme = parsed.scheme or "http"
        host = host or parsed.hostname or "127.0.0.1"
        port = port or parsed.port
        url = f"{scheme}://{host if port is None else f'{host}:{port}'}"
    return _Target(url=url, author=profile.author, api_key=profile.api_key)


_CLIENTS: Final[dict[_Target, Client]] = {}
"""Live clients by connection identity, shared across invocations.

A module global because the sharing must outlive one ``parse_and_run`` call:
under the daemon that function runs per request, so a call-local cache would
build (and leak) a pool per request. Bounded by the number of distinct
profiles a user actually addresses."""

_CLIENTS_LOCK: Final = threading.Lock()
"""The daemon serves requests on threads, so two may resolve the same target
at once; without this each would build a pool and one would be orphaned."""


def _shared_client(target: _Target) -> Client:
    """The Client for ``target``, building it once per process."""
    with _CLIENTS_LOCK:
        if (client := _CLIENTS.get(target)) is not None:
            return client
        client = Client(target.url, author=target.author, api_key=target.api_key)
        _CLIENTS[target] = client
        return client


def close_clients() -> None:
    """Close every shared client and forget it.

    Registered by the one-shot CLI path at exit; the daemon calls it when a
    profile is rewritten, since the cached client still carries the old token.
    """
    with _CLIENTS_LOCK:
        for client in _CLIENTS.values():
            client.close()
        _CLIENTS.clear()


class Help(Command):
    """Print top-level help or per-verb help."""

    names = ("help",)
    help = HelpPage(
        usage="trax COMMAND [ARGS] [OPTIONS]",
        summary="Subjects:\n  issue artifact experiment paper belief codechange webresult websearch agentsession",
        examples=(
            "trax issue                                           list issues",
            'trax issue title to "Retry bug" priority to high     create issue',
            "trax issue 7 priority to high                        set field",
            "trax issue 7 blocks issue 3                          link rows",
            "trax search retry --kind issue                       search rows",
            "trax recent --limit 10                               show audit log",
            "trax profile                                         show active profile",
            "trax profile url to https://trackinizer.example      set server URL",
        ),
        notes=(
            "Commands: search recent next blocked graph board cost profile",
            "Help: trax issue help; trax issue 7 priority help; trax profile url help",
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax help", description=cls.__doc__)
        parser.add_argument("topic", nargs="?", default="")
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb, client_factory
        if not args.topic:
            echo(cls.help_text(), nl=False)
            return
        topic = args.topic.lower()
        for dispatcher in DISPATCHERS:
            if dispatcher.matches(topic):
                echo(dispatcher.help_text_for(topic), nl=False)
                return
        raise ClientError(f"unknown verb: {topic!r}")


DISPATCHERS: tuple[type[Command], ...] = (
    Kind,
    Id,
    Search,
    Recent,
    Next,
    Blocked,
    Graph,
    Board,
    Cost,
    Send,
    Version,
    Help,
    Profiles,
)


def parse_and_run(
    argv: Sequence[str],
    *,
    client_factory: Callable[[], Client] | None = None,
) -> None:
    """Parse ``argv`` per the grammar and execute the matching verb.

    The connection flags (``--profile``, ``--host``, ``--port``) are peeled
    off first and bound into the default ``client_factory``. A leading or
    sole ``--help`` / ``-h`` routes to the ``help`` verb; bare ``trax``
    lists every kind. Tests pass an explicit ``client_factory``, so the
    connection flags go unused.
    """
    top, leftover = _peel_top_flags(list(argv))
    SHOW_IDS.set(bool(getattr(top, "show_ids", False)))
    if client_factory is None:
        # ``connect`` shares one Client per resolved target for the life of
        # the process, so every verb here -- and every later invocation, when
        # a daemon runs this function repeatedly -- reuses one httpx2 pool.
        client_factory = functools.partial(connect, top)
    if leftover and leftover[0] in {"--help", "-h"}:
        echo(Help.help_text(), nl=False)
        return None
    if not leftover:
        args = Kind.make_parser().parse_args([])
        query = ListQuery(kinds=VALID_KINDS, ranges={}, filters=())
        return run_list_query(query, args, client_factory)
    verb = leftover[0].lower()
    rest = leftover[1:]
    if verb == "run":
        # ``trax run`` shim. ``run_main`` is a module-level ``lazy_import`` proxy,
        # so the PTY/tail machinery imports only here, on first call -- other
        # verbs never pay for it. Sync is on by default, so resolve a Client
        # from the active profile (same chain every verb uses) and hand it over;
        # the memoized ``client_factory`` is unused.
        del client_factory
        rc = run_main(rest, client_factory=lambda: connect(top))
        if rc != 0:
            sys.exit(rc)
        return None
    for dispatcher in DISPATCHERS:
        if dispatcher.matches(verb):
            return dispatcher.dispatch(verb, rest, client_factory)
    raise ClientError(f"unknown verb: {verb!r}")


def _peel_top_flags(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Split connection flags from the sub-command argv.

    Global flags are peeled ONLY from the pre-verb prefix -- the run of leading
    tokens up to the first verb/kind. A flag spelling that appears AFTER the
    verb is a field value (``issue 7 title to --show-ids``) and must flow
    through verbatim, so it is never consumed here (TRAX-CLI-001). Top-level
    ``--help`` is left for ``parse_and_run`` to route to the ``help`` verb,
    keeping one help renderer in charge of output.
    """
    parser = argparse.ArgumentParser(prog="trax", add_help=False)
    connect_flags(parser)
    parser.add_argument(
        "--show-ids",
        dest="show_ids",
        action="store_true",
        help="include UUIDs in command output (hidden by default)",
    )
    split = _prefix_end(argv)
    args, unknown = parser.parse_known_args(argv[:split])
    return args, [*unknown, *argv[split:]]


# The connection flags that take a value, so the prefix scan knows to skip the
# token after them when locating the verb. ``--show-ids`` is a store_true and
# takes none.
_VALUE_FLAGS: frozenset[str] = frozenset({"--profile", "--host", "--port"})


def _prefix_end(argv: list[str]) -> int:
    """Index of the first verb/kind token: the end of the global-flag prefix.

    The prefix is the leading run of global flags and their values. The first
    token that is neither a flag (``--``) nor the value of a preceding
    value-taking flag is the verb/kind; everything from there on is the
    sub-command, where a flag spelling is a field value, not a global flag.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            return index
        # ``--flag=value`` carries its value inline; a bare value-taking flag
        # consumes the next token.
        if "=" not in token and token in _VALUE_FLAGS:
            index += 1
        index += 1
    return len(argv)


def main(argv: Sequence[str] | None = None) -> None:
    # Drain pooled sockets at exit; httpx2 warns if a Client is garbage
    # collected with any still open. The daemon never reaches this path -- it
    # calls ``parse_and_run`` directly and keeps its clients for its lifetime.
    atexit.register(close_clients)
    try:
        parse_and_run(sys.argv[1:] if argv is None else list(argv))
    except ClientError as err:
        sys.stderr.write(f"trax: {err}\n")
        sys.exit(2)
