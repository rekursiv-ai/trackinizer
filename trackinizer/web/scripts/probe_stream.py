#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Stream the live-stream probe through one hop and print its timeline.

`GET /api/web/subscribe/probe` sends frames on the schedule these flags set,
each stamped with the server's seconds since the request. The timeline shows
when the headers arrived, each chunk's arrival beside the time the server sent
its frames (the difference is how long the path held them), and whether the
server ended the stream or something cut it first. It answers, without a
redeploy, whether a hop holds headers until the first body byte, when it cuts an
idle stream, and whether a keep-alive or `Cache-Control: no-transform` changes
that.

The server is the active trax profile's, with its token. --via reaches the same
server at another address, such as a hop behind the edge through an ssh tunnel,
and sends the profile's host as Host, so the token still goes to that server
alone. --url names another server (the local preview) and sends no token.

Examples:
  ./probe_stream.py --first-after 10 --for 30     # headers at 0 s or at 10 s?
  ./probe_stream.py --for 200                     # when is an idle stream cut?
  ./probe_stream.py --every 25 --for 300          # does a 25 s keep-alive hold it?
  ./probe_stream.py --accept-encoding identity --first-after 10 --for 30
  ssh -N -L 18446:127.0.0.1:8446 -L 18443:127.0.0.1:8443 server-host &
  ./probe_stream.py --via http://127.0.0.1:18446 --first-after 10 --for 30
  ./probe_stream.py --via http://127.0.0.1:18443 --first-after 10 --for 30

'''
# fmt: on

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.parse import urlsplit

import argparse
import json
import time

import httpx2

from trackinizer.lib.custom_json import convert, parse
from trackinizer.trax.profile import Profile, load_profile


if TYPE_CHECKING:
    from collections.abc import Mapping


PROBE_PATH: Final = "/api/web/subscribe/probe"


def main() -> int:
    """Probe one hop and print the timeline.

    Returns:
      exit_code: 0; a refused connection raises instead.

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    flags = cast(_Flags, parser.parse_args())
    base_url, headers = client_settings(
        via=flags.via,
        url=flags.url,
        accept_encoding=flags.accept_encoding,
        profile=load_profile(),
    )
    params = {
        "first_after_sec": str(flags.first_after),
        "every_sec": str(flags.every),
        "for_sec": str(flags.for_sec),
        "forbid_transform": str(flags.forbid_transform).lower(),
    }
    print(
        f"GET {base_url}{PROBE_PATH} {params} Accept-Encoding: {flags.accept_encoding}",
    )
    with httpx2.Client(base_url=base_url, headers=headers) as http:
        # Past the server's own end, so only a cut ends the read early.
        events = probe(http, params=params, read_timeout_sec=flags.for_sec + 30)
    for event in events:
        print(f"{event.at_sec:9.3f}s  {event.what}")
    return 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """One moment in the probe's timeline, in seconds since the request."""

    at_sec: float
    what: str


def client_settings(
    *,
    via: str | None,
    url: str | None,
    accept_encoding: str,
    profile: Profile,
) -> tuple[str, dict[str, str]]:
    """Return the base URL and headers for the server the flags name.

    Args:
      via: Another address for the profile's server, or None.
      url: Another server, which gets no token, or None.
      accept_encoding: The ``Accept-Encoding`` to send.
      profile: The active trax profile.

    Returns:
      base_url: Where to connect.
      headers: The request headers; the token only for the profile's server.

    """
    headers = {"Accept-Encoding": accept_encoding}
    if url:
        return url.rstrip("/"), headers
    if profile.api_key:
        headers["Authorization"] = f"Bearer {profile.api_key}"
    if via:
        headers["Host"] = urlsplit(profile.url).netloc
        return via.rstrip("/"), headers
    return profile.url.rstrip("/"), headers


def probe(
    http: httpx2.Client,
    *,
    params: Mapping[str, str],
    read_timeout_sec: float,
) -> list[Event]:
    """Stream the probe and record when each part arrived.

    Args:
      http: A client on the server, with its headers.
      params: The probe's query: its schedule and ``forbid_transform``.
      read_timeout_sec: Longest wait for the next chunk.

    Returns:
      events: The headers, each chunk, and the end or the cut, in order.

    """
    start = time.perf_counter()
    events: list[Event] = []
    try:
        with http.stream(
            "GET",
            PROBE_PATH,
            params=params,
            timeout=httpx2.Timeout(30.0, read=read_timeout_sec),
        ) as response:
            events.append(
                Event(at_sec=time.perf_counter() - start, what=_headers(response)),
            )
            encoding = response.headers.get("content-encoding", "")
            pending = b""
            for chunk in response.iter_raw():
                at_sec = time.perf_counter() - start
                if response.is_error:
                    what = f"chunk {len(chunk)} B: {chunk[:120]!r}"
                elif encoding:
                    what = f"chunk {len(chunk)} B: {encoding}, frames not read"
                else:
                    *frames, pending = (pending + chunk).split(b"\n\n")
                    what = f"chunk {len(chunk)} B: {_frames(frames, at_sec=at_sec)}"
                events.append(Event(at_sec=at_sec, what=what))
    except httpx2.HTTPError as err:
        what = f"cut: {type(err).__name__}: {err}"
    else:
        what = "end: the server closed the stream"
    events.append(Event(at_sec=time.perf_counter() - start, what=what))
    return events


class _Flags(Protocol):
    """Parsed command-line flags."""

    via: str | None
    url: str | None
    first_after: float
    every: float
    for_sec: float
    forbid_transform: bool
    accept_encoding: str


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags on ``parser``."""
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--via",
        help="Another address for the profile's server (a hop), with its token.",
    )
    where.add_argument(
        "--url",
        help="Another server, such as the local preview; no token.",
    )
    parser.add_argument(
        "--first-after",
        type=float,
        default=0.0,
        help="Seconds before the first frame; at or past --for, none.",
    )
    parser.add_argument(
        "--every",
        type=float,
        default=0.0,
        help="Seconds between frames after the first; 0 sends one.",
    )
    parser.add_argument(
        "--for",
        dest="for_sec",
        type=float,
        default=60.0,
        help="Seconds until the server ends the stream (at most 600).",
    )
    parser.add_argument(
        "--forbid-transform",
        action="store_true",
        help="Ask the server to send Cache-Control: no-transform.",
    )
    parser.add_argument(
        "--accept-encoding",
        default="gzip, deflate, br, zstd",
        help="The Accept-Encoding to send; a browser's by default, or identity.",
    )


def _headers(response: httpx2.Response) -> str:
    """Name the status and the headers that show which hops touched the response."""
    names = (
        "content-type",
        "content-encoding",
        "cache-control",
        "via",
        "server",
        "cf-ray",
    )
    shown = " ".join(f"{name}={response.headers.get(name, '-')}" for name in names)
    return f"headers {response.status_code} {shown}"


def _frames(frames: list[bytes], *, at_sec: float) -> str:
    """Each whole probe frame's send time and how long the path held it."""
    parts: list[str] = []
    for frame in frames:
        try:
            data = parse(frame.removeprefix(b"data: "), dict[str, object])
        except (json.JSONDecodeError, TypeError):
            parts.append(f"not a probe frame: {frame[:40]!r}")
            continue
        sent_sec = convert(data["t"], float)
        parts.append(
            f"seq {convert(data['seq'], int)} sent at +{sent_sec:.3f}s, "
            f"held {at_sec - sent_sec:.3f}s",
        )
    return "; ".join(parts) or "frames: none complete"


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
