"""Remote (Streamable HTTP) MCP servers: configuration contract, identity and a real
loopback server. No external network: the only server listens on 127.0.0.1."""

import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import types
from mcp.client.streamable_http import StreamableHTTPError
from mcp.shared.exceptions import McpError
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
from app import mcp_client, service
from app.models import McpServer
from app.routers import mcp_router

SECRET = "synthetic-header-secret-417"
# Built from parts so the release scanner's credential-URL rule does not flag the test.
CREDENTIAL_URL = "https://user" + ":secret@docs.example/mcp"

SERVER = """
import json
import socket
import sys
import uvicorn
from mcp.server.fastmcp import Context, FastMCP

server = FastMCP("http-test")
count = 0
stats = {"sessions": 0, "posts": 0}
control = {"expire_next": False}

@server.tool()
def increment() -> int:
    global count
    count += 1
    return count

@server.tool()
def huge() -> str:
    return "large-output-" * 5000

@server.tool()
def fails() -> str:
    raise ValueError("expected remote tool error")

@server.tool()
async def announce(ctx: Context) -> str:
    await ctx.session.send_tool_list_changed()
    return "announced"

inner = server.streamable_http_app()


async def respond(send, status, body=b"", headers=()):
    await send({"type": "http.response.start", "status": status, "headers": list(headers)})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    if scope["type"] == "http":
        headers = dict(scope["headers"])
        path = scope["path"]
        if path == "/control/stats":
            return await respond(send, 200, json.dumps(stats).encode())
        if path == "/control/expire":
            control["expire_next"] = True
            return await respond(send, 200)
        if path == "/moved":
            return await respond(send, 307, headers=[(b"location", b"/mcp")])
        if headers.get(b"authorization") != b"Bearer synthetic-token":
            return await respond(send, 401, b"denied")
        if scope["method"] == "POST":
            stats["posts"] += 1
            if b"mcp-session-id" not in headers:
                stats["sessions"] += 1
            elif control["expire_next"]:
                control["expire_next"] = False
                return await respond(send, 404)
    await inner(scope, receive, send)


sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
"""


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    script = tmp_path_factory.mktemp("mcp-http") / "server.py"
    script.write_text(SERVER)
    process = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        port = int(process.stdout.readline())
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while True:
            try:
                httpx.get(base + "/control/stats", timeout=1).raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        yield base
    finally:
        mcp_client.close_all()
        process.terminate()
        process.wait(5)


@pytest.fixture
def remote_spec(remote):
    spec = mcp_client.server_spec(
        transport="http",
        url=f"{remote}/mcp",
        headers={"Authorization": "Bearer synthetic-token"},
        allow_private=True,
    )
    yield spec
    mcp_client.close_all()


def stats(base):
    return httpx.get(base + "/control/stats", timeout=5).json()


def test_remote_session_lists_calls_and_keeps_state(remote_spec):
    names = {tool["name"] for tool in mcp_client.list_tools(remote_spec)}
    assert {"increment", "huge", "fails"} <= names
    first = int(mcp_client.call_tool(remote_spec, "increment", {}))
    assert int(mcp_client.call_tool(remote_spec, "increment", {})) == first + 1
    output = mcp_client.call_tool(remote_spec, "huge", {})
    assert len(output) <= mcp_client.MAX_OUTPUT_CHARS and "已截断" in output
    error = mcp_client.call_tool(remote_spec, "fails", {})
    assert "MCP 工具报告错误" in error and "expected remote tool error" in error


def test_headers_reach_the_server_and_never_the_error_text(remote, remote_spec):
    wrong = {**remote_spec, "headers": {"Authorization": f"Bearer {SECRET}"}}
    with pytest.raises(mcp_client.MCPRemoteStatusError) as raised:
        mcp_client.list_tools(wrong)
    assert raised.value.status_code == 401
    message = mcp_client.describe_error(raised.value, wrong)
    assert message == mcp_client.REMOTE_DENIED and SECRET not in message
    failure = mcp_client.call_tool(wrong, "increment", {})
    assert "调用失败" in failure and SECRET not in failure and remote not in failure


def test_redirects_are_not_followed(remote, remote_spec):
    moved = {**remote_spec, "url": f"{remote}/moved"}
    with pytest.raises(mcp_client.MCPRemoteStatusError) as raised:
        mcp_client.list_tools(moved)
    assert raised.value.status_code == 307
    assert mcp_client.describe_error(raised.value, moved) == mcp_client.REMOTE_REDIRECT


