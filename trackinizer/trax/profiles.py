"""The ``trax profile`` command, backed by the saved-profile store."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, ClassVar, cast, override

import argparse

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.profile import (
    LOCALHOST_FALLBACK_URL,
    Profile,
    current_profile,
    del_profile,
    list_profiles,
    load_profile,
    read_profile,
    save_profile,
    switch_profile,
)
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from trackinizer.client.client import Client
    from trackinizer.trax import login
else:
    from wrapt import lazy_import

    # Only ``trax profile NAME login URL`` polls a server, so the device-login models
    # stay off the cold-start path of every other verb.
    login = lazy_import("trackinizer.trax.login")


class Profiles(Command):
    """Show and mutate saved server profiles, subject-first like the rest of trax."""

    names = ("profile",)

    fields: ClassVar[tuple[str, ...]] = ("url", "actor", "token", "current")

    help = """\
Usage: trax profile [NAME] [ACTION]

Examples:
  trax profile                                     list all profiles (active *)
  trax profile foo                                 show profile foo
  trax profile url                                 show active profile URL
  trax profile url to https://trackinizer.example  set active profile URL
  trax profile token to trax__...                  set active token
  trax profile foo token to trax__...              set profile foo token
  trax profile current foo                         select profile foo
  trax profile foo del                             delete profile foo
  trax profile foo login https://trackinizer.example
                                                   log in with a code you approve
                                                   in a browser; saves profile foo

