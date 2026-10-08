"""The ``trax env`` command: list, set and delete the org's environment variables.

A secret's value is write-only. It is read from stdin or a file, never from the
command line, and no message this module prints or raises contains it.
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
    from trackinizer.wire import wire_variables
else:
    from wrapt import lazy_import

    # Only ``trax env`` touches the variable models, so their pydantic build stays
    # off the cold-start path of every other verb.
    wire_variables = lazy_import("trackinizer.wire.wire_variables")


ENV_WORD: Final = "env"
"""The verb that selects this command, in matching, help and usage."""

_FORMS: Final = "[NAME to VALUE|-|@FILE | secret NAME to -|@FILE | NAME del]"

_USAGE: Final = f"usage: trax {ENV_WORD} {_FORMS}"


class Variables(Command):
    """List, set and delete the org's environment variables."""

    names = (ENV_WORD,)

    help = HelpPage(
        usage=f"trax {ENV_WORD} {_FORMS}",
        summary=(
            "Bare 'env' lists the org's variables; a secret shows (secret), never "
            "its value. Setting and deleting need the admin role."
        ),
        arguments=(
            ("VALUE", "text; '-' reads stdin, '@FILE' reads a file"),
            ("secret", "write-only; its value is never read back"),
        ),
        examples=(
            f"trax {ENV_WORD}                            list variables",
            f"trax {ENV_WORD} NAME to VALUE              set a plain value",
            f"trax {ENV_WORD} NAME to -                  plain value from stdin",
            f"trax {ENV_WORD} NAME to @FILE              plain value from a file",
            f"trax {ENV_WORD} secret NAME to -           secret value from stdin",
            f"trax {ENV_WORD} secret NAME to @FILE       secret value from a file",
            f"trax {ENV_WORD} NAME del                   delete a variable",
        ),
        notes=(
            "A secret's value never travels on the command line.",
            "One trailing newline is stripped from stdin and file values.",
            (
                "A last word of help, -h or --help shows this page; set such a value "
                "with @FILE."
            ),
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=f"trax {ENV_WORD}",
            description="List, set or delete the org's environment variables.",
        )
        # REMAINDER, not "*": a plain value such as ``--verbose`` must reach
        # ``run`` instead of failing as an unknown option.
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
        # ``secret`` is a keyword only in front of ``NAME to SOURCE``; anywhere
        # else it is an ordinary variable name.
        elif len(tokens) == 4 and words[0] == "secret" and words[2] == "to":
            cls.run_set(client_factory, tokens[1], tokens[3], secret=True)
        elif len(tokens) == 3 and words[1] == "to":
            cls.run_set(client_factory, tokens[0], tokens[2], secret=False)
        elif len(tokens) == 2 and words[1] == "del":
            cls.run_del(client_factory, tokens[0])
        else:
            raise ClientError(_USAGE)

    @classmethod
    def run_list(cls, client_factory: Callable[[], Client]) -> None:
        """Print one line per variable: name, then value or ``(secret)``.

        Args:
          client_factory: Callable returning an authenticated trax Client.

        """
        try:
            variables = client_factory().list_variables()
        except ClientError as err:
            refusal = _refusal(err, role="writer")
        else:
            if not variables:
                echo("(no variables)")
                return
            width = max(len(variable.name) for variable in variables)
            for variable in variables:
                shown = "(secret)" if variable.secret else variable.value or ""
                one_line = shown.replace("\r", "\\r").replace("\n", "\\n")
                echo(f"{variable.name.ljust(width)}  {one_line}")
            return
        raise refusal

    @classmethod
    def run_set(
        cls,
        client_factory: Callable[[], Client],
        name: str,
        source: str,
        *,
        secret: bool,
    ) -> None:
        """Set ``name`` from ``source``: a literal, ``-`` (stdin) or ``@FILE``.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Variable name.
          source: Literal value, ``-``, or ``@FILE``.
          secret: Store the value write-only; a literal ``source`` is refused.

        """
        _check_name(name)
        if secret and source != "-" and not source.startswith("@"):
            raise ClientError(
                "a secret's value comes from stdin (-) or a file (@FILE), "
                "never the command line",
            )
        value = _read_value(source, secret=secret)
        if not value:
            raise ClientError("empty value")
        if len(value.encode()) > wire_variables.MAX_VALUE_BYTES:
            raise ClientError(f"value exceeds {wire_variables.MAX_VALUE_BYTES} bytes")
        try:
            client_factory().put_variable(name, value=value, secret=secret)
        except ClientError as err:
            refusal = _refusal(err, role="admin", secret=secret)
        else:
            echo(f"set: {ENV_WORD} {name}{' (secret)' if secret else ''}")
            return
        raise refusal

    @classmethod
    def run_del(cls, client_factory: Callable[[], Client], name: str) -> None:
        """Delete ``name``; a 404 means it was not set.

        Args:
          client_factory: Callable returning an authenticated trax Client.
          name: Variable name.

        """
        _check_name(name)
        try:
            client_factory().delete_variable(name)
        except ClientError as err:
            if err.status_code == 404:
                refusal = ClientError(f"variable {name!r} not found")
            else:
                refusal = _refusal(err, role="admin")
        else:
            echo(f"deleted: {ENV_WORD} {name}")
            return
        raise refusal


def _check_name(name: str) -> None:
    """Refuse a name the store would not accept, before any request."""
    if not re.fullmatch(wire_variables.NAME_PATTERN, name):
        raise ClientError(
            f"invalid variable name {name!r}; expected {wire_variables.NAME_PATTERN}",
        )


def _read_value(source: str, *, secret: bool) -> str:
    """Resolve a value source; stdin and file text lose one trailing newline."""
    if source != "-" and not source.startswith("@"):
        return source
    raw = _source_bytes(source, secret=secret)
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        # The decoder's own message names the first bad byte, which may be part of
        # a secret.
        refusal = ClientError("the value is not valid UTF-8")
    else:
        if text.endswith("\n"):
            return text.removesuffix("\n").removesuffix("\r")
        return text
    raise refusal


def _source_bytes(source: str, *, secret: bool) -> bytes:
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
        reason = err.strerror or type(err).__name__
        # A literal secret that starts with ``@`` lands here, so its text must not
        # be repeated.
        refusal = ClientError(
            f"cannot read the secret file ({reason})"
            if secret
            else f"cannot read @{path}: {reason}",
        )
    raise refusal


# Raised by the caller outside its ``except`` block, so the server's text is
# neither the new error's cause nor its context.
# A 403 means the role is missing. A failed secret write never shows the server's
# text: it can echo the request body, JSON-escaped or cut off, and no match on the
# value finds every such form.
def _refusal(err: ClientError, *, role: str, secret: bool = False) -> ClientError:
    """Map a failed request to the error the user sees."""
    if err.status_code == 403:
        return ClientError(f"needs the {role} role")
    if secret and err.status_code is not None:
        return ClientError(f"server refused the request (HTTP {err.status_code})")
    return ClientError(str(err))