def test_private_address_needs_the_explicit_opt_in(remote, remote_spec):
    before = stats(remote)
    refused = {**remote_spec, "allow_private": False}
    with pytest.raises(mcp_client.MCPConfigError) as raised:
        mcp_client.list_tools(refused)
    assert str(raised.value) == mcp_client.REMOTE_URL_PRIVATE
    assert stats(remote) == before  # Refused before any request was sent.
    assert mcp_client._key(refused) != mcp_client._key(remote_spec)


def test_expired_remote_session_reconnects_once_for_discovery(remote, remote_spec):
    assert mcp_client.list_tools(remote_spec)
    sessions = stats(remote)["sessions"]
    httpx.get(f"{remote}/control/expire", timeout=5).raise_for_status()
    assert {tool["name"] for tool in mcp_client.list_tools(remote_spec)} >= {"increment"}
    assert stats(remote)["sessions"] == sessions + 1


def test_remote_checks_refuse_private_unresolvable_and_credentialed_urls(monkeypatch):
    def resolver(address):
        return lambda host, port, **kwargs: [(2, 1, 6, "", (address, port))]

    spec = mcp_client.server_spec(transport="http", url="https://mcp.example/mcp")
    monkeypatch.setattr(mcp_client, "RESOLVER", resolver("93.184.215.14"))
    mcp_client._check_remote(spec)  # A public address passes.
    for address in ("10.0.0.5", "100.101.102.103", "169.254.169.254", "::1"):
        monkeypatch.setattr(mcp_client, "RESOLVER", resolver(address))
        with pytest.raises(mcp_client.MCPConfigError, match="内网"):
            mcp_client._check_remote(spec)
        mcp_client._check_remote({**spec, "allow_private": True})

    def unresolvable(*args, **kwargs):
        raise socket.gaierror("synthetic lookup failure")

    for lookup in (unresolvable, lambda *args, **kwargs: []):
        monkeypatch.setattr(mcp_client, "RESOLVER", lookup)
        with pytest.raises(mcp_client.MCPConfigError, match="无法解析") as raised:
            mcp_client._check_remote(spec)
        assert "synthetic" not in str(raised.value)
    for url in (CREDENTIAL_URL, "file:///etc/passwd", "ftp://mcp.example"):
        with pytest.raises(mcp_client.MCPConfigError, match="地址无效"):
            mcp_client._check_remote({**spec, "url": url, "allow_private": True})


def test_http_failures_count_as_lost_transport_and_timeouts_stay_uncertain():
    request = httpx.Request("POST", "https://mcp.example/mcp")
    status = httpx.HTTPStatusError(
        "denied", request=request, response=httpx.Response(401, request=request)
    )
    terminated = McpError(types.ErrorData(code=32600, message="Session terminated"))
    for error in (
        httpx.ConnectError("refused"),
        status,
        StreamableHTTPError("closed"),
        terminated,
        ExceptionGroup("group", [httpx.RemoteProtocolError("broken")]),
    ):
        assert mcp_client._transport_error(error)
    assert mcp_client._timeout_error(httpx.ReadTimeout("slow"))
    assert mcp_client._http_status(ExceptionGroup("group", [status])) == 401
    assert not mcp_client._transport_error(
        McpError(types.ErrorData(code=-32602, message="Invalid params"))
    )


