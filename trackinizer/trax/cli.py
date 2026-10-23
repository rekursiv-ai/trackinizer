"""CLI dispatcher: peel top-level flags, then route to one ``Command``.

``connect``, which builds a ``Client`` from the flags, env vars, and saved
profile, lives in ``connection``; this module re-exports it for its callers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, cast, override

import argparse
import atexit
import functools
import sys

from trackinizer.client.errors import ClientError
from trackinizer.trax.client_cache import close_clients
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.connection import connect, connect_flags
from trackinizer.trax.doctor import Doctor
from trackinizer.trax.forward import MOVED_VERBS, moved
from trackinizer.trax.parser import parse_backfill_timestamp, parse_list_query
from trackinizer.trax.profile import Profiles, unmatched_server_hint
from trackinizer.trax.render import SHOW_IDS, echo
from trackinizer.trax.variables import ENV_WORD, Variables
from trackinizer.trax.verbs import (
    Authority,
    Blocked,
    Board,
    Confidence,
    Cost,
    Export,
    Graph,
    Id,
    Kind,
    Next,
    Recent,
    Search,
    Send,
    Version,
    Workspace,
    run_list_query,
)


if TYPE_CHECKING:
    from trackinizer.client.client import Client


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
            "trax issue title re retry                            filter rows",
            "trax title re retry                                  filter every kind",
            "trax recent --limit 10                               show audit log",
            "trax profile                                         show active profile",
            "trax profile url to https://trackinizer.example      set server URL",
        ),
        notes=(
            f"Commands: recent next blocked graph board cost profile doctor {ENV_WORD}",
            f"Moved out of trax: {' '.join(sorted(MOVED_VERBS))}",
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
        del verb
        flags = cast(_HelpFlags, args)
        if not flags.topic:
            echo(cls.help_text(), nl=False)
            return
        topic = flags.topic.lower()
        if topic in MOVED_VERBS:
            moved(topic)
        for dispatcher in DISPATCHERS:
            if dispatcher.matches(topic):
                echo(dispatcher.help_text_for(topic), nl=False)
                return
        raise ClientError(f"unknown verb: {topic!r}")


DISPATCHERS: tuple[type[Command], ...] = (
    Kind,
    Id,
    Recent,
    Search,
    Next,
    Blocked,
    Graph,
    Board,
    Cost,
    Confidence,
    Authority,
    Send,
    Version,
    Export,
    Workspace,
    Doctor,
    Help,
    Profiles,
    Variables,
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

    Args:
      argv: Command-line tokens to parse (verb and arguments).
      client_factory: Optional factory; uses profile if None.

    """
    top, leftover = _peel_top_flags(list(argv))
    flags = cast(_TopFlags, top)
    SHOW_IDS.set(flags.show_ids)
    if flags.backfill is not None:
        if not leftover or leftover[0].lower() not in Kind.names:
            raise ClientError("--backfill requires a create command")
        leftover = [leftover[0], f"--backfill={flags.backfill}", *leftover[1:]]
    if client_factory is None:
        # ``connect`` shares one Client per resolved target for the life of
        # the process, so every verb here -- and every later invocation, when
        # a daemon runs this function repeatedly -- reuses one httpx2 pool.
        client_factory = functools.partial(connect, top)
    if leftover and leftover[0] in {"--help", "-h"}:
        echo(Help.help_text(), nl=False)
        return None
    if not leftover:
        return _run_kindless(leftover, client_factory)
    verb = leftover[0].lower()
    rest = leftover[1:]
    if verb in MOVED_VERBS:
        moved(verb)
    for dispatcher in DISPATCHERS:
        if dispatcher.matches(verb):
            return dispatcher.dispatch(verb, rest, client_factory)
    # Neither a verb nor a kind: the token is a FILTER FIELD, and the query
    # spans every kind (``trax title re retry``). Verbs and kinds are matched
    # first, so adding a field can never steal an existing command. A token
    # that is no field either fails inside the parser naming the field, which
    # is the accurate diagnosis -- it was never a verb position.
    return _run_kindless(leftover, client_factory)


# The connection flags that take a value, so the prefix scan knows to skip the
# token after them when locating the verb. ``--show-ids`` is a store_true and
# takes none.
_VALUE_FLAGS: frozenset[str] = frozenset(
    {"--profile", "--host", "--port", "--backfill"},
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the program; return the process exit code.

    Args:
      argv: Command-line arguments; None for sys.argv[1:].

    Returns:
      exit_code: 0 on success, 2 on ClientError.

    """
    # Drain pooled sockets at exit; httpx2 warns if a Client is garbage
    # collected with any still open. The daemon never reaches this path -- it
    # calls ``parse_and_run`` directly and keeps its clients for its lifetime.
    atexit.register(close_clients)
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        # A verb that moved out of trax only says so: a hint about trax's profiles is
        # not its.
        verb = next(iter(args[_prefix_end(args) :]), "").lower()
        if verb not in MOVED_VERBS and (hint := unmatched_server_hint()) is not None:
            _ = sys.stderr.write(hint)
        parse_and_run(args)
    except ClientError as err:
        sys.stderr.write(f"trax: {err}\n")
        return 2
    return 0


class _HelpFlags(Protocol):
    """Parsed ``trax help`` flags."""

    topic: str


class _TopFlags(Protocol):
    """Parsed top-level flags, as ``_peel_top_flags`` registers them.

    ``connect`` still takes the raw ``Namespace``: its callers may pass a bare
    ``Namespace()`` and rely on the ``getattr`` defaults in ``resolve_target``.
    """

    profile: str | None
    host: str | None
    port: int | None
    show_ids: bool
    backfill: str | None


# The one kindless path: bare ``trax`` reaches it with no tokens, and a leading filter
# field reaches it with the whole command. Parsed through ``Kind``'s parser so
# ``--format`` / ``--limit`` / ``--sort`` mean the same thing they do after a kind name.
def _run_kindless(tokens: Sequence[str], client_factory: Callable[[], Client]) -> None:
    """List across every kind, filtered by ``tokens``."""
    args = Kind.make_parser().parse_args(list(tokens))
    query = parse_list_query(None, cast(Sequence[str], args.rest))
    if query is None:
        raise ClientError(f"unknown verb: {tokens[0]!r}")
    return run_list_query(query, args, client_factory)


# Global flags are peeled ONLY from the pre-verb prefix -- the run of leading tokens up
# to the first verb/kind. A flag spelling that appears AFTER the verb is a field value
# (``issue 7 title to --show-ids``) and must flow through verbatim, so it is never
# consumed here (TRAX-CLI-001). Top-level ``--help`` is left for ``parse_and_run`` to
# route to the ``help`` verb, keeping one help renderer in charge of output.
def _peel_top_flags(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Split connection flags from the sub-command argv."""
    parser = argparse.ArgumentParser(prog="trax", add_help=False)
    connect_flags(parser)
    parser.add_argument(
        "--show-ids",
        dest="show_ids",
        action="store_true",
        help="include UUIDs in command output (hidden by default)",
    )
    parser.add_argument("--backfill", type=parse_backfill_timestamp, default=None)
    split = _prefix_end(argv)
    args, unknown = parser.parse_known_args(argv[:split])
    return args, [*unknown, *argv[split:]]


# The prefix is the leading run of global flags and their values. The first token that
# is neither a flag (``--``) nor the value of a preceding value-taking flag is the
# verb/kind; everything from there on is the sub-command, where a flag spelling is a
# field value, not a global flag.
def _prefix_end(argv: list[str]) -> int:
    """Index of the first verb/kind token: the end of the global-flag prefix."""
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
