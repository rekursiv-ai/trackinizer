from __future__ import annotations

from typing import Any, cast

import argparse

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage


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
    assert _Cmd.help_text_for("widget") == "widget help text\n"


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
        _Cmd.run("norun", argparse.Namespace(), cast(Any, lambda: None))
