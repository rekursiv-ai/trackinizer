"""The ``trax profile`` command, over the isolated profile store.

Direct ``Profiles`` calls pin exact output; ``run(["profile", ...])`` drives it
through the CLI dispatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

import argparse

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import config_dir
from trackinizer.trax import profile
from trackinizer.trax.conftest import FakeClient, run
from trackinizer.trax.login import LoginResult
from trackinizer.trax.profile import (
    LOCALHOST_FALLBACK_URL,
    Profile,
    current_profile,
    read_profile,
    save_profile,
    switch_profile,
)
from trackinizer.trax.profiles import Profiles


if TYPE_CHECKING:
    from trackinizer.client.client import Client


def _no_client() -> Client:
    raise AssertionError("trax profile must not build a client")


def _run(*tokens: str) -> None:
    Profiles.run("profile", argparse.Namespace(rest=list(tokens)), _no_client)


def test_parser_collects_positionals() -> None:
    args = Profiles.make_parser().parse_args(["foo", "url"])
    assert cast(list[str], args.rest) == ["foo", "url"]


def test_list_empty(capsys: pytest.CaptureFixture[str]) -> None:
    _run()
    assert capsys.readouterr().out == "(no profiles)\n"


def test_list_marks_active_and_author(capsys: pytest.CaptureFixture[str]) -> None:
    save_profile("alpha", Profile(url="https://a", author="alice"))
    save_profile("b", Profile(url="https://b"))
    switch_profile("b")
    _run()
    assert capsys.readouterr().out == (
        "  alpha  https://a  (as alice)\n* b      https://b\n"
    )


def test_show_named_and_current(capsys: pytest.CaptureFixture[str]) -> None:
    save_profile("foo", Profile(url="https://foo", api_key="trax__abcdefghijklmn"))
    save_profile("default", Profile(url="https://d", author="bob"))
    _run("foo")
    _run("default")
    assert capsys.readouterr().out == (
        "profile: foo\nurl:     https://foo\nactor:   (none)\n"
        "token:   set (prefix trax__abcdef)\n"
        "profile: default\nurl:     https://d\nactor:   bob\ntoken:   unset\n"
    )


def test_set_bootstraps_then_updates(capsys: pytest.CaptureFixture[str]) -> None:
    _run("foo", "actor", "to", "carol")
    assert read_profile("foo") == Profile(url=LOCALHOST_FALLBACK_URL, author="carol")
    _run("foo", "url", "to", "https://x")
    _run("token", "to", "tok")
    assert read_profile("foo") == Profile(url="https://x", author="carol")
    assert read_profile("default").api_key == "tok"
    assert capsys.readouterr().out == (
        "set: profile foo actor\nset: profile foo url\nset: profile default token\n"
    )


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        (("foo", "current"), "unknown profile field 'current'"),
        (("foo", "current", "to", "x"), "unknown profile field 'current'"),
        (("foo", "bogus", "to", "x"), "unknown profile field 'bogus'"),
    ],
)
def test_unknown_fields_are_refused(tokens: tuple[str, ...], message: str) -> None:
    save_profile("foo", Profile(url="https://foo"))
    with pytest.raises(ClientError, match=message):
        _run(*tokens)


def test_current_switches_and_del_removes(capsys: pytest.CaptureFixture[str]) -> None:
    save_profile("foo", Profile(url="https://foo"))
    _run("current", "foo")
    assert current_profile() == "foo"
    save_profile("bar", Profile(url="https://bar"))
    switch_profile("bar")
    _run("foo", "del")
    with pytest.raises(ClientError):
        read_profile("foo")
    assert capsys.readouterr().out == (
        "set: profile current foo\ndeleted: profile foo\n"
    )


def test_current_refuses_unknown_profile() -> None:
    with pytest.raises(ClientError):
        _run("current", "ghost")
    assert current_profile() == "default"


def test_show_or_field_with_no_tokens_shows(capsys: pytest.CaptureFixture[str]) -> None:
    save_profile("foo", Profile(url="https://foo"))
    Profiles.run_field_or_set("foo", [])
    assert capsys.readouterr().out.startswith("profile: foo\n")


class _Wider(Profiles):
    fields: ClassVar[tuple[str, ...]] = (*Profiles.fields, "region")


def test_listed_field_without_a_renderer_is_refused() -> None:
    """A subclass that lists a field ``run_field`` cannot show gets a refusal."""
    save_profile("foo", Profile(url="https://foo"))
    with pytest.raises(ClientError, match="unknown profile field 'region'"):
        _Wider.run_field("foo", "region")


@pytest.mark.parametrize(
    ("prefix", "usage"),
    [
        (["url", "to"], "trax profile url to VALUE"),
        (["foo"], "Usage: trax profile [NAME] [ACTION]"),
    ],
)
def test_help_follows_the_prefix(prefix: list[str], usage: str) -> None:
    assert usage in Profiles.help_with_context("profile", prefix)


def test_profile_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["profile", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile [NAME] [ACTION]" in out
    assert "Examples:" in out
    assert "Fields: url actor token" in out
    assert "Arguments:" not in out
    assert not client.calls


def test_profile_field_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["profile", "url", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile url [to VALUE]" in out
    assert "No VALUE projects the field" in out
    assert not client.calls


def test_profile_field_trailing_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Help is a tail-action verb; '--help' is not infused anywhere."""
    run(["profile", "url", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile url [to VALUE]" in out
    assert "No VALUE projects the field" in out
    assert not client.calls


def test_profile_set_leaf_help_shows_local_forms(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["profile", "foo", "token", "to", "secret", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile foo token to VALUE" in out
    assert "Mutates the selected profile field" in out
    assert not client.calls


def test_bare_profile_lists_all_with_active_marked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare ``trax profile`` lists all profiles, active ``*``-marked.

    Consistent with bare ``trax issue`` listing all rows; there is no
    separate ``list`` action. Detail for one profile is ``trax profile NAME``.
    """
    profile.save_profile(
        "default",
        Profile(url="https://trackinizer.local:8765", author="alice", api_key="secret"),
    )
    profile.save_profile("staging", Profile(url="https://staging:8765"))
    profile.switch_profile("default")
    run(["profile"], FakeClient())
    out = capsys.readouterr().out
    assert "* default" in out
    assert "https://trackinizer.local:8765" in out
    assert "staging" in out


def test_profile_name_displays_named_connection_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("foo", Profile(url="https://foo:8765"))
    run(["profile", "foo"], FakeClient())
    out = capsys.readouterr().out
    assert "profile: foo" in out
    assert "url:     https://foo:8765" in out


def test_profile_field_displays_active_profile_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("default", Profile(url="https://trackinizer.local:8765"))
    run(["profile", "url"], FakeClient())
    assert capsys.readouterr().out == "https://trackinizer.local:8765\n"


def test_profile_name_field_displays_named_profile_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("foo", Profile(url="https://foo:8765", api_key="secret"))
    run(["profile", "foo", "token"], FakeClient())
    assert capsys.readouterr().out == "set (prefix secret)\n"


def test_profile_token_sets_active_profile_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("default", Profile(url="https://trackinizer.local:8765"))
    run(["profile", "token", "to", "secret"], FakeClient())
    assert profile.read_profile("default").api_key == "secret"
    assert "set: profile default token" in capsys.readouterr().out


def test_profile_name_token_sets_named_profile_token() -> None:
    profile.save_profile("foo", Profile(url="https://foo:8765"))
    run(["profile", "foo", "token", "to", "secret"], FakeClient())
    assert profile.read_profile("foo").api_key == "secret"


def test_profile_url_creates_default_profile() -> None:
    run(["profile", "url", "to", "https://trackinizer.local:8765"], FakeClient())
    assert profile.read_profile("default").url == "https://trackinizer.local:8765"


def test_profile_current_selects_existing_profile() -> None:
    profile.save_profile("foo", Profile(url="https://foo:8765"))
    run(["profile", "current", "foo"], FakeClient())
    assert profile.current_profile() == "foo"


def test_profile_token_bootstraps_missing_profile() -> None:
    """A first-write on any missing profile bootstraps with the default URL."""
    run(["profile", "token", "to", "secret"], FakeClient())
    assert (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default").exists()
    saved = (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default").read_text(
        encoding="utf-8",
    )
    assert "api_key=secret" in saved
    assert "url=" in saved


def test_profile_rejects_old_subcommand_sugar() -> None:
    profile.save_profile("default", Profile(url="https://trackinizer.local:8765"))
    for argv in (
        ["profile", "show"],
        ["profile", "auth", "set", "secret"],
        ["profile", "add", "foo", "https://foo:8765"],
        ["profile", "switch", "foo"],
    ):
        with pytest.raises(ClientError):
            run(argv, FakeClient())


def test_profile_help_for_field_subprefix(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["profile", "alpha", "url", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile alpha url" in out


def test_profile_help_for_unknown_token_falls_through(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["profile", "alpha", "beta", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile" in out


def test_bare_profile_lists_profiles_present(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("alpha", Profile(url="https://alpha.example", author="al"))
    profile.save_profile("beta", Profile(url="https://beta.example"))
    run(["profile"], client)
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out


def test_profile_show_named(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("alpha", Profile(url="https://alpha.example", author="al"))
    run(["profile", "alpha"], client)
    out = capsys.readouterr().out
    assert "alpha" in out


def test_profile_read_actor(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("default", Profile(url="https://x.example", author="bob"))
    run(["profile", "actor"], client)
    assert "bob" in capsys.readouterr().out


def test_profile_read_token_set(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("default", Profile(url="https://x.example", api_key="secret"))
    run(["profile", "token"], client)
    out = capsys.readouterr().out
    assert "set" in out


def test_profile_read_token_unset(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile.save_profile("default", Profile(url="https://x.example"))
    run(["profile", "token"], client)
    assert "unset" in capsys.readouterr().out


def test_profile_current_requires_one_arg(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="expected profile name after current"):
        run(["profile", "current"], client)


def test_profile_del_requires_one_arg(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="expected profile name before del"):
        run(["profile", "del"], client)


def test_profile_del_missing_raises(client: FakeClient) -> None:
    with pytest.raises(ClientError, match="not found"):
        run(["profile", "ghost", "del"], client)


def test_profile_set_rejects_bare_adjacency(client: FakeClient) -> None:
    """A set must use ``field to value`` (E007), not bare ``field value``."""
    profile.save_profile("alpha", Profile(url="https://x.example"))
    with pytest.raises(ClientError, match="profile set is 'field to value'"):
        run(["profile", "alpha", "url", "https://x"], client)


def test_profile_set_is_one_field_per_command(client: FakeClient) -> None:
    """The multi-pair form is gone: one ``field to value`` per command."""
    profile.save_profile("alpha", Profile(url="https://x.example"))
    with pytest.raises(ClientError, match="profile set is 'field to value'"):
        run(["profile", "alpha", "url", "to", "https://a", "token", "to", "b"], client)


def test_profile_login_runs_the_device_login_and_names_the_result(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, str]] = []

    def fake_login(url: str, *, profile: str) -> LoginResult:
        asked.append((url, profile))
        return LoginResult(profile=profile, role="writer")

    monkeypatch.setattr("trackinizer.trax.login.login", fake_login)

    run(["profile", "work", "login", "https://trackinizer.example"], client)

    assert asked == [("https://trackinizer.example", "work")]
    assert capsys.readouterr().out == "logged in: profile work (writer)\n"
    assert not client.calls


def test_profile_help_lists_the_login_form(
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["profile", "help"], client)

    assert (
        "trax profile foo login https://trackinizer.example" in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "argv",
    [["work", "login"], ["work", "login", "a", "b"], ["login", "work", "x"]],
)
def test_profile_login_needs_a_name_and_one_url(
    client: FakeClient,
    argv: list[str],
) -> None:
    with pytest.raises(ClientError):
        run(["profile", *argv], client)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
