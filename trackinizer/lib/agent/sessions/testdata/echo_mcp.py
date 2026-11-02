"""A one-tool stdio MCP server, so a capture can record an MCP tool call.

``capture.py --cli claude-kinds`` points claude at it with ``--mcp-config``;
claude names its one tool ``mcp__probe__echo``.
"""

from __future__ import annotations

from mcp.server import MCPServer


def echo(text: str) -> str:
    """Return ``text`` unchanged.

    Args:
      text: What to echo.

    Returns:
      text: The same text.

    """
    return text


def build() -> MCPServer:
    """Return the server, with ``echo`` as its only tool."""
    server = MCPServer("probe")
    server.tool()(echo)
    return server


if __name__ == "__main__":
    build().run()