Fields: url actor token
"""

    field_help: ClassVar[HelpPage] = HelpPage(
        usage="trax profile [NAME] FIELD [to VALUE]",
        summary="No VALUE projects the field; 'to VALUE' mutates it.",
        examples=("trax profile url", "trax profile foo token to trax__..."),
    )

    field_set_help: ClassVar[HelpPage] = HelpPage(
        usage="trax profile [NAME] FIELD to VALUE",
        summary="Mutates the selected profile field (one field per command).",
        examples=("trax profile token to trax__...",),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="trax profile",
            description="Show or mutate saved server profiles.",
        )
        parser.add_argument("rest", nargs="*", metavar="POS")
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
        tokens = cast(Sequence[str], args.rest)
        if not tokens:
            # Bare verb lists all profiles, like ``trax issue`` lists rows; the
            # active one is ``*``-marked. ``trax profile <name>`` shows detail.
            cls.run_list()
            return
        if tokens[-1] == "del":
            cls.run_del(tokens[:-1])
            return
        if tokens[0] == "current":
            cls.run_current(tokens[1:])
            return
        if len(tokens) == 3 and tokens[1] == "login":
            cls.run_login(tokens[0], url=tokens[2])
            return
        if tokens[0] in cls.fields:
            cls.run_field_or_set(current_profile(), tokens)
            return
        if len(tokens) == 1:
            cls.run_show(tokens[0])
            return
        cls.run_field_or_set(tokens[0], tokens[1:])

    @classmethod
    @override
    def help_with_context(cls, verb: str, prefix: list[str]) -> str:
        del verb
        return cls.profile_help(prefix)

    @classmethod
    def profile_help(cls, tokens: Sequence[str]) -> str:
        """Help for.

        Args:
          tokens: Command tokens to resolve help for.

        Returns:
          result: Rendered help text with usage instructions.

        """
        if not tokens:
            return cls.help_text()
        if tokens[0] in cls.fields:
            return cls._field_help(tokens[0], value=len(tokens) > 1)
        if len(tokens) == 1:
            return cls.help_text()
        if len(tokens) >= 2 and tokens[1] in cls.fields:
            prefix = f"{tokens[0]} {tokens[1]}"
            return cls._field_help(prefix, value=len(tokens) > 2)
        return cls.help_text()

    @classmethod
    def _field_help(cls, prefix: str, *, value: bool) -> str:
        page = cls.field_set_help if value else cls.field_help
        suffix = "to VALUE" if value else "[to VALUE]"
        return page.with_usage(f"trax profile {prefix} {suffix}").render()

    @classmethod
    def run_list(cls) -> None:
        """Run list."""
        rows = list_profiles()
        if not rows:
            echo("(no profiles)")
            return
        active = current_profile()
        width = max(len(name) for name, _ in rows)
        for name, profile in rows:
            marker = "*" if name == active else " "
            as_who = f"  (as {profile.author})" if profile.author else ""
            echo(f"{marker} {name.ljust(width)}  {profile.url}{as_who}")

    @classmethod
    def run_show(cls, name: str) -> None:
        """Run show.

        Args:
          name: Profile name to display.

        """
        profile = load_profile() if name == current_profile() else read_profile(name)
        echo(f"profile: {name}")
        echo(f"url:     {profile.url}")
        echo(f"actor:   {profile.author or '(none)'}")
        if profile.api_key:
            echo(f"token:   set (prefix {profile.api_key[:12]})")
        else:
            echo("token:   unset")

    @classmethod
    def run_field_or_set(cls, name: str, tokens: Sequence[str]) -> None:
        """Run field or set.

        Args:
          name: Profile name to query or modify.
          tokens: Command tokens (empty=show, one=get field, three=set field).

        """
        if not tokens:
            cls.run_show(name)
            return
        if len(tokens) == 1:
            cls.run_field(name, tokens[0])
            return
        # Set mirrors the row grammar exactly: ``field to value`` (GRAMMAR.md
        # §4/§8). One field per command, like a row scalar edit -- no bare
        # adjacency, no multi-pair.
        if len(tokens) != 3 or tokens[1] != "to":
            raise ClientError(
                f"expected '{tokens[0]} to VALUE'; profile set is 'field to value'",
            )
        cls.run_set(name, field=tokens[0], value=tokens[2])

    @classmethod
    def run_field(cls, name: str, field: str) -> None:
        """Run field.

        Args:
          name: Profile name to query.
          field: Field name to display (url, actor, or token).

        """
        if field not in cls.fields or field == "current":
            raise ClientError(f"unknown profile field {field!r}")
        profile = load_profile() if name == current_profile() else read_profile(name)
        if field == "url":
            echo(profile.url)
            return
        if field == "actor":
            echo(profile.author)
            return
        if field == "token":
            if profile.api_key:
                echo(f"set (prefix {profile.api_key[:12]})")
            else:
                echo("unset")
            return
        raise ClientError(f"unknown profile field {field!r}")

    @classmethod
    def run_set(cls, name: str, *, field: str, value: str) -> None:
        """Run set.

        Args:
          name: Profile name to modify.
          field: Field name to set (url, actor, or token).
          value: New value for the field.

        """
        if field not in cls.fields or field == "current":
            raise ClientError(f"unknown profile field {field!r}")
        profile = cls._profile_for_write(name)
        save_profile(
            name,
            Profile(
                url=value if field == "url" else profile.url,
                author=value if field == "actor" else profile.author,
                api_key=value if field == "token" else profile.api_key,
            ),
        )
        echo(f"set: profile {name} {field}")

    @classmethod
    def run_current(cls, tokens: Sequence[str]) -> None:
        """Run current."""
        if len(tokens) != 1:
            raise ClientError("expected profile name after current")
        read_profile(tokens[0])
        switch_profile(tokens[0])
        echo(f"set: profile current {tokens[0]}")

    @classmethod
    def run_login(cls, name: str, *, url: str) -> None:
        """Log in to ``url`` by a code approved in a browser; save the key as ``name``.

        Args:
          name: Profile name to save the key under.
          url: The server's URL.

        """
        result = login.login(url, profile=name)
        echo(f"logged in: profile {result.profile} ({result.role})")

    @classmethod
    def run_del(cls, tokens: Sequence[str]) -> None:
        """Run del."""
        if len(tokens) != 1:
            raise ClientError("expected profile name before del")
        if not del_profile(tokens[0]):
            raise ClientError(f"profile {tokens[0]!r} not found")
        echo(f"deleted: profile {tokens[0]}")

    # Before a profile exists, the first field-set bootstraps it; the URL defaults to
    # the localhost fallback until the user sets one.
    @classmethod
    def _profile_for_write(cls, name: str) -> Profile:
        """Return the existing profile, or a localhost-URL template on first write."""
        try:
            return read_profile(name)
        except ClientError:
            return Profile(url=LOCALHOST_FALLBACK_URL)
