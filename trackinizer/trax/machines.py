"""The ``trax machine`` command: record where campaigns may run, and read it back.

The registry only records machines; no verb here reaches one. Listing and
showing need the writer role, and changing the registry needs the admin role.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, cast, override

import argparse
import re
import sys

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import cwd
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from trackinizer.client.client import Client
    from trackinizer.wire import wire_machines
    from trackinizer.wire.wire_machines import Machine
else:
    from wrapt import lazy_import

    # Only ``trax machine`` touches the machine models, so their pydantic build
    # stays off the cold-start path of every other verb.
    wire_machines = lazy_import("trackinizer.wire.wire_machines")


MACHINE_WORD: Final = "machine"
"""The verb that selects this command, in matching, help and usage."""

_FORMS: Final = "[NAME [FIELD [to VALUE|-|@FILE] | label add|del LABEL | del]]"

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
            "the admin role. The registry records machines; it never reaches one."
        ),
        arguments=(
            ("NAME", "lowercase letters, digits and '-', at most 63 characters"),
            ("role", "what the machine is for; to '' clears it"),
            ("how", "one line telling an agent how to use the machine; to '' clears"),
            ("VALUE", "text; '-' reads stdin, '@FILE' reads a file"),
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
        ),
        notes=(
            "A field left out of a set keeps its value; to '' clears one.",
            (
                "One trailing newline is stripped from stdin and file values; an "
                "empty one is refused."
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
            description="List, show, set or delete the registered machines.",
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
        name = tokens[0]
        _check_name(name)
        if len(tokens) == 1:
            cls.run_show(client_factory, name)
        elif len(tokens) == 2 and words[1] == "del":
            cls.run_del(client_factory, name)
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
