from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.lib.userdirs import config_dir
from trackinizer.trax import profile
from trackinizer.trax.profile import Profile


def test_profile_store_uses_shared_user_directory_isolation(
    isolate_user_dirs: Path | None,
    tmp_path: Path,
) -> None:
    assert isolate_user_dirs is not None
    assert config_dir() == isolate_user_dirs / "config"
    assert (config_dir() / "rekursiv-ai" / "trax" / "profiles").is_dir()
    assert list(tmp_path.iterdir()) == []


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
    profile.save_profile("default", Profile(url="https://saved:9000/"))
    assert profile.load_profile() == Profile(url="https://saved:9000")
    profile.switch_profile("prod")
    profile.save_profile("prod", Profile(url="https://prod:7000", author="alice"))
    assert profile.load_profile() == Profile(url="https://prod:7000", author="alice")
    assert (
        config_dir() / "rekursiv-ai" / "trax" / "current"
    ).read_text().strip() == "prod"


def test_profile_file_format_round_trip() -> None:
    profile.save_profile("prod", Profile(url="https://x:1", author="alice"))
    text = (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod").read_text()
    assert text == "url=https://x:1\nauthor=alice\n"
    profile.save_profile("dev", Profile(url="https://x:2"))
    assert (
        config_dir() / "rekursiv-ai" / "trax" / "profiles" / "dev"
    ).read_text() == "url=https://x:2\n"


def test_profile_rejects_url_without_scheme() -> None:
    path = config_dir() / "rekursiv-ai" / "trax" / "profiles" / "default"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("url=actor\n")
    with pytest.raises(ClientError, match="invalid URL"):
        profile.load_profile()


def test_profile_api_key_round_trip() -> None:
    profile.save_profile(
        "prod",
        Profile(url="https://x:1", author="alice", api_key="trax_secret_xyz"),
    )
    text = (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod").read_text()
    assert text == "url=https://x:1\nauthor=alice\napi_key=trax_secret_xyz\n"
    loaded = profile.read_profile("prod")
    assert loaded == Profile(
        url="https://x:1",
        author="alice",
        api_key="trax_secret_xyz",
    )


def test_profile_file_is_mode_0600() -> None:
    profile.save_profile("prod", Profile(url="https://x:1"))
    path = config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod"
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    profile.save_profile("prod", Profile(url="https://x:1", api_key="trax_topsecret"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_current_profile_pointer_is_mode_0600() -> None:
    profile.save_profile("prod", Profile(url="https://x:1"))
    profile.switch_profile("prod")
    path = config_dir() / "rekursiv-ai" / "trax" / "current"
    assert path.read_text() == "prod\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_list_and_del_profiles() -> None:
    assert profile.list_profiles() == []
    profile.save_profile("dev", Profile(url="https://dev:1"))
    profile.save_profile("prod", Profile(url="https://prod:2", author="bob"))
    assert profile.list_profiles() == [
        ("dev", Profile(url="https://dev:1")),
        ("prod", Profile(url="https://prod:2", author="bob")),
    ]
    assert profile.del_profile("dev") is True
    assert profile.list_profiles() == [
        ("prod", Profile(url="https://prod:2", author="bob")),
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
    profile.save_profile("prod", Profile(url="https://prod:1"))
    profile.switch_profile("prod")
    with pytest.raises(ClientError, match="cannot delete active profile"):
        profile.del_profile("prod")
    assert (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "prod").exists()


def test_read_profile_ignores_comments_and_blank_lines() -> None:
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "# comment\n\nurl=https://x.example\n",
        encoding="utf-8",
    )
    p = profile.read_profile("alpha")
    assert p.url == "https://x.example"


def test_read_profile_of_an_unsaved_name_raises() -> None:
    with pytest.raises(ClientError, match="profile 'ghost' not found"):
        profile.read_profile("ghost")


def test_read_profile_missing_url_raises() -> None:
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "author=bob\n",
        encoding="utf-8",
    )
    with pytest.raises(ClientError, match="no url= line"):
        profile.read_profile("alpha")


def test_iter_profiles_skips_non_files() -> None:
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "subdir").mkdir()
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "url=https://x.example\n",
    )
    names = [n for n, _ in profile.list_profiles()]
    assert "subdir" not in names
    assert "alpha" in names


def test_iter_profiles_silently_drops_unreadable() -> None:
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "alpha").write_text(
        "url=https://x.example\n",
    )
    (config_dir() / "rekursiv-ai" / "trax" / "profiles" / "broken").write_text(
        "not-a-profile\n",
    )
    names = [n for n, _ in profile.list_profiles()]
    assert "alpha" in names
    assert "broken" not in names


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", r"a\b", "", "."])
def test_profile_name_rejects_path_traversal(bad: str) -> None:
    r"""A profile name with ``/``, ``\``, or ``..`` must be rejected.

    ``trax profile ../escape token to X`` would otherwise resolve through
    ``<profiles>/<name>`` and write outside the profiles directory
    (OWASP-class path traversal). Every store entry point that takes a name
    must reject it before any filesystem access.
    """
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.save_profile(bad, Profile(url="https://x.example"))
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.read_profile(bad)
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.switch_profile(bad)
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.del_profile(bad)
    # No bytes escaped the profiles dir: the parent is untouched.
    assert not (config_dir() / "rekursiv-ai" / "trax" / "escape").exists()


