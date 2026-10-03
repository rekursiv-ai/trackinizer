"""Tests for ``probe_stream.py``: its timeline, and where the token goes."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import argparse

import httpx2
import pytest

from trackinizer.trax.profile import Profile
from trackinizer.web.scripts.probe_stream import (
    PROBE_PATH,
    _add_arguments,
    _Flags,
    client_settings,
    probe,
)


if TYPE_CHECKING:
    from collections.abc import Iterator


_PROFILE = Profile(url="https://tracker.example", api_key="trax_secret")


def _client(
    status: int,
    chunks: Iterator[bytes],
    headers: dict[str, str] | None = None,
) -> httpx2.Client:
    return httpx2.Client(
        base_url="https://tracker.example",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(status, headers=headers, content=chunks),
        ),
    )


def _frames(*pairs: tuple[int, float]) -> bytes:
    return b"".join(
        f'data: {{"seq": {seq}, "t": {t}}}\n\n'.encode() for seq, t in pairs
    )


def _cut_after(first: bytes) -> Iterator[bytes]:
    yield first
    raise httpx2.RemoteProtocolError("peer closed connection")


def test_timeline_reads_headers_frames_and_a_clean_end() -> None:
    # A frame split across chunks is still read once, whole.
    body = _frames((0, 0.001), (1, 0.002))
    events = probe(
        _client(200, iter([body[:10], body[10:]]), {"via": "1.1 Caddy"}),
        params={},
        read_timeout_sec=5,
    )
    lines = [event.what for event in events]
    assert lines[0].startswith("headers 200")
    assert "via=1.1 Caddy" in lines[0]
    assert "frames: none complete" in lines[1]
    assert "seq 0 sent at +0.001s" in lines[2]
    assert "seq 1 sent at +0.002s" in lines[2]
    assert lines[-1] == "end: the server closed the stream"
    assert [event.at_sec for event in events] == sorted(e.at_sec for e in events)


def test_a_cut_stream_says_how_it_ended() -> None:
    events = probe(
        _client(200, _cut_after(_frames((0, 0.001)))),
        params={},
        read_timeout_sec=5,
    )
    assert events[-1].what == "cut: RemoteProtocolError: peer closed connection"


def test_an_encoded_body_is_sized_not_parsed() -> None:
    events = probe(
        _client(200, iter([b"\x28\xb5\x2f\xfd"]), {"content-encoding": "zstd"}),
        params={},
        read_timeout_sec=5,
    )
    assert events[1].what == "chunk 4 B: zstd, frames not read"


def test_a_proxy_error_page_shows_its_status() -> None:
    events = probe(
        _client(524, iter([b"<!DOCTYPE html>"])),
        params={},
        read_timeout_sec=5,
    )
    assert events[0].what.startswith("headers 524")
    assert events[1].what == "chunk 15 B: b'<!DOCTYPE html>'"


@pytest.mark.parametrize(
    ("argv", "base_url", "headers"),
    [
        (
            [],
            "https://tracker.example",
            {"Authorization": "Bearer trax_secret"},
        ),
        (
            ["--via", "http://127.0.0.1:18443"],
            "http://127.0.0.1:18443",
            {"Authorization": "Bearer trax_secret", "Host": "tracker.example"},
        ),
        (["--url", "http://127.0.0.1:8765"], "http://127.0.0.1:8765", {}),
    ],
)
def test_the_token_goes_only_to_the_profiles_server(
    argv: list[str],
    base_url: str,
    headers: dict[str, str],
) -> None:
    # --via reaches the same server another way (a hop behind the edge), so
    # the token and the server's Host go with it; --url is another server.
    parser = argparse.ArgumentParser()
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args(argv))
    got_url, got_headers = client_settings(
        via=flags.via,
        url=flags.url,
        accept_encoding="identity",
        profile=_PROFILE,
    )
    assert got_url == base_url
    assert got_headers == {"Accept-Encoding": "identity", **headers}


def test_the_schedule_flags_become_the_probe_query() -> None:
    parser = argparse.ArgumentParser()
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args(["--first-after", "10", "--for", "30"]))
    assert (flags.first_after, flags.every, flags.for_sec) == (10.0, 0.0, 30.0)
    assert PROBE_PATH == "/api/web/subscribe/probe"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
