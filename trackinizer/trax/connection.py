"""Build a ``Client`` from the connection flags, environment, and saved profile.

The connection chain ``trax`` shares with the commands that sit beside it.
It lives here rather than on ``Client`` so ``client.py`` stays pure HTTP transport
with no dependency on the profile store, and rather than in ``cli`` so an endpoint
imports the chain without importing every ``trax`` verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse, urlsplit

import re

from trackinizer.client.client import Client, server_url
from trackinizer.client.errors import ClientError
from trackinizer.trax.client_cache import Target, shared_client
from trackinizer.trax.context import env
from trackinizer.trax.profile import Profile, load_profile, read_profile


if TYPE_CHECKING:
    import argparse


__all__ = ["connect", "connect_flags", "resolve_target", "server_dir"]


def connect_flags(parser: argparse.ArgumentParser) -> None:
    """Register ``--profile``, ``--host``, and ``--port``.

    Args:
      parser: Parser.

    """
    parser.add_argument("--profile", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)


def connect(args: argparse.Namespace) -> Client:
    """Return a Client for the flags, environment, and saved profile.

    Resolution order, highest precedence first:
      1. ``--host`` / ``--port`` flags rewrite the host and port of the URL
         chosen below.
      2. ``--profile <name>`` (a saved profile, key included; missing on disk
         is an error). ``$TRACKINIZER_TOKEN`` is ignored.
      3. ``$TRACKINIZER_URL`` with ``$TRACKINIZER_TOKEN`` (a raw URL and its
         key). A token without a URL is ignored, never sent to a profile's
         server.
      4. ``$TRACKINIZER_URL`` alone (a raw URL, no key).
      5. ``$TRACKINIZER_PROFILE`` (a profile name; missing is an error).
      6. The ``current`` file written by ``trax profile current NAME``.
      7. The ``default`` profile.
      8. ``http://127.0.0.1:8765``, when nothing above is set.

    Clients are shared per resolved identity. A one-shot CLI run builds
    exactly one either way, but the daemon serves thousands of invocations
    from one process: a fresh ``Client`` per request would open a new
    connection pool each time -- discarding the keep-alive the transport
    exists to provide -- and accumulate sockets for the daemon's whole life.

    Args:
      args: Parsed top-level flags (--profile, --host, --port).

    Returns:
      client: Shared cached Client for the resolved target identity.

    """
    return shared_client(resolve_target(args))


def resolve_target(args: argparse.Namespace) -> Target:
    """Resolve flags, environment, and profile into one connection identity.

    Args:
      args: Parsed connection flags; a bare ``Namespace()`` reads as no flags.

    Returns:
      target: The server URL, audit actor and key the flags and environment name.

    Raises:
      ClientError: ``$TRACKINIZER_TOKEN`` is malformed, or a named profile or
        ``$TRACKINIZER_URL`` is unusable.

    """
    host = cast(str | None, getattr(args, "host", None))  # -- argparse namespace field.
    port = cast(int | None, getattr(args, "port", None))  # -- argparse namespace field.
    if (
        name := cast(str | None, getattr(args, "profile", None)) or ""
    ):  # -- argparse field.
        profile = read_profile(name)
    elif env_url := env("TRACKINIZER_URL") or "":
        profile = Profile(
            url=server_url(env_url, "TRACKINIZER_URL"),
            api_key=_env_key(),
        )
    else:
        # ``TRACKINIZER_TOKEN`` alone is not read: ops shells export it without a URL,
        # and sending it to a profile's server would hand that server another key.
        profile = load_profile()
    url = profile.url
    if host is not None or port is not None:
        parsed = urlparse(profile.url)
        scheme = parsed.scheme or "http"
        host = host or parsed.hostname or "127.0.0.1"
        port = port or parsed.port
        url = f"{scheme}://{host if port is None else f'{host}:{port}'}"
    return Target(url=url, author=profile.author, api_key=profile.api_key)


def server_dir(url: str) -> str:
    """Return the directory name of the server at ``url``: its host, and port if set.

    A credential belongs to the server that issued it: the same name on another
    server is another machine, which would refuse it. Results of a GPU job are kept
    the same way.

    Args:
      url: The server's URL.

    Returns:
      name: The lowercase host, then ``_`` and the port when the URL has one.

    Raises:
      ClientError: The URL names no host.

    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise ClientError(f"{url!r} names no server host")
    return host if parts.port is None else f"{host}_{parts.port}"


# The value must be refused here, with no text of it in the message: httpx2 rejects a
# header value holding a newline or non-ASCII character with an error that quotes it,
# and an uncaught traceback would print the key into the caller's log.
def _env_key() -> str:
    """Return ``$TRACKINIZER_TOKEN``, empty when unset, refusing a malformed one."""
    key = env("TRACKINIZER_TOKEN") or ""
    if key and re.fullmatch(r"[\x21-\x7e]+", key) is None:
        raise ClientError(
            "TRACKINIZER_TOKEN must be printable ASCII with no whitespace",
        )
    return key
