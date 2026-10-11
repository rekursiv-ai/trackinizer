"""The ``trax machine`` command: record where campaigns may run, and read it back.

The registry records machines; listing and showing need the writer role, and
changing the registry needs the admin role. A writer may also provision a machine
(``up``), for a provider to create, and take it down again (``down``). On the host
itself, ``connect`` joins and keeps the machine connected, and ``request`` asks
its provider for something, both with the machine's own credential.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, cast, override

import argparse
import json
import re
import sys
import time

from treekle import from_plain, json_loads, to_plain

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import cwd
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from trackinizer.trax import machine_host
    from trackinizer.wire import wire_machines
    from trackinizer.wire.wire_machines import Machine
else:
    from wrapt import lazy_import

    # Only ``trax machine`` touches the machine models and the host loop, so their
    # pydantic build stays off the cold-start path of every other verb.
    wire_machines = lazy_import("trackinizer.wire.wire_machines")
    machine_host = lazy_import("trackinizer.trax.machine_host")


MACHINE_WORD: Final = "machine"
"""The verb that selects this command, in matching, help and usage."""

_FORMS: Final = (
    "[up PROVIDER [ttl SECONDS] [spec VALUE] | NAME [FIELD [to VALUE] "
    "| label add|del LABEL | del | down | connect | request KIND [body VALUE]]]"
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
            "one. Both need the writer role, as do 'up' and 'down'. Setting, "
            "labelling and deleting need the admin role. 'connect' and 'request' "
            "run on the machine's host, with its own credential."
        ),
        arguments=(
            ("NAME", "lowercase letters, digits and '-', at most 63 characters"),
            ("role", "what the machine is for; to '' clears it"),
            ("how", "one line telling an agent how to use the machine; to '' clears"),
            ("VALUE", "text; '-' reads stdin, '@FILE' reads a file"),
            ("PROVIDER", "the provider that creates the host, such as a cloud name"),
            ("SECONDS", "the machine's lifetime; the server's most when left out"),
            ("KIND", "what a request asks the provider for, such as 'push'"),
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
            f"trax {MACHINE_WORD} up PROVIDER               provision; prints the name",
            f"trax {MACHINE_WORD} up PROVIDER ttl 3600      provision for an hour",
            f"trax {MACHINE_WORD} up PROVIDER spec @FILE    hand the provider a spec",
            f"trax {MACHINE_WORD} NAME down                 ask its provider to end it",
            f"trax {MACHINE_WORD} NAME connect < TOKEN      join, then heartbeat",
            f"trax {MACHINE_WORD} NAME request KIND         ask its provider and wait",
        ),
        notes=(
            "A field left out of a set keeps its value; to '' clears one.",
            (
                "One trailing newline is stripped from stdin and file values; an "
                "empty one is refused."
            ),
            "A spec, and a request's 'body VALUE', is one JSON object.",
            (
                "connect reads the one-use enrollment token from stdin and saves the "
                "machine credential for this user; it exits 0 once the machine is "
                "revoked or destroyed."
            ),
            (
                "request waits for the provider's result, prints it as JSON, and "
                "exits nonzero when the request failed."
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
            description="List, show, set, provision or host the registered machines.",
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
        tokens = cast(Sequence[str], args.rest)
        words = [token.lower() for token in tokens]
        if not tokens:
            cls.run_list(client_factory)
            return
        if words[0] == "up":
            cls.run_up(client_factory, tokens[1:])
            return
        name = tokens[0]
        _check_name(name)
        if len(tokens) == 1:
            cls.run_show(client_factory, name)
        elif len(tokens) == 2 and words[1] == "del":
            cls.run_del(client_factory, name)
        elif len(tokens) == 2 and words[1] == "down":
            cls.run_down(client_factory, name)
        elif len(tokens) == 2 and words[1] == "connect":
            cls.run_connect(client_factory, name)
        elif words[1] == "request":
            cls.run_request(client_factory, name, tokens[2:])
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
        if machine.provider:
            echo(f"provider:   {machine.provider}")
            echo(f"requested:  {machine.requested_by}")
            for label, at in (
                ("expires:", machine.expires_at),
                ("down:", machine.down_at),
                ("destroyed:", machine.destroyed_at),
            ):
                if at is not None:
                    echo(f"{label:<11} {at.isoformat()}")
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
    def run_up(
        cls,
        client_factory: Callable[[], Client],
        tokens: Sequence[str],
    ) -> None:
        """Provision a machine and print its name.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          tokens: ``PROVIDER [ttl SECONDS] [spec VALUE]``.

        """
        provider, *rest = tokens or [""]
        words = [token.lower() for token in rest]
        ttl_sec: int | None = None
        if words[:1] == ["ttl"] and len(rest) >= 2:
            ttl_sec = _seconds(rest[1])
            rest, words = rest[2:], words[2:]
        spec: dict[str, object] = {}
        if words[:1] == ["spec"] and len(rest) == 2:
            spec = _json_object(rest[1], what="spec")
            rest = []
        if not provider or rest:
            raise ClientError(_USAGE)
        try:
            machine = client_factory().provision_machine(
                provider,
                ttl_sec=ttl_sec,
                spec=spec,
            )
        except ClientError as err:
            raise _refusal(err, role="writer") from err
        echo(machine.name)

    @classmethod
    def run_down(cls, client_factory: Callable[[], Client], name: str) -> None:
        """Ask a provisioned machine's provider to take it down.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Machine name.

        """
        try:
            client_factory().down_machine(name)
        except ClientError as err:
            raise _refusal(err, role="writer", name=name) from err
        echo(f"down: {MACHINE_WORD} {name}")

    @classmethod
    def run_connect(cls, client_factory: Callable[[], Client], name: str) -> None:
        """Join as ``name`` when no credential is saved, then heartbeat until revoked.

        Args:
          client_factory: Callable returning a trax Client; only its URL is used.
          name: Machine name.

        """
        url = client_factory().base_url
        machine_host.connect(
            name,
            client_for=lambda key: Client(url, api_key=key),
            read_token=sys.stdin.readline,
            sleep=time.sleep,
        )

    @classmethod
    def run_request(
        cls,
        client_factory: Callable[[], Client],
        name: str,
        tokens: Sequence[str],
    ) -> None:
        """Ask the machine's provider for something, as the machine, and wait.

        Prints the result as JSON, and fails when the provider reports ``failed``.

        Args:
          client_factory: Callable returning a trax Client; only its URL is used.
          name: Machine name.
          tokens: ``KIND [body VALUE]``; no body sends ``{}``.

        """
        if len(tokens) == 1:
            body: dict[str, object] = {}
        elif len(tokens) == 3 and tokens[1].lower() == "body":
            body = _json_object(tokens[2], what="body")
        else:
            raise ClientError(_USAGE)
        kind = tokens[0]
        url = client_factory().base_url
        finished = machine_host.send_request(
            name,
            kind=kind,
            body=body,
            client_for=lambda key: Client(url, api_key=key),
            sleep=time.sleep,
        )
        echo(json.dumps(to_plain(finished.result)))
        if finished.status == "failed":
            raise ClientError(f"request {finished.id} failed")


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


def _seconds(text: str) -> int:
    """Parse a ``ttl``, refusing anything but a whole number of seconds."""
    if not text.isdigit():
        raise ClientError(f"ttl must be a whole number of seconds, not {text!r}")
    return int(text)


def _json_object(source: str, *, what: str) -> dict[str, object]:
    """Read one JSON object from a literal, ``-`` or ``@FILE``."""
    try:
        return from_plain(json_loads(_read_value(source)), dict[str, object])
    except ValueError:
        # ``ReadError`` is a ``ValueError`` too: valid JSON that is not an object.
        raise ClientError(f"{what} must be one JSON object") from None


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
