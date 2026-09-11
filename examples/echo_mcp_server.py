"""一个最小 MCP server（stdio），用来离线验证 MCP 工具链路。

在「设置 → MCP 工具」里这样配：
  command = <项目 .venv 的 python 绝对路径>
  args    = ["<本文件绝对路径>"]
它暴露两个工具：echo、add。
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
    mcp.run()  # 默认 stdio 传输
