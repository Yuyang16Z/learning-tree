"""Exercise a real local stdio transport without user tools, keys or network."""

import asyncio
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ImageContent, Tool

from app import mcp_client

SERVER = """
import asyncio
import os
from pathlib import Path
import sys
from mcp.server.fastmcp import FastMCP

server = FastMCP("runtime-test")
count = 0
ledger = Path(sys.argv[1])

@server.tool()
def increment() -> int:
    global count
    count += 1
    return count

@server.tool()
def process_id() -> int:
    return os.getpid()

@server.tool()
async def slow_mutation() -> str:
    with ledger.open("a") as handle:
        handle.write("mutation\\n")
    await asyncio.sleep(10)
    return "finished"

@server.tool()
def crash_mutation() -> str:
    with ledger.open("a") as handle:
        handle.write("crash\\n")
    os._exit(0)

@server.tool()
def huge() -> str:
    return "large-output-" * 5000

@server.tool()
def fails() -> str:
    raise ValueError("expected tool error")

server.run(transport="stdio")
"""


@pytest.fixture
def server_spec(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    ledger = tmp_path / "actions.txt"
    spec = {"command": sys.executable, "args": [str(script), str(ledger)]}
    yield spec, ledger
    mcp_client.close_all()


def _assert_exited(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.03)
    pytest.fail(f"MCP child {pid} survived shutdown")


def test_one_session_preserves_state_across_discovery_and_threads(server_spec):
    spec, _ = server_spec
    names = {tool["name"] for tool in mcp_client.list_tools(spec)}
    assert {"increment", "process_id", "slow_mutation"} <= names
    pid = int(mcp_client.call_tool(spec, "process_id", {}))
    assert mcp_client.call_tool(spec, "increment", {}) == "1"
    assert mcp_client.list_tools(spec)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(
            pool.map(lambda _: int(mcp_client.call_tool(spec, "increment", {})), range(4))
        )
    assert sorted(values) == [2, 3, 4, 5]
    assert int(mcp_client.call_tool(spec, "process_id", {})) == pid
    mcp_client.close_all()
    _assert_exited(pid)


def test_invalidation_releases_old_process_and_resets_session(server_spec):
    spec, _ = server_spec
    pid = int(mcp_client.call_tool(spec, "process_id", {}))
    assert mcp_client.call_tool(spec, "increment", {}) == "1"
    mcp_client.invalidate(spec)
    _assert_exited(pid)
    assert mcp_client.call_tool(spec, "increment", {}) == "1"
    assert int(mcp_client.call_tool(spec, "process_id", {})) != pid


def test_timed_out_tool_is_not_replayed(server_spec, monkeypatch):
    spec, ledger = server_spec
    mcp_client.list_tools(spec)
    pid = int(mcp_client.call_tool(spec, "process_id", {}))
    monkeypatch.setattr(mcp_client, "CALL_TIMEOUT", 0.15)
    start = time.monotonic()
    result = mcp_client.call_tool(spec, "slow_mutation", {})
    assert "超时" in result and "未确认" in result
    assert time.monotonic() - start < 3
    assert ledger.read_text().splitlines() == ["mutation"]
    mcp_client.close_all()
    _assert_exited(pid)


def test_crashed_tool_is_not_replayed_and_next_discovery_reconnects(server_spec):
    spec, ledger = server_spec
    mcp_client.list_tools(spec)
    result = mcp_client.call_tool(spec, "crash_mutation", {})
    assert "调用失败" in result
    assert ledger.read_text().splitlines() == ["crash"]
    assert mcp_client.list_tools(spec)
    assert mcp_client.call_tool(spec, "increment", {}) == "1"


def test_tool_errors_and_large_output_are_bounded(server_spec):
    spec, _ = server_spec
    error = mcp_client.call_tool(spec, "fails", {})
    assert "MCP 工具报告错误" in error and "expected tool error" in error
    assert mcp_client.call_tool(spec, "increment", {}) == "1"
    output = mcp_client.call_tool(spec, "huge", {})
    assert len(output) <= mcp_client.MAX_OUTPUT_CHARS
    assert "已截断" in output


def test_binary_result_is_not_dumped_into_model_context():
    result = CallToolResult(
        content=[ImageContent(type="image", mimeType="image/png", data="A" * 500_000)]
    )
    output = mcp_client._result_text(result)
    assert len(output) < 200
    assert "image/png" in output and "未发送" in output
    assert "A" * 100 not in output
    result = CallToolResult(
        content=[], structuredContent={"data": "B" * 500_000, "answer": "useful"}
    )
    output = mcp_client._result_text(result)
    assert "useful" in output and "B" * 100 not in output


def test_discovery_reads_all_pages_and_rejects_cycles():
    class PaginatedSession:
        async def list_tools(self, cursor=None):
            name = "first" if cursor is None else "second"
            return SimpleNamespace(
                tools=[Tool(name=name, inputSchema={"type": "object"})],
                nextCursor="next" if cursor is None else None,
            )

    assert [
        tool["name"] for tool in asyncio.run(mcp_client._SessionWorker._list(PaginatedSession()))
    ] == ["first", "second"]

    class CyclicSession:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[], nextCursor="same")

    with pytest.raises(RuntimeError, match="重复"):
        asyncio.run(mcp_client._SessionWorker._list(CyclicSession()))


def test_parameters_expand_home_preserve_path_and_do_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("LEARNING_TREE_PRIVATE_TEST_SECRET", "do-not-forward")
    params = mcp_client._params(
        {"command": sys.executable, "args": ["~/notes"], "env": {"MCP_TEST": "1"}}
    )
    assert params.command == sys.executable
    assert params.args == [str(Path.home() / "notes")]
    assert params.env["MCP_TEST"] == "1" and params.env["PATH"]
    assert "LEARNING_TREE_PRIVATE_TEST_SECRET" not in params.env


def test_initialize_deadline_closes_unresponsive_child(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "unresponsive.py"
    script.write_text(
        "import os, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\nfor line in sys.stdin: pass\n"
    )
    spec = {"command": sys.executable, "args": [str(script), str(pid_file)]}
    monkeypatch.setattr(mcp_client, "CONNECT_TIMEOUT", 0.15)
    start = time.monotonic()
    try:
        with pytest.raises(mcp_client.MCPTimeoutError, match="连接超时"):
            mcp_client.list_tools(spec)
        assert time.monotonic() - start < 3
        _assert_exited(int(pid_file.read_text()))
    finally:
        mcp_client.close_all()


def test_shutdown_finishes_active_waiter_and_closes_its_child(server_spec):
    spec, ledger = server_spec
    pid = int(mcp_client.call_tool(spec, "process_id", {}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(mcp_client.call_tool, spec, "slow_mutation", {})
        deadline = time.monotonic() + 3
        while not ledger.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ledger.exists()
        mcp_client.close_all()
        assert "未确认" in waiting.result(timeout=3)
    _assert_exited(pid)
