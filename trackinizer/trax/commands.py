"""Base ``Command`` class and help-page model for the ``trax`` dispatcher."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import argparse

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax.render import echo


@dataclass(frozen=True, kw_only=True, slots=True)
class HelpPage:
    """A plain-text help page for one command or grammar topic."""

    usage: str
    summary: str
    arguments: tuple[tuple[str, str], ...] = ()
    options: tuple[tuple[str, str], ...] = ()
    examples: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def render(self) -> str:
        """Format the page as CLI text."""
        lines = [f"Usage: {self.usage}", "", self.summary]
        for title, rows in (("Arguments", self.arguments), ("Options", self.options)):
            if not rows:
                continue
            width = max(len(name) for name, _ in rows)
            lines.extend(["", f"{title}:"])
            lines.extend(f"  {name.ljust(width)}  {text}" for name, text in rows)
        if self.examples:
            lines.extend(["", "Examples:"])
            lines.extend(f"  {example}" for example in self.examples)
        if self.notes:
            lines.extend(["", "Notes:"])
            lines.extend(f"  {note}" for note in self.notes)
        return "\n".join(lines).rstrip() + "\n"

    def with_usage(
        self,
        usage: str,
        *,
        examples: tuple[str, ...] | None = None,
    ) -> HelpPage:
        """Copy this page with a concrete usage line (and optional examples)."""
        return HelpPage(
            usage=usage,
            summary=self.summary,
            arguments=self.arguments,
            options=self.options,
            examples=self.examples if examples is None else examples,
            notes=self.notes,
        )


class Command:
    """Base class for one trax command's grammar and dispatch."""

    names: ClassVar[tuple[str, ...]]
    help: ClassVar[str | HelpPage] = ""

    @classmethod
    def matches(cls, verb: str) -> bool:
        """Whether this command handles ``verb``."""
        return verb in cls.names

    @classmethod
    def make_parser(cls) -> argparse.ArgumentParser:
        """Build this command's argparse parser."""
        raise NotImplementedError

    @classmethod
    def dispatch(
        cls,
        verb: str,
        rest: list[str],
        client_factory: Callable[[], Client],
    ) -> None:
        """Parse the local args and run this command.

        A trailing ``help`` is a tail action: the tokens before it go to
        ``help_with_context`` for context-specific help. ``--help`` / ``-h``
        are an alias for bare ``help`` in leading *or* trailing position
        (``trax issue 7 --help`` shows the context help, not argparse
        usage); only a ``--help`` buried mid-command stays an argparse token.
        """
        if rest and rest[0] in {"--help", "-h"}:
            rest = [*rest[1:], "help"]
        elif rest and rest[-1] in {"--help", "-h"}:
            rest = [*rest[:-1], "help"]
        if rest and rest[-1] == "help":
            echo(cls.help_with_context(verb, rest[:-1]), nl=False)
            return
        args = cls.make_parser().parse_args(rest)
        cls.run(verb, args, client_factory)

    @classmethod
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        """Execute the parsed args."""
        del verb, args, client_factory
        raise NotImplementedError

    @classmethod
    def help_text(cls) -> str:
        """Help text for this command's first name."""
        return cls.help_text_for(next(iter(cls.names)))

    @classmethod
    def help_text_for(cls, verb: str) -> str:
        """Help text for one handled verb."""
        del verb
        if isinstance(cls.help, HelpPage):
            return cls.help.render()
        if cls.help:
            return cls.help
        raise ClientError(f"help not configured for {next(iter(cls.names))!r}")

    @classmethod
    def help_with_context(cls, verb: str, prefix: list[str]) -> str:
        """Help text given the tokens typed before ``help``.

        Subclasses override this for context-sensitive help: ``trax issue 7
        priority help`` should describe the priority field, not the whole
        Issue verb. The base ignores ``prefix`` and returns the per-verb page.
        """
        del prefix
        return cls.help_text_for(verb)