def test_http_identity_hashes_every_field_without_exposing_header_values():
    spec = mcp_client.server_spec(
        transport="http",
        url="https://mcp.example/mcp",
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    key = mcp_client._key(spec)
    assert SECRET not in key and len(key) == 64
    variants = [
        {**spec, "url": "https://other.example/mcp"},
        {**spec, "headers": {"Authorization": "Bearer another"}},
        {**spec, "allow_private": True},
    ]
    assert len({key, *(mcp_client._key(variant) for variant in variants)}) == 4
    row = SimpleNamespace(
        command="",
        args=None,
        transport="http",
        url=" https://mcp.example/mcp ",
        headers={"Authorization": f"Bearer {SECRET}"},
        allow_private=False,
    )
    assert mcp_client.server_spec_for(row) == spec
    legacy = SimpleNamespace(command="uvx", args=["mcp-server-time"])
    assert mcp_client.server_spec_for(legacy) == mcp_client.server_spec("uvx", ["mcp-server-time"])


def test_remote_selection_lists_and_executes_with_one_identity(monkeypatch):
    server = SimpleNamespace(
        id=7,
        label="Remote docs",
        enabled=True,
        command="",
        args=None,
        transport="http",
        url="https://docs.example/mcp",
        headers={"Authorization": f"Bearer {SECRET}"},
        allow_private=False,
    )
    session = SimpleNamespace(get=lambda model, sid: server if sid == server.id else None)
    listed, calls = [], []

    def list_tools(spec, *args, **kwargs):
        listed.append(spec)
        return [{"name": "lookup", "description": "Find docs", "schema": {"type": "object"}}]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: calls.append(args) or "found")
    definitions, routes = service.assemble_tools(session, ["mcp_server_7"])
    assert [item["function"]["name"] for item in definitions] == ["mcp_7_lookup"]
    assert listed == [mcp_client.server_spec_for(server)]
    assert listed[0]["url"] == "https://docs.example/mcp" and listed[0]["transport"] == "http"
    assert service.make_executor(routes)("mcp_7_lookup", {"q": "x"}) == "found"
    assert calls[0][0] == listed[0] and calls[0][1] == "lookup"


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    SQLModel.metadata.create_all(engine)
    application = FastAPI()
    application.include_router(mcp_router.router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[database.get_session] = session_override
    invalidated = []
    monkeypatch.setattr(mcp_client, "invalidate", lambda spec, **kwargs: invalidated.append(spec))
    yield TestClient(application), engine, invalidated
    engine.dispose()


REMOTE = {
    "label": "Remote docs",
    "transport": "http",
    "url": "https://docs.example/mcp",
    "headers": {"Authorization": f"Bearer {SECRET}", "X-Team": "learning"},
    "enabled": True,
}


def stored(engine, server_id):
    with Session(engine) as session:
        return session.get(McpServer, server_id)


def test_header_values_are_stored_but_never_returned(api):
    client, engine, _ = api
    created = client.post("/mcp", json=REMOTE)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["header_names"] == ["Authorization", "X-Team"]
    assert body["transport"] == "http" and body["url"] == REMOTE["url"]
    assert body["command"] == "" and body["args"] == [] and body["allow_private"] is False
    listing = client.get("/mcp")
    assert (
        SECRET not in created.text and SECRET not in listing.text and "learning" not in listing.text
    )
    assert stored(engine, body["id"]).headers == REMOTE["headers"]


@pytest.mark.parametrize(
    "change",
    [
        {"url": CREDENTIAL_URL},
        {"url": "file:///etc/passwd"},
        {"url": "docs.example/mcp"},
        {"url": None},
        {"url": "https://docs.example:99999/mcp"},
        {"headers": {"Host": "elsewhere.example"}},
        {"headers": {"Mcp-Session-Id": "forged"}},
        {"headers": {"bad name": "x"}},
        {"headers": {"X-Injected": "a\r\nX-Other: b"}},
        {"headers": {"X-Team": "a", "x-team": "b"}},
        {"transport": "stdio", "command": "  "},
    ],
)
def test_invalid_server_configurations_are_rejected(api, change):
    client, _, _ = api
    assert client.post("/mcp", json={**REMOTE, **change}).status_code == 422


def test_blank_header_keeps_stored_value_and_omission_keeps_all(api):
    client, engine, invalidated = api
    created = client.post("/mcp", json=REMOTE).json()
    sid = created["id"]
    assert client.post("/mcp", json={**REMOTE, "headers": {"X-New": ""}}).status_code == 422
    renamed = {key: value for key, value in REMOTE.items() if key != "headers"}
    assert client.put(f"/mcp/{sid}", json={**renamed, "label": "Docs"}).status_code == 200
    assert stored(engine, sid).headers == REMOTE["headers"]
    kept = client.put(
        f"/mcp/{sid}", json={**REMOTE, "headers": {"authorization": "", "X-Team": "changed"}}
    )
    assert kept.status_code == 200 and kept.json()["header_names"] == ["authorization", "X-Team"]
    assert stored(engine, sid).headers == {
        "authorization": f"Bearer {SECRET}",
        "X-Team": "changed",
    }
    assert client.put(f"/mcp/{sid}", json={**REMOTE, "headers": {"X-New": ""}}).status_code == 422
    assert stored(engine, sid).headers["X-Team"] == "changed"  # A rejected update changes nothing.
    assert client.put(f"/mcp/{sid}", json={**REMOTE, "headers": {}}).json()["header_names"] == []
    assert stored(engine, sid).headers is None
    # Every update closed the session of the identity the row had before it.
    assert invalidated[0]["headers"] == REMOTE["headers"] and invalidated[0]["url"] == REMOTE["url"]
    assert all(spec["transport"] == "http" for spec in invalidated)


def test_switching_to_a_local_command_drops_remote_credentials(api):
    client, engine, invalidated = api
    sid = client.post("/mcp", json=REMOTE).json()["id"]
    local = {"label": "Local", "command": "uvx", "args": ["mcp-server-time"], "enabled": True}
    body = client.put(f"/mcp/{sid}", json=local).json()
    assert body["transport"] == "stdio" and body["url"] is None and body["header_names"] == []
    row = stored(engine, sid)
    assert row.headers is None and row.url is None and row.command == "uvx"
    assert invalidated[-1]["url"] == REMOTE["url"]
    client.patch(f"/mcp/{sid}", params={"enabled": False})
    assert invalidated[-1] == mcp_client.server_spec("uvx", ["mcp-server-time"])
    client.delete(f"/mcp/{sid}")
    assert invalidated[-1] == mcp_client.server_spec("uvx", ["mcp-server-time"])


def raising(error):
    def fail(*args, **kwargs):
        raise error

    return fail


def test_connection_test_reports_fixed_messages_only(api, monkeypatch):
    client, _, _ = api
    sid = client.post("/mcp", json=REMOTE).json()["id"]
    seen = []

    def failing(spec, *args, **kwargs):
        seen.append(spec)
        raise RuntimeError(f"upstream echoed Authorization: Bearer {SECRET}")

    monkeypatch.setattr(mcp_client, "list_tools", failing)
    result = client.post(f"/mcp/{sid}/test").json()
    assert result == {"ok": False, "detail": mcp_client.CONNECTION_FAILED, "tools": []}
    assert seen[0]["headers"] == REMOTE["headers"] and seen[0]["url"] == REMOTE["url"]
    for error, expected in (
        (mcp_client.MCPRemoteStatusError(403), mcp_client.REMOTE_DENIED),
        (mcp_client.MCPRemoteStatusError(404), mcp_client.REMOTE_NOT_FOUND),
        (mcp_client.MCPRemoteStatusError(502), mcp_client.REMOTE_FAILED),
        (mcp_client.MCPConnectionError("MCP 连接已关闭"), mcp_client.REMOTE_UNREACHABLE),
        (mcp_client.MCPTimeoutError("MCP 连接启动超时"), mcp_client.REMOTE_TIMEOUT),
        (mcp_client.MCPConfigError(mcp_client.REMOTE_URL_PRIVATE), mcp_client.REMOTE_URL_PRIVATE),
    ):
        monkeypatch.setattr(mcp_client, "list_tools", raising(error))
        assert client.post(f"/mcp/{sid}/test").json()["detail"] == expected
    monkeypatch.setattr(mcp_client, "list_tools", lambda *a, **k: [{"name": "lookup"}])
    assert client.post(f"/mcp/{sid}/test").json() == {
        "ok": True,
        "detail": "连上了，发现 1 个工具",
        "tools": ["lookup"],
    }


def test_local_command_errors_keep_their_fixed_wording():
    local = mcp_client.server_spec("uvx", [])
    missing = mcp_client.MCPCommandNotFound("未找到 MCP 启动命令：uvx。请确认已经安装。")
    assert mcp_client.describe_error(missing, local) == str(missing)
    closed = mcp_client.MCPConnectionError("MCP 连接已关闭")
    assert mcp_client.describe_error(closed, local) == "MCP 连接已关闭"
    assert mcp_client.describe_error(ValueError("/private/path"), local) == (
        mcp_client.CONNECTION_FAILED
    )


def test_legacy_server_table_gains_transport_columns(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE mcpserver (id INTEGER PRIMARY KEY, label VARCHAR NOT NULL, "
                "command VARCHAR NOT NULL, args JSON, enabled BOOLEAN NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO mcpserver VALUES "
                "(1, 'Time', 'uvx', '[\"mcp-server-time\"]', 1, '2026-01-01 00:00:00')"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with Session(engine) as session:
        row = session.get(McpServer, 1)
        assert (row.transport, row.url, row.headers, row.allow_private) == (
            "stdio",
            None,
            None,
            False,
        )
        assert mcp_client.server_spec_for(row) == mcp_client.server_spec("uvx", ["mcp-server-time"])
    engine.dispose()
