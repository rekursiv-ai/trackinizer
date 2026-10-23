"""The connection chain: flags, environment and saved profile to one ``Client``."""

from __future__ import annotations

from typing import Final

import argparse

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.trax import connection, profile
from trackinizer.trax.client_cache import Target, close_clients
from trackinizer.trax.profile import Profile


def test_profile_flag_overrides_trackinizer_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile.save_profile("prod", Profile(url="http://prod:9000", author="alice"))
    monkeypatch.setenv("TRACKINIZER_URL", "http://env:8766")
    client = connection.connect(
        argparse.Namespace(profile="prod", host=None, port=None),
    )
    try:
        assert client.base_url == "http://prod:9000"
        assert client.author == "alice"
    finally:
        close_clients()


_KEY: Final = "tok-SENTINEL-93f1c7"
"""A recognisable key, so a leak is found by substring."""


@pytest.fixture(autouse=True)
def no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub an exported ``TRACKINIZER_TOKEN`` so it cannot bleed into these tests."""
    monkeypatch.delenv("TRACKINIZER_TOKEN", raising=False)


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
                {"TRACKINIZER_URL": "http://env:1", "TRACKINIZER_TOKEN": _KEY},
                Target(url="http://prod:9000", author="alice", api_key="profile-key"),
                id="row1-profile-flag-ignores-env-token",
            ),
            pytest.param(
                None,
                {
                    "TRACKINIZER_URL": "http://env:1",
                    "TRACKINIZER_TOKEN": _KEY,
                    "TRACKINIZER_PROFILE": "prod",
                },
                Target(url="http://env:1", author="", api_key=_KEY),
                id="row2-env-url-and-token",
            ),
            pytest.param(
                None,
                {"TRACKINIZER_URL": "http://env:1", "TRACKINIZER_PROFILE": "prod"},
                Target(url="http://env:1", author="", api_key=""),
                id="row3-env-url-alone-has-no-key",
            ),
            pytest.param(
                None,
                {"TRACKINIZER_PROFILE": "prod"},
                Target(url="http://prod:9000", author="alice", api_key="profile-key"),
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
            Profile(url="http://prod:9000", author="alice", api_key="profile-key"),
        )
        for name, value in env_pairs.items():
            monkeypatch.setenv(name, value)

        assert _resolve(profile_name=profile_flag) == expected

    def test_host_flags_rewrite_the_env_url_and_keep_the_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "http://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve(port=9) == Target(url="http://env:9", author="", api_key=_KEY)

    def test_token_without_url_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An ops shell exports only the token; the profile's own key stays in use."""
        profile.save_profile(
            "prod",
            Profile(url="http://prod:9000", author="alice", api_key="profile-key"),
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
            url="http://prod:9000",
            author="alice",
            api_key="profile-key",
        )
        assert sent_key == "profile-key"
        assert capsys.readouterr().err == ""

    def test_token_without_url_falls_through_to_the_profile_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile.save_profile("a", Profile(url="http://a:1", api_key="key-a"))
        profile.save_profile("b", Profile(url="http://b:1", api_key="key-b"))
        profile.switch_profile("a")
        monkeypatch.setenv("TRACKINIZER_PROFILE", "b")
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve() == Target(url="http://b:1", author="", api_key="key-b")

    def test_token_without_url_and_without_profile_sends_no_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve().api_key == ""

    def test_an_empty_token_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACKINIZER_URL", "http://env:1")
        monkeypatch.setenv("TRACKINIZER_TOKEN", "")

        assert _resolve() == Target(url="http://env:1", author="", api_key="")

    def test_profile_flag_needs_no_url_for_an_unused_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile.save_profile("prod", Profile(url="http://prod:9000"))
        monkeypatch.setenv("TRACKINIZER_TOKEN", _KEY)

        assert _resolve(profile_name="prod").url == "http://prod:9000"


class TestClientSharing:
    """A long-lived process must not build a connection pool per invocation.

    The daemon calls ``parse_and_run`` once per request. A client cached
    inside that call would open a fresh ``httpx2`` pool -- and a fresh TCP
    handshake -- every time, discarding the keep-alive the transport exists
    to provide, and would accumulate open sockets for the daemon's whole life.
    """

    def test_reuses_one_client_across_invocations(self) -> None:
        profile.save_profile("prod", Profile(url="http://prod:9000"))
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
        profile.save_profile("a", Profile(url="http://a:9000"))
        profile.save_profile("b", Profile(url="http://b:9000"))
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
        profile.save_profile("prod", Profile(url="http://prod:9000", api_key="old"))
        args = argparse.Namespace(profile="prod", host=None, port=None)
        try:
            before = connection.connect(args)
            profile.save_profile("prod", Profile(url="http://prod:9000", api_key="new"))
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


def test_a_url_that_names_no_host_has_no_directory() -> None:
    with pytest.raises(ClientError, match="names no server host"):
        _ = connection.server_dir("not a url")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
