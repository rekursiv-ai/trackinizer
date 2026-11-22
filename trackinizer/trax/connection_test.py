"""The connection chain: flags, environment and saved profile to one ``Client``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import argparse

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import connection, profile
from trackinizer.trax.client_cache import Target, close_clients
from trackinizer.trax.profile import Profile


if TYPE_CHECKING:
    from pathlib import Path


def test_profile_flag_overrides_trackinizer_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.save_profile("prod", Profile(url="https://prod:9000", author="alice"))
    monkeypatch.setenv("TRACKINIZER_URL", "https://env:8766")
    client = connection.connect(
        argparse.Namespace(profile="prod", host=None, port=None),
    )
    try:
        assert client.base_url == "https://prod:9000"
        assert client.author == "alice"
    finally:
        close_clients()


_KEY: Final = "tok-SENTINEL-93f1c7"
"""A recognisable key, so a leak is found by substring."""


@pytest.fixture(autouse=True)
def no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub an exported ``TRACKINIZER_TOKEN`` so it cannot bleed into these tests."""
    monkeypatch.delenv("TRACKINIZER_TOKEN", raising=False)
    monkeypatch.delenv("TRACKINIZER_TOKEN_FILE", raising=False)


def _resolve(*, profile_name: str | None = None, port: int | None = None) -> Target:
    return connection.resolve_target(
        argparse.Namespace(profile=profile_name, host=None, port=port),
    )


class TestTokenEnvironment:
    """``TRACKINIZER_TOKEN`` with ``TRACKINIZER_URL`` names a server and a key.

    Without a URL the token is ignored.
    """

    @pytest.mark.parametrize(
        ("profile_flag", "env_pairs", "expected"),
        [
            pytest.param(
                "prod",
                {"TRACKINIZER_URL": "https://env:1", "TRACKINIZER_TOKEN": _KEY},
                Target(url="https://prod:9000", author="alice", api_key="profile-key"),
                id="row1-profile-flag-ignores-env-token",
            ),
            pytest.param(
                None,
                {
                    "TRACKINIZER_URL": "https://env:1",
                    "TRACKINIZER_TOKEN": _KEY,
                    "TRACKINIZER_PROFILE": "prod",
                },
                Target(url="https://env:1", author="", api_key=_KEY),
                id="row2-env-url-and-token",
            ),
            pytest.param(
                None,
                {"TRACKINIZER_URL": "https://env:1", "TRACKINIZER_PROFILE": "prod"},
                Target(url="https://env:1", author="", api_key=""),
                id="row3-env-url-alone-has-no-key",
            ),
            pytest.param(
                None,
                {"TRACKINIZER_PROFILE": "prod"},
                Target(url="https://prod:9000", author="alice", api_key="profile-key"),
                id="row4-profile-environment",
            ),
        ],
    )
    def test_token_precedence_rows_1_to_4(
        self,
        monkeypatch: pytest.MonkeyPatch,
        profile_flag: str | None,
        env_pairs: dict[str, str],
        expected: Target,
    ) -> None:
        profile.save_profile(
            "prod",
            Profile(url="https://prod:9000", author="alice", api_key="profile-key"),
        )
        for name, value in env_pairs.items():
            monkeypatch.setenv(name, value)

        assert _resolve(profile_name=profile_flag) == expected

    def test_host_flags_rewrite_the_env_url_and_keep_the_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve(port=9) == Target(url="https://env:9", author="", api_key=_KEY)

    def test_token_without_url_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An ops shell exports only the token; the profile's own key stays in use."""
        profile.save_profile(
            "prod",
            Profile(url="https://prod:9000", author="alice", api_key="profile-key"),
        )
        profile.switch_profile("prod")
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        target = _resolve()
        try:
            client = connection.connect(
                argparse.Namespace(profile=None, host=None, port=None),
            )
            sent_key = client.api_key
        finally:
            close_clients()

        assert target == Target(
            url="https://prod:9000",
            author="alice",
            api_key="profile-key",
        )
        assert sent_key == "profile-key"
        assert capsys.readouterr().err == ""

    def test_token_without_url_falls_through_to_the_profile_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile.save_profile("a", Profile(url="https://a:1", api_key="key-a"))
        profile.save_profile("b", Profile(url="https://b:1", api_key="key-b"))
        profile.switch_profile("a")
        monkeypatch.setenv("TRACKINIZER_PROFILE", "b")
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve() == Target(url="https://b:1", author="", api_key="key-b")

    def test_token_without_url_and_without_profile_sends_no_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve().api_key == ""

    def test_an_empty_token_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", "")

        assert _resolve() == Target(url="https://env:1", author="", api_key="")

    @pytest.mark.parametrize(
        "bad_key",
        [f"{_KEY} trailing", f"{_KEY}\n", f"{_KEY}\u00e9"],
        ids=["space", "newline", "non-ascii"],
    )
    def test_a_malformed_token_is_refused_without_echoing_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bad_key: str,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", bad_key)

        with pytest.raises(ClientError, match="printable ASCII") as caught:
            _ = _resolve()

        assert str(caught.value) == (
            "TRACKINIZER_TOKEN must be printable ASCII with no whitespace"
        )

    def test_profile_flag_needs_no_url_for_an_unused_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile.save_profile("prod", Profile(url="https://prod:9000"))
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve(profile_name="prod").url == "https://prod:9000"


