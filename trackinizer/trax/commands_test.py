from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, override

import argparse

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from collections.abc import Callable

    from trackinizer.client.client import Client


def test_help_page_render_includes_required_sections() -> None:
    page = HelpPage(
        usage="trax foo BAR",
        summary="One-line summary.",
        arguments=(("BAR", "the bar"),),
        options=(("--flag", "a flag"),),
        examples=("trax foo 1", "trax foo 2"),
        notes=("first note",),
    )
    out = page.render()
    assert out.startswith("Usage: trax foo BAR\n")
    assert "One-line summary." in out
    assert "Arguments:" in out
    assert "  BAR  the bar" in out
    assert "Options:" in out
    assert "  --flag  a flag" in out
    assert "Examples:" in out
    assert "  trax foo 1" in out
    assert "Notes:" in out
    assert "  first note" in out


def test_help_page_render_omits_empty_sections() -> None:
    page = HelpPage(usage="trax bare", summary="only summary")
    out = page.render()
    assert "Arguments:" not in out
    assert "Options:" not in out
    assert "Examples:" not in out
    assert "Notes:" not in out


def test_help_page_with_usage_keeps_other_fields() -> None:
    base = HelpPage(
        usage="trax foo",
        summary="s",
        arguments=(("BAR", "the bar"),),
        examples=("ex",),
    )
    derived = base.with_usage("trax foo BAR")
    assert derived.usage == "trax foo BAR"
    assert derived.summary == "s"
    assert derived.arguments == (("BAR", "the bar"),)
    assert derived.examples == ("ex",)


def test_help_page_with_usage_can_override_examples() -> None:
    base = HelpPage(usage="trax foo", summary="s", examples=("orig",))
    derived = base.with_usage("trax foo BAR", examples=("new-1", "new-2"))
    assert derived.examples == ("new-1", "new-2")


def test_command_base_help_text_for_handles_string_help() -> None:
    class _Cmd(Command):
        names = ("widget",)
        help = "widget help text\n"

    assert _Cmd.help_text() == "widget help text\n"
    assert _Cmd.verb_help_text("widget") == "widget help text\n"


def test_command_base_help_text_raises_when_unconfigured() -> None:
    class _Cmd(Command):
        names = ("nohelp",)

    with pytest.raises(ClientError, match="help not configured"):
        _Cmd.help_text()


def test_command_base_matches_returns_true_for_registered_name() -> None:
    class _Cmd(Command):
        names = ("alpha", "beta")

    assert _Cmd.matches("alpha")
    assert _Cmd.matches("beta")
    assert not _Cmd.matches("gamma")


def test_command_base_make_parser_raises_not_implemented() -> None:
    class _Cmd(Command):
        names = ("noparser",)

    with pytest.raises(NotImplementedError):
        _Cmd.make_parser()


def test_command_base_run_raises_not_implemented() -> None:
    class _Cmd(Command):
        names = ("norun",)

    with pytest.raises(NotImplementedError):
        _Cmd.run("norun", argparse.Namespace(), lambda: pytest.fail("never called"))


def test_help_page_render_complete_layout() -> None:
    page = HelpPage(
        usage="trax foo BAR",
        summary="Choose a bar.",
        arguments=(("BAR", "first argument"), ("LONGER", "second argument")),
        options=(("-v", "verbose"), ("--quiet", "quiet")),
        examples=("trax foo 1", "trax foo 2"),
        notes=("first note", "last note  "),
    )
    assert page.render() == (
        "Usage: trax foo BAR\n\nChoose a bar.\n\n"
        "Arguments:\n  BAR     first argument\n  LONGER  second argument\n\n"
        "Options:\n  -v       verbose\n  --quiet  quiet\n\n"
        "Examples:\n  trax foo 1\n  trax foo 2\n\n"
        "Notes:\n  first note\n  last note\n"
    )