def test_write_atomic_cleans_tmp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "atomic" / "target"
    with (
        patch("os.fsync", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        profile._write_atomic(target, "x", mode=0o600)
    # No temp artifact survives the failure, whatever its (now unique) name.
    assert list(target.parent.iterdir()) == []


def test_write_atomic_cleans_tmp_when_the_rename_fails(tmp_path: Path) -> None:
    target = tmp_path / "atomic" / "target"
    (target / "occupied").mkdir(parents=True)

    with pytest.raises(OSError, match="target"):
        profile._write_atomic(target, "x", mode=0o600)

    assert list(target.parent.iterdir()) == [target]


def test_list_profiles_is_empty_without_a_profiles_directory() -> None:
    (config_dir() / "rekursiv-ai" / "trax" / "profiles").rmdir()
    assert profile.list_profiles() == []


def test_write_atomic_uses_unique_temp_per_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent writes of one profile must not share a fixed temp name (F27).

    The old fixed ``.{name}.tmp`` + ``O_TRUNC`` let two same-profile writers
    clobber each other's half-written temp before the rename. A unique temp
    name per write keeps each writer's content isolated until its own atomic
    rename. Assert two interleaved writes each land their own bytes and leave
    no temp behind.
    """
    target = tmp_path / "atomic" / "p"
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
    assert list(target.parent.iterdir()) == [target]


def _save(name: str, url: str) -> None:
    profile.save_profile(name, Profile(url=url, api_key="trax_k"))


def test_a_shell_from_a_terminal_uses_the_saved_profile_for_its_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    _save("hosted", "https://trax.test")
    _save("local", "http://127.0.0.1:8765")
    monkeypatch.setenv("LC_TRAX_SERVER", "https://trax.test/")

    assert profile.load_profile().url == "https://trax.test"
    assert profile.unmatched_server_hint() is None


def test_an_explicit_profile_or_url_beats_the_server_a_terminal_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save("hosted", "https://trax.test")
    _save("local", "http://127.0.0.1:8765")
    monkeypatch.setenv("LC_TRAX_SERVER", "https://trax.test")
    monkeypatch.setenv("TRACKINIZER_PROFILE", "local")

    assert profile.load_profile().url == "http://127.0.0.1:8765"
    monkeypatch.delenv("TRACKINIZER_PROFILE")
    profile.switch_profile("local")
    assert profile.load_profile().url == "http://127.0.0.1:8765"


def test_a_server_without_a_saved_profile_gets_the_login_hint_and_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    monkeypatch.delenv("TRACKINIZER_URL", raising=False)
    _save("local", "http://127.0.0.1:8765")
    monkeypatch.setenv("LC_TRAX_SERVER", "https://other.test")

    hint = profile.unmatched_server_hint()

    assert hint is not None
    assert "trax profile remote url to https://other.test, then add a token" in hint
    assert "from Settings" in hint
    assert "token to" not in hint
    assert profile.load_profile().url == "http://127.0.0.1:8765"
    monkeypatch.setenv("TRACKINIZER_URL", "https://x.test")
    assert profile.unmatched_server_hint() is None


@pytest.mark.parametrize(
    "server",
    [
        "ftp://x.test",
        "https://user:pw@x.test",
        "https://x.test/a b",
        "https://x.test/\x1b[31m",
        "javascript:alert(1)",
        "https://",
        "x.test",
        "https://[::1",
    ],
)
def test_a_server_that_is_not_a_sound_url_is_ignored_and_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
    server: str,
) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    monkeypatch.delenv("TRACKINIZER_URL", raising=False)
    _save("hosted", "https://x.test")
    monkeypatch.setenv("LC_TRAX_SERVER", server)

    assert profile.terminal_server() is None
    assert profile.unmatched_server_hint() is None
    assert profile.load_profile().url != server


def test_the_hint_quotes_a_server_that_holds_a_shell_metacharacter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    monkeypatch.delenv("TRACKINIZER_URL", raising=False)
    monkeypatch.setenv("LC_TRAX_SERVER", "https://x.test/;rm")

    hint = profile.unmatched_server_hint()

    assert hint is not None
    assert "'https://x.test/;rm'" in hint


def test_a_usable_profile_is_a_pinned_one_or_a_saved_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)
    assert not profile.has_usable_profile()

    profile.save_profile("work", Profile(url="https://work:9000"))
    assert not profile.has_usable_profile()

    profile.switch_profile("work")
    assert profile.has_usable_profile()


def test_a_saved_default_profile_is_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACKINIZER_PROFILE", raising=False)

    profile.save_profile("default", Profile(url="https://work:9000"))

    assert profile.has_usable_profile()


def test_a_profile_set_in_the_environment_is_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKINIZER_PROFILE", "lab")

    assert profile.has_usable_profile()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a b"])
def test_a_profile_name_that_could_leave_the_directory_is_refused(name: str) -> None:
    with pytest.raises(ClientError, match="invalid profile name"):
        profile.validate_profile_name(name)


def test_profile_repr_and_str_hold_no_key_text() -> None:
    """A traceback or log line that shows a ``Profile`` must not print its key."""
    key = "tok-SENTINEL-7b41d9"
    shown = Profile(url="https://prod:9000", author="alice", api_key=key)

    assert key not in repr(shown)
    assert key not in str(shown)
    assert shown != Profile(url="https://prod:9000", author="alice", api_key="other")


def test_read_missing_profile_names_it_and_preserves_the_cause() -> None:
    with pytest.raises(ClientError, match="profile 'missing' not found") as error:
        profile.read_profile("missing")

    assert isinstance(error.value.__cause__, FileNotFoundError)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