class TestTokenFile:
    """``TRACKINIZER_TOKEN_FILE`` names the key's file, as systemd's ``LoadCredential`` hands it."""

    def test_the_key_is_read_from_the_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "key").write_text(f"{_KEY}\n")
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN_FILE", str(tmp_path / "key"))

        assert _resolve() == Target(url="https://env:1", author="", api_key=_KEY)

    def test_the_token_wins_over_the_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "key").write_text("from-the-file\n")
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)
        monkeypatch.setenv("TRACKINIZER_TOKEN_FILE", str(tmp_path / "key"))

        assert _resolve().api_key == _KEY

    def test_a_file_that_cannot_be_read_is_one_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        missing = tmp_path / "missing"
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN_FILE", str(missing))

        with pytest.raises(ClientError) as caught:
            _ = _resolve()

        assert (
            str(caught.value)
            == f"TRACKINIZER_TOKEN_FILE {missing}: No such file or directory"
        )
        assert caught.value.__cause__ is None

    def test_a_malformed_key_in_the_file_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "key").write_text(f"{_KEY} trailing\n")
        monkeypatch.setenv("TRACKINIZER_URL", "https://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN_FILE", str(tmp_path / "key"))

        with pytest.raises(ClientError) as caught:
            _ = _resolve()

        assert str(caught.value) == (
            f"TRACKINIZER_TOKEN_FILE {tmp_path / 'key'} must hold printable ASCII "
            "with no whitespace"
        )


class TestClientSharing:
    """A long-lived process must not build a connection pool per invocation.

    The daemon calls ``parse_and_run`` once per request. A client cached
    inside that call would open a fresh ``httpx2`` pool -- and a fresh TCP
    handshake -- every time, discarding the keep-alive the transport exists
    to provide, and would accumulate open sockets for the daemon's whole life.
    """

    def test_reuses_one_client_across_invocations(self) -> None:
        profile.save_profile("prod", Profile(url="https://prod:9000"))
        args = argparse.Namespace(profile="prod", host=None, port=None)
        try:
            clients = [connection.connect(args) for _ in range(5)]

            assert len({id(client) for client in clients}) == 1, (
                "each invocation built its own Client; under the daemon that "
                "is a new connection pool and a leaked socket per request"
            )
        finally:
            close_clients()

    def test_separates_clients_by_resolved_target(self) -> None:
        """Two profiles are two servers; they must not share a connection."""
        profile.save_profile("a", Profile(url="https://a:9000"))
        profile.save_profile("b", Profile(url="https://b:9000"))
        try:
            first = connection.connect(
                argparse.Namespace(profile="a", host=None, port=None),
            )
            second = connection.connect(
                argparse.Namespace(profile="b", host=None, port=None),
            )

            assert first is not second
        finally:
            close_clients()

    def test_a_profile_rewrite_invalidates_the_cached_client(self) -> None:
        """A cached client still carries the OLD token after a token change."""
        profile.save_profile("prod", Profile(url="https://prod:9000", api_key="old"))
        args = argparse.Namespace(profile="prod", host=None, port=None)
        try:
            before = connection.connect(args)
            profile.save_profile(
                "prod",
                Profile(url="https://prod:9000", api_key="new"),
            )
            after = connection.connect(args)

            assert before is not after
            assert after.api_key == "new"
        finally:
            close_clients()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Trax.Example", "trax.example"),
        ("http://127.0.0.1:8765/api", "127.0.0.1_8765"),
        ("https://trax.example:443", "trax.example_443"),
    ],
)
def test_a_server_has_one_directory_named_by_its_host_and_port(
    url: str,
    expected: str,
) -> None:
    assert connection.server_dir(url) == expected


def test_connect_flags_parse_profile_host_and_port() -> None:
    parser = argparse.ArgumentParser()
    connection.connect_flags(parser)

    assert parser.parse_args([]) == argparse.Namespace(
        profile=None,
        host=None,
        port=None,
    )
    assert parser.parse_args(
        ["--profile", "prod", "--host", "h", "--port", "9"],
    ) == argparse.Namespace(profile="prod", host="h", port=9)


def test_a_url_that_names_no_host_has_no_directory() -> None:
    with pytest.raises(ClientError, match="names no server host"):
        _ = connection.server_dir("not a url")


def test_connection_flags_parse_defaults_and_explicit_values() -> None:
    parser = argparse.ArgumentParser()
    connection.connect_flags(parser)

    assert vars(parser.parse_args([])) == {"profile": None, "host": None, "port": None}
    flags = parser.parse_args(["--profile", "lab", "--host", "server", "--port", "9"])
    assert vars(flags) == {"profile": "lab", "host": "server", "port": 9}


@pytest.mark.parametrize("token", ["secret\n", "secret value", "secreté"])
def test_malformed_environment_key_is_refused_without_echoing(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    monkeypatch.setenv("TRACKINIZER_URL", "https://server.test")
    monkeypatch.setenv("TRACKINIZER_TOKEN", token)

    with pytest.raises(ClientError, match="printable ASCII") as error:
        _resolve()

    assert token not in str(error.value)
    assert "secret" not in str(error.value)


def test_a_plain_http_env_url_is_refused_before_its_key_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TRACKINIZER_URL=http://...`` would send ``TRACKINIZER_TOKEN`` in clear."""
    monkeypatch.setenv("TRACKINIZER_URL", "http://server.example")
    monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

    with pytest.raises(ClientError, match=r"TRACKINIZER_URL .* is plain http") as error:
        _resolve()

    assert _KEY not in str(error.value)


def test_a_host_flag_cannot_point_a_loopback_http_url_at_another_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--host`` keeps the scheme, so a local http URL cannot reach a far host."""
    monkeypatch.setenv("TRACKINIZER_URL", "http://127.0.0.1:8446")
    monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

    with pytest.raises(ClientError, match="plain http"):
        connection.connect(
            argparse.Namespace(profile=None, host="server.example", port=None),
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
