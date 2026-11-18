"""The ``trax machine`` command: record where campaigns may run, and read it back.

The registry records machines; listing and showing need the writer role, and
changing the registry needs the admin role. On the host itself, ``connect`` joins
and keeps the machine connected with the machine's own credential. ``connect`` with
no name enrolls this computer as a personal machine of the signed-in user, and serves
a terminal in the browser.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, cast, override
from urllib.parse import urlsplit

import argparse
import re
import socket
import sys
import time

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import cwd, env
from trackinizer.trax.profile import profile_for_server
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from trackinizer.trax import (
        machine_host,
        terminal_direct_aiortc,
        terminal_host,
    )
    from trackinizer.trax.terminal_direct import PeerFactory
    from trackinizer.trax.terminal_host import TerminalHost
    from trackinizer.wire import wire_machines
    from trackinizer.wire.wire_machine_host import JoinResponse
    from trackinizer.wire.wire_machines import Machine
else:
    from wrapt import lazy_import

    # Only ``trax machine`` touches the machine models and the host loop, so their
    # pydantic build stays off the cold-start path of every other verb.
    wire_machines = lazy_import("trackinizer.wire.wire_machines")
    machine_host = lazy_import("trackinizer.trax.machine_host")
    terminal_host = lazy_import("trackinizer.trax.terminal_host")
    terminal_direct_aiortc = lazy_import("trackinizer.trax.terminal_direct_aiortc")


MACHINE_WORD: Final = "machine"
"""The verb that selects this command, in matching, help and usage."""

_FORMS: Final = (
    "[NAME [FIELD [to VALUE|-|@FILE] | label add|del LABEL | del | connect [--sandbox]]"
    " | connect [--sandbox]]"
)

_USAGE: Final = f"usage: trax {MACHINE_WORD} {_FORMS}"

_FIELDS: Final = ("role", "how")


class Machines(Command):
    """List, show, set and delete the machines campaigns may run on."""

    names = (MACHINE_WORD,)

    help = HelpPage(
        usage=f"trax {MACHINE_WORD} {_FORMS}",
        summary=(
            "Bare 'machine' lists the registered machines; 'machine NAME' shows "
            "one. Both need the writer role. Setting, labelling and deleting need "
            "the admin role. 'connect' runs on the machine's host, with its own "
            "credential."
        ),
        arguments=(
            ("NAME", "lowercase letters, digits and '-', at most 63 characters"),
            ("role", "what the machine is for; to '' clears it"),
            ("how", "one line telling an agent how to use the machine; to '' clears"),
            ("VALUE", "text; '-' reads stdin, '@FILE' reads a file"),
        ),
        options=(
            (
                "--sandbox",
                "after connect only: this computer is a disposable sandbox",
            ),
        ),
        examples=(
            f"trax {MACHINE_WORD}                           list machines",
            f"trax {MACHINE_WORD} NAME                      show one machine",
            f"trax {MACHINE_WORD} NAME role                 print its role",
            f"trax {MACHINE_WORD} NAME role to ROLE         set the role (creates)",
            f"trax {MACHINE_WORD} NAME how to TEXT          set the how line",
            f"trax {MACHINE_WORD} NAME how to -             how line from stdin",
            f"trax {MACHINE_WORD} NAME how to @FILE         how line from a file",
            f"trax {MACHINE_WORD} NAME label add LABEL      add a label",
            f"trax {MACHINE_WORD} NAME label del LABEL      remove a label",
            f"trax {MACHINE_WORD} NAME del                  unregister the machine",
            f"trax {MACHINE_WORD} NAME connect < TOKEN      join, then heartbeat",
            f"trax {MACHINE_WORD} connect                   enroll this computer, serve a terminal",
            f"trax {MACHINE_WORD} connect --sandbox         the same, on a disposable computer",
            f"trax {MACHINE_WORD} NAME connect --sandbox    join, as a disposable computer",
        ),
        notes=(
            "A field left out of a set keeps its value; to '' clears one.",
            (
                "One trailing newline is stripped from stdin and file values; an "
                "empty one is refused."
            ),
            (
                "connect reads the one-use enrollment token from stdin and saves the "
                "machine credential for this user; it exits 0 once the machine is "
                "revoked or a newer connect of it takes over. A restart serves at "
                "once; Ctrl-C shows the machine offline at once."
            ),
            (
                "'connect' with no NAME names this computer by its short hostname, "
                "enrolls it as your personal machine when it is new, and serves "
                "terminals in the browser (TRAX_MACHINE_TERMINAL=1 for that run). "
                "Needs the writer role."
            ),
            (
                "--sandbox declares this computer a disposable sandbox where agents "
                "may act freely. Every connect records its answer for this user, so "
                "a connect without the flag withdraws it. While it holds, 'trax run' "
                "starts codex without approvals and claude in bypass mode, unless you "
                "pass the agent's own permission flags. Never declare a computer "
                "that holds work or keys you would mind losing."
            ),
            (
                "A last word of help, -h or --help shows this page: set such a value "
                "with @FILE; such a machine name or label is out of reach here."
            ),
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=f"trax {MACHINE_WORD}",
            description="List, show, set, delete or host the registered machines.",
        )
        # REMAINDER, not "*": a value such as ``--verbose`` must reach ``run``
        # instead of failing as an unknown option.
        parser.add_argument("rest", nargs=argparse.REMAINDER, metavar="POS")
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
        tokens = list(cast(Sequence[str], args.rest))
        sandbox = _declares_sandbox(tokens)
        if sandbox:
            tokens.pop()
        words = [token.lower() for token in tokens]
        if not tokens:
            cls.run_list(client_factory)
            return
        if len(tokens) == 1 and words[0] == "connect":
            cls.run_connect(client_factory, None, sandbox=sandbox)
            return
        name = tokens[0]
        _check_name(name)
        if len(tokens) == 1:
            cls.run_show(client_factory, name)
        elif len(tokens) == 2 and words[1] == "del":
            cls.run_del(client_factory, name)
        elif len(tokens) == 2 and words[1] == "connect":
            cls.run_connect(client_factory, name, sandbox=sandbox)
        elif len(tokens) == 2 and words[1] in _FIELDS:
            cls.run_field(client_factory, name, words[1])
        elif len(tokens) == 4 and words[1] in _FIELDS and words[2] == "to":
            cls.run_set(client_factory, name, field=words[1], source=tokens[3])
        elif len(tokens) == 4 and words[1] == "label" and words[2] in {"add", "del"}:
            cls.run_label(client_factory, name, op=words[2], label=tokens[3])
        else:
            raise ClientError(_USAGE)

    @classmethod
    def run_list(cls, client_factory: Callable[[], Client]) -> None:
        """Print one line per machine: name, role, labels, then how.

        Args:
          client_factory: Callable returning an authenticated trax Client.

        """
        try:
            machines = client_factory().list_machines()
        except ClientError as err:
            raise _refusal(err, role="writer") from err
        if not machines:
            echo("(no machines)")
            return
        cells = [
            (m.name, m.role or "-", _labels(m) or "-", _cut(m.how)) for m in machines
        ]
        widths = [max(len(row[col]) for row in cells) for col in range(3)]
        for *head, how in cells:
            padded = (
                cell.ljust(width) for cell, width in zip(head, widths, strict=True)
            )
            echo("  ".join((*padded, how)))

    @classmethod
    def run_show(cls, client_factory: Callable[[], Client], name: str) -> None:
        """Print every field of one machine.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Machine name.

        """
        machine = _get(client_factory, name)
        echo(f"machine:    {machine.name}")
        echo(f"role:       {machine.role or '(none)'}")
        echo(f"labels:     {_labels(machine) or '(none)'}")
        echo(f"how:        {_one_line(machine.how) or '(none)'}")
        echo(f"status:     {machine.status}")
        echo(f"updated by: {machine.updated_by}")
        echo(f"updated:    {machine.updated.isoformat()}")

    @classmethod
    def run_field(
        cls,
        client_factory: Callable[[], Client],
        name: str,
        field: str,
    ) -> None:
        """Print one field's value exactly as stored.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Machine name.
          field: ``role`` or ``how``.

        """
        machine = _get(client_factory, name)
        echo(machine.role if field == "role" else machine.how)

    @classmethod
    def run_set(
        cls,
        client_factory: Callable[[], Client],
        name: str,
        *,
        field: str,
        source: str,
    ) -> None:
        """Set ``field`` from ``source``, creating the machine when it is new.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Machine name.
          field: ``role`` or ``how``.
          source: Literal value, ``-``, or ``@FILE``.

        """
        value = _read_value(source)
        try:
            if field == "role":
                client_factory().put_machine(name, role=value)
            else:
                client_factory().put_machine(name, how=value)
        except ClientError as err:
            # No name: a set creates the machine, so a 404 is a server without
            # the routes, not an absent machine.
            raise _refusal(err, role="admin") from err
        echo(f"set: {MACHINE_WORD} {name} {field}")

    @classmethod
    def run_label(
        cls,
        client_factory: Callable[[], Client],
        name: str,
        *,
        op: str,
        label: str,
    ) -> None:
        """Add or remove one label.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Machine name.
          op: ``add`` or ``del``.
          label: The label.

        """
        try:
            if op == "add":
                client_factory().change_machine_labels(name, add=[label])
            else:
                client_factory().change_machine_labels(name, remove=[label])
        except ClientError as err:
            raise _refusal(err, role="admin", name=name) from err
        echo(f"set: {MACHINE_WORD} {name} label {op} {label}")

    @classmethod
    def run_del(cls, client_factory: Callable[[], Client], name: str) -> None:
        """Unregister a machine; nothing on the machine itself is touched.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Machine name.

        """
        try:
            client_factory().delete_machine(name)
        except ClientError as err:
            raise _refusal(err, role="admin", name=name) from err
        echo(f"deleted: {MACHINE_WORD} {name}")

    @classmethod
    def run_connect(
        cls,
        client_factory: Callable[[], Client],
        name: str | None,
        *,
        sandbox: bool = False,
    ) -> None:
        """Join as ``name`` when no credential is saved, then heartbeat until revoked.

        Args:
          client_factory: Callable returning a trax Client; its URL is used, and
            with no ``name`` its key enrolls this computer.
          name: Machine name; ``None`` names this computer by its hostname, enrolls
            it as a personal machine if new, and serves terminals.
          sandbox: Whether this connect declares the computer a disposable sandbox.

        """
        client = client_factory()
        url = client.base_url
        own = name is None
        name = host_name() if name is None else name
        serve = own or env("TRAX_MACHINE_TERMINAL") == "1"
        direct = _direct_link() if serve else None
        machine_host.connect(
            name,
            server=url,
            client_for=lambda key: Client(url, api_key=key),
            read_token=(
                (lambda: client.enroll_machine(name).token)
                if own
                else sys.stdin.readline
            ),
            sleep=time.sleep,
            terminals_for=(
                (lambda joined: _terminal_host(name, url, joined, direct=direct))
                if serve
                else None
            ),
            sandbox=sandbox,
        )


def host_name() -> str:
    """Return this computer's short hostname as a machine name.

    ``connect`` with no name enrolls this computer under it, and ``trax run`` says it
    runs on the machine of that name.

    Returns:
      name: The hostname's first label, lowercased, each character a machine name
        may not hold made ``-``, trimmed of ``-`` and cut to 63.

    Raises:
      ClientError: The result is reserved or still not a machine name.

    """
    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower())
    name = name.strip("-")[:63]
    _check_name(name)
    return name


# ``--sandbox`` is a flag of ``connect`` and of nothing else. Any other place it turns
# up stays a token: a value may be spelled like it (``how to --sandbox``), and every
# other form refuses a token it does not know.
def _declares_sandbox(tokens: Sequence[str]) -> bool:
    """Return whether ``tokens`` end a ``connect`` form with ``--sandbox``."""
    return (
        len(tokens) in {2, 3}
        and tokens[-1] == "--sandbox"
        and tokens[-2].lower() == "connect"
    )


def _terminal_host(
    name: str,
    url: str,
    joined: JoinResponse,
    *,
    direct: PeerFactory | None = None,
) -> TerminalHost:
    """Make the terminal server for a joined machine; its shells name the server."""
    # A URL with credentials in it would be sent to every ssh server by ``SendEnv LC_*``.
    trax_env = {} if "@" in urlsplit(url).netloc else {"LC_TRAX_SERVER": url}
    if (profile := profile_for_server(url)) is not None:
        trax_env["TRACKINIZER_PROFILE"] = profile
    return terminal_host.TerminalHost(
        name=name,
        server_url=url,
        credential=joined.credential,
        client_for=lambda key: Client(url, api_key=key),
        trax_env=trax_env,
        direct=direct,
    )


# The link needs aiortc, which this package does not depend on: a user whose terminal
# feels slow installs it into their own tool environment.
def _direct_link() -> PeerFactory | None:
    """Return the peer factory for a direct terminal link, and say which way it is."""
    if env("TRAX_MACHINE_TERMINAL_DIRECT") == "0":
        echo("direct terminal link: off (TRAX_MACHINE_TERMINAL_DIRECT=0)")
        return None
    factory = terminal_direct_aiortc.aiortc_peer_factory()
    if factory is None:
        echo(
            "direct terminal link: off (relay only). Typing feels slow? "
            "Run: uv tool install --with aiortc "
            "git+https://github.com/rekursiv-ai/trackinizer, then restart "
            "`trax machine connect`.",
        )
    else:
        echo("direct terminal link: on")
    return factory


def _check_name(name: str) -> None:
    """Refuse a reserved or malformed name, before any request."""
    if name in wire_machines.RESERVED_NAMES:
        raise ClientError(f"machine name {name!r} is reserved")
    if not re.fullmatch(wire_machines.NAME_PATTERN, name):
        raise ClientError(
            f"invalid machine name {name!r}; expected {wire_machines.NAME_PATTERN}",
        )


def _get(client_factory: Callable[[], Client], name: str) -> Machine:
    """Fetch one machine, mapping a failed request to the error the user sees."""
    try:
        return client_factory().get_machine(name)
    except ClientError as err:
        raise _refusal(err, role="writer", name=name) from err


def _refusal(err: ClientError, *, role: str, name: str = "") -> ClientError:
    """Map a failed request to the error the user sees."""
    if err.status_code == 403:
        return ClientError(f"needs the {role} role")
    if err.status_code == 404 and name:
        return ClientError(f"no machine {name}")
    return err


def _one_line(text: str) -> str:
    """Escape the line breaks of ``text`` so it fits on one line."""
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _labels(machine: Machine) -> str:
    """Return the labels of ``machine`` joined on one line."""
    return _one_line(", ".join(machine.labels))


def _cut(how: str) -> str:
    """Return ``how`` on one line of at most 60 characters, ``-`` when empty."""
    one_line = _one_line(how)
    if not one_line:
        return "-"
    return one_line if len(one_line) <= 60 else f"{one_line[:57]}..."


def _read_value(source: str) -> str:
    """Resolve a value source; stdin and file text lose one trailing newline."""
    if source != "-" and not source.startswith("@"):
        return source
    try:
        text = _source_bytes(source).decode()
    except UnicodeDecodeError:
        raise ClientError("the value is not valid UTF-8") from None
    if text.endswith("\n"):
        text = text.removesuffix("\n").removesuffix("\r")
    if not text:
        raise ClientError("empty value; clear a field with to ''")
    return text


def _source_bytes(source: str) -> bytes:
    """Read the bytes of ``-`` (stdin) or ``@FILE``."""
    # Bytes, not text: text mode folds ``\r\n`` into ``\n``, which would hide the
    # pair that counts as one trailing newline.
    if source == "-":
        return cast(bytes, sys.stdin.buffer.read())
    path = source[1:]
    if not path:
        raise ClientError("@ value requires a path")
    try:
        # Resolved against the CALLER's directory: under the daemon the process cwd
        # belongs to whichever shell spawned it.
        return (cwd() / path).read_bytes()
    except OSError as err:
        raise ClientError(
            f"cannot read @{path}: {err.strerror or type(err).__name__}",
        ) from None
