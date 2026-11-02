"""Tests for the capture's one-tool MCP server."""

from __future__ import annotations

import asyncio

from trackinizer.lib.agent.sessions.testdata.echo_mcp import build, echo


def test_echo_returns_its_text() -> None:
    assert echo("hi") == "hi"


def test_the_server_is_probe_and_serves_only_echo() -> None:
    server = build()

    assert server.name == "probe"
    assert [tool.name for tool in asyncio.run(server.list_tools())] == ["echo"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
