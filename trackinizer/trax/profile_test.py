from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import config_dir
from trackinizer.trax import profile
from trackinizer.trax.conftest import FakeClient, run
from trackinizer.trax.profile import Profile


def _redirect_config(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point config_dir at ``root`` so profile writes stay in tmp.

    The module builds every path inline from ``config_dir``, so redirecting
    ``XDG_CONFIG_HOME`` isolates the test through the same resolution
    production uses -- no module global to patch, and no way for the test to
    pass while the real lookup is broken.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    (root / "rekursiv-ai" / "trax" / "profiles").mkdir(parents=True, exist_ok=True)


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare ``trax profile`` lists all profiles, active ``*``-marked.

    Consistent with bare ``trax issue`` listing all rows; there is no
    separate ``list`` action. Detail for one profile is ``trax profile NAME``.
    """
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile(
        "default",
        Profile(url="http://trackinizer.local:8765", author="alice", api_key="secret"),
    )
    profile.save_profile("staging", Profile(url="http://staging:8765"))
    profile.switch_profile("default")
    run(["profile"], FakeClient())
    out = capsys.readouterr().out
    assert "* default" in out
    assert "http://trackinizer.local:8765" in out
    assert "staging" in out


def test_profile_name_displays_named_connection_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("foo", Profile(url="http://foo:8765"))
    run(["profile", "foo"], FakeClient())
    out = capsys.readouterr().out
    assert "profile: foo" in out
    assert "url:     http://foo:8765" in out


def test_profile_field_displays_active_profile_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("default", Profile(url="http://trackinizer.local:8765"))
    run(["profile", "url"], FakeClient())
    assert capsys.readouterr().out == "http://trackinizer.local:8765\n"


def test_profile_name_field_displays_named_profile_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("foo", Profile(url="http://foo:8765", api_key="secret"))
    run(["profile", "foo", "token"], FakeClient())
    assert capsys.readouterr().out == "set (prefix secret)\n"


def test_profile_token_sets_active_profile_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("default", Profile(url="http://trackinizer.local:8765"))
    run(["profile", "token", "to", "secret"], FakeClient())
    assert profile.read_profile("default").api_key == "secret"
    assert "set: profile default token" in capsys.readouterr().out


def test_profile_name_token_sets_named_profile_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("foo", Profile(url="http://foo:8765"))
    run(["profile", "foo", "token", "to", "secret"], FakeClient())
    assert profile.read_profile("foo").api_key == "secret"


def test_profile_url_creates_default_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    run(["profile", "url", "to", "http://trackinizer.local:8765"], FakeClient())
    assert profile.read_profile("default").url == "http://trackinizer.local:8765"


def test_profile_current_selects_existing_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("foo", Profile(url="http://foo:8765"))
    run(["profile", "current", "foo"], FakeClient())
    assert profile.current_profile() == "foo"


def test_profile_token_bootstraps_missing_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A first-write on any missing profile bootstraps with the default URL."""
    _redirect_config(monkeypatch, tmp_path)
    run(["profile", "token", "to", "secret"], FakeClient())
    assert (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default").exists()
    saved = (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default").read_text(
        encoding="utf-8"
    )
    assert "api_key=secret" in saved
    assert "url=" in saved


def test_profile_rejects_old_subcommand_sugar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    profile.save_profile("default", Profile(url="http://trackinizer.local:8765"))
    for argv in (
        ["profile", "show"],
        ["profile", "auth", "set", "secret"],
        ["profile", "add", "foo", "http://foo:8765"],
        ["profile", "switch", "foo"],
    ):
        with pytest.raises(ClientError):
            run(argv, FakeClient())


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _redirect_config(monkeypatch, tmp_path)
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)


def test_current_profile_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert profile.current_profile() == "default"
    (config_dir() / "rekursiv-ai" / "trax" / "current").write_text("from-file\n")
    assert profile.current_profile() == "from-file"
    monkeypatch.setenv("TRACKINIZER_PROFILE", "from-env")
    assert profile.current_profile() == "from-env"


def test_load_profile_reads_current() -> None:
    assert profile.load_profile() == Profile(url="http://127.0.0.1:8765")
    profile.save_profile("default", Profile(url="http://saved:9000/"))
    assert profile.load_profile() == Profile(url="http://saved:9000")
    profile.switch_profile("prod")
    profile.save_profile("prod", Profile(url="http://prod:7000", author="alice"))
    assert profile.load_profile() == Profile(url="http://prod:7000", author="alice")
    assert (
        config_dir() / "rekursiv-ai" / "trax" / "current"
    ).read_text().strip() == "prod"


def test_profile_file_format_round_trip() -> None:
    profile.save_profile("prod", Profile(url="http://x:1", author="alice"))
    text = (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod").read_text()
    assert text == "url=http://x:1\nauthor=alice\n"
    profile.save_profile("dev", Profile(url="http://x:2"))
    assert (
        config_dir() / "rekursiv-ai" / "trax" / "profiles" / "dev"
    ).read_text() == "url=http://x:2\n"


def test_profile_rejects_url_without_scheme() -> None:
    path = config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("url=actor\n")
    with pytest.raises(ClientError, match="invalid URL"):
        profile.load_profile()


def test_profile_api_key_round_trip() -> None:
    profile.save_profile(
        "prod",
        Profile(url="http://x:1", author="alice", api_key="trax_secret_xyz"),
    )
    text = (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod").read_text()
    assert text == "url=http://x:1\nauthor=alice\napi_key=trax_secret_xyz\n"
    loaded = profile.read_profile("prod")
    assert loaded == Profile(
        url="http://x:1", author="alice", api_key="trax_secret_xyz"
    )


def test_profile_file_is_mode_0600() -> None:
    profile.save_profile("prod", Profile(url="http://x:1"))
    path = config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod"
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    profile.save_profile("prod", Profile(url="http://x:1", api_key="trax_topsecret"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_list_and_del_profiles() -> None:
    assert profile.list_profiles() == []
    profile.save_profile("dev", Profile(url="http://dev:1"))
    profile.save_profile("prod", Profile(url="http://prod:2", author="bob"))
    assert profile.list_profiles() == [
        ("dev", Profile(url="http://dev:1")),
        ("prod", Profile(url="http://prod:2", author="bob")),
    ]
    assert profile.del_profile("dev") is True
    assert profile.list_profiles() == [
        ("prod", Profile(url="http://prod:2", author="bob"))
    ]
    assert profile.del_profile("dev") is False


def test_load_profile_raises_when_current_points_to_missing() -> None:
    (config_dir() / "rekursiv-ai" / "trax" / "current").write_text("prod\n")
    with pytest.raises(ClientError, match="profile 'prod' not found"):
        profile.load_profile()


def test_load_profile_raises_when_env_points_to_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKINIZER_PROFILE", "prod")
    with pytest.raises(ClientError, match="profile 'prod' not found"):
        profile.load_profile()


def test_del_active_profile_refused() -> None:
    profile.save_profile("prod", Profile(url="http://prod:1"))
    profile.switch_profile("prod")
    with pytest.raises(ClientError, match="cannot delete active profile"):
        profile.del_profile("prod")
    assert (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod").exists()


# Coverage for context-sensitive help, list/show branches, field reads, and
# storage internals (read_profile, list_profiles, _write_atomic).


def test_profile_help_for_field_subprefix(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["profile", "alpha", "url", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile alpha url" in out


def test_profile_help_for_unknown_token_falls_through(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["profile", "alpha", "beta", "help"], client)
    out = capsys.readouterr().out
    assert "Usage: trax profile" in out


def test_bare_profile_lists_profiles_present(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    profile.save_profile("alpha", Profile(url="http://alpha.example", author="al"))
    profile.save_profile("beta", Profile(url="http://beta.example"))
    run(["profile"], client)
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out


def test_profile_show_named(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    profile.save_profile("alpha", Profile(url="http://alpha.example", author="al"))
    run(["profile", "alpha"], client)
    out = capsys.readouterr().out
    assert "alpha" in out


def test_profile_read_actor(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    profile.save_profile("default", Profile(url="http://x.example", author="bob"))
    run(["profile", "actor"], client)
    assert "bob" in capsys.readouterr().out


def test_profile_read_token_set(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    profile.save_profile("default", Profile(url="http://x.example", api_key="secret"))
    run(["profile", "token"], client)
    out = capsys.readouterr().out
    assert "set" in out


def test_profile_read_token_unset(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    profile.save_profile("default", Profile(url="http://x.example"))
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
    profile.save_profile("alpha", Profile(url="http://x.example"))
    with pytest.raises(ClientError, match="profile set is 'field to value'"):
        run(["profile", "alpha", "url", "http://x"], client)


def test_profile_set_is_one_field_per_command(client: FakeClient) -> None:
    """The multi-pair form is gone: one ``field to value`` per command."""
    profile.save_profile("alpha", Profile(url="http://x.example"))
    with pytest.raises(ClientError, match="profile set is 'field to value'"):
        run(["profile", "alpha", "url", "to", "http://a", "token", "to", "b"], client)


def test_read_profile_ignores_comments_and_blank_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "# comment\n\nurl=http://x.example\n", encoding="utf-8"
    )
    p = profile.read_profile("alpha")
    assert p.url == "http://x.example"


def test_read_profile_missing_url_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "author=bob\n", encoding="utf-8"
    )
    with pytest.raises(ClientError, match="no url= line"):
        profile.read_profile("alpha")


def test_iter_profiles_skips_non_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "subdir").mkdir()
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "url=http://x.example\n"
    )
    names = [n for n, _ in profile.list_profiles()]
    assert "subdir" not in names
    assert "alpha" in names


def test_iter_profiles_silently_drops_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_config(monkeypatch, tmp_path)
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "url=http://x.example\n"
    )
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "broken").write_text(
        "not-a-profile\n"
    )
    names = [n for n, _ in profile.list_profiles()]
    assert "alpha" in names
    assert "broken" not in names


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", r"a\b", "", "."])
def test_profile_name_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    r"""A profile name with ``/``, ``\``, or ``..`` must be rejected.

    ``trax profile ../escape token to X`` would otherwise resolve through
    ``<profiles>/<name>`` and write outside the profiles directory
    (OWASP-class path traversal). Every store entry point that takes a name
    must reject it before any filesystem access.
    """
    _redirect_config(monkeypatch, tmp_path)
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.save_profile(bad, Profile(url="http://x.example"))
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.read_profile(bad)
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.switch_profile(bad)
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.del_profile(bad)
    # No bytes escaped the profiles dir: the parent is untouched.
    assert not (tmp_path.parent / "escape").exists()


def test_write_atomic_cleans_tmp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "target"
    with (
        patch("os.fsync", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        profile._write_atomic(target, "x", mode=0o600)
    # No temp artifact survives the failure, whatever its (now unique) name.
    # The autouse config fixture seeds a sibling org namespace dir.
    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name not in ("target", "rekursiv-ai")
    ]
    assert leftovers == [], f"temp file leaked on failure: {leftovers}"


def test_write_atomic_uses_unique_temp_per_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent writes of one profile must not share a fixed temp name (F27).

    The old fixed ``.{name}.tmp`` + ``O_TRUNC`` let two same-profile writers
    clobber each other's half-written temp before the rename. A unique temp
    name per write keeps each writer's content isolated until its own atomic
    rename. Assert two interleaved writes each land their own bytes and leave
    no temp behind.
    """
    target = tmp_path / "p"
    seen_temps: list[str] = []
    real_replace = Path.replace

    def _spy_replace(self: Path, dst: str | Path) -> Path:
        seen_temps.append(self.name)
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", _spy_replace)
    profile._write_atomic(target, "first\n", mode=0o600)
    profile._write_atomic(target, "second\n", mode=0o600)
    assert target.read_text() == "second\n"
    # Each write used its own distinct temp name (not a shared fixed one).
    assert len(set(seen_temps)) == 2, f"temp names not unique: {seen_temps}"
    # The autouse ``tmp_config_dir`` fixture seeds a sibling org namespace dir.
    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name not in ("p", "rekursiv-ai")
    ]
    assert leftovers == [], f"temp file leaked: {leftovers}"


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