def test_help_page_render_options_without_arguments() -> None:
    page = HelpPage(
        usage="trax foo",
        summary="Choose an option.",
        options=(("--flag", "enable the flag"),),
    )
    assert page.render() == (
        "Usage: trax foo\n\nChoose an option.\n\nOptions:\n  --flag  enable the flag\n"
    )


def test_help_page_with_usage_preserves_complete_page() -> None:
    page = HelpPage(
        usage="trax foo",
        summary="Choose a bar.",
        arguments=(("BAR", "the bar"),),
        options=(("--flag", "a flag"),),
        examples=("trax foo 1",),
        notes=("Keep this note.",),
    )
    assert page.with_usage("trax foo BAR") == HelpPage(
        usage="trax foo BAR",
        summary="Choose a bar.",
        arguments=(("BAR", "the bar"),),
        options=(("--flag", "a flag"),),
        examples=("trax foo 1",),
        notes=("Keep this note.",),
    )
    assert page.usage == "trax foo"


def test_help_page_with_usage_empty_examples_overrides_originals() -> None:
    page = HelpPage(usage="trax foo", summary="Summary.", examples=("example",))
    assert page.with_usage("trax foo BAR", examples=()).examples == ()
    assert page.examples == ("example",)


def test_command_help_text_uses_first_alias() -> None:
    assert _VerbCommand.help_text() == "alpha help\n"
    assert _VerbCommand.verb_help_text("beta") == "beta help\n"


def test_command_base_help_with_context_uses_requested_verb() -> None:
    assert _VerbCommand.help_with_context("beta", ["7", "priority"]) == "beta help\n"


def test_command_base_help_renders_help_page() -> None:
    class _PageCommand(Command):
        names = ("page",)
        help = HelpPage(usage="trax page", summary="Page summary.")

    assert _PageCommand.help_text() == "Usage: trax page\n\nPage summary.\n"


@pytest.mark.parametrize("rest", [[], ["7", "priority"]])
def test_command_dispatch_parses_and_runs(
    rest: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ParsingCommand.dispatch("beta", rest, _refuse_client)
    captured = capsys.readouterr()
    assert captured.out == f"beta Namespace(tokens={rest!r})\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("rest", "prefix"),
    [
        (["help"], []),
        (["7", "priority", "help"], ["7", "priority"]),
        (["--help"], []),
        (["-h"], []),
        (["--help", "7", "priority"], ["7", "priority"]),
        (["-h", "7", "priority"], ["7", "priority"]),
        (["7", "priority", "--help"], ["7", "priority"]),
        (["7", "priority", "-h"], ["7", "priority"]),
    ],
)
def test_command_dispatch_context_help_without_client(
    rest: list[str],
    prefix: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ContextCommand.dispatch("beta", rest, _refuse_client)
    captured = capsys.readouterr()
    assert captured.out == f"beta context {prefix!r}\n"
    assert captured.err == ""


def test_command_dispatch_bare_help_uses_base_context_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _VerbCommand.dispatch("beta", ["help"], _refuse_client)
    captured = capsys.readouterr()
    assert captured.out == "beta help\n"
    assert captured.err == ""


def test_command_dispatch_middle_help_remains_argparse_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        _ContextCommand.dispatch("beta", ["7", "--help", "priority"], _refuse_client)
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: trax sample ")
    assert "positional arguments:" in captured.out
    assert "beta context" not in captured.out
    assert captured.err == ""


class _VerbCommand(Command):
    names = ("alpha", "beta")

    @classmethod
    @override
    def verb_help_text(cls, verb: str) -> str:
        return f"{verb} help\n"


class _ParsingCommand(Command):
    names = ("alpha", "beta")

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax sample")
        _add_arguments(parser)
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del client_factory
        echo(f"{verb} {args}")


class _ContextCommand(_ParsingCommand):
    @classmethod
    @override
    def help_with_context(cls, verb: str, prefix: list[str]) -> str:
        return f"{verb} context {prefix!r}\n"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("tokens", nargs="*")


def _refuse_client() -> NoReturn:
    pytest.fail("Help and argument parsing must not create a client.")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
