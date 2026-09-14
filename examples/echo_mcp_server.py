"""A minimal stdio MCP server for offline tool-integration checks.

Configure it in Settings → MCP tools:
  command = <absolute path to the project .venv Python executable>
  args    = ["<absolute path to this file>"]
It exposes two tools: echo and add. Their localized descriptions are model-facing.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-demo")


@mcp.tool()
def echo(text: str) -> str:
    """原样回显传入的文本。"""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """返回两个整数之和。"""
    return a + b


if __name__ == "__main__":
    mcp.run()  # Use the default stdio transport.
