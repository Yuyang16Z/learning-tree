"""MCP server 配置：增 / 列 / 删 / 启停 / 测连接（列出它暴露的工具）。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import mcp_client
from ..db import get_session
from ..models import McpServer
from ..schemas import McpServerIn, McpServerOut, McpTestOut

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _out(s: McpServer) -> McpServerOut:
    return McpServerOut(
        id=s.id, label=s.label, command=s.command, args=s.args or [], enabled=s.enabled
    )


@router.post("", response_model=McpServerOut)
def add_server(body: McpServerIn, session: Session = Depends(get_session)) -> McpServerOut:
    srv = McpServer(label=body.label, command=body.command, args=body.args, enabled=body.enabled)
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return _out(srv)


@router.get("", response_model=list[McpServerOut])
def list_servers(session: Session = Depends(get_session)) -> list[McpServerOut]:
    return [_out(s) for s in session.exec(select(McpServer).order_by(McpServer.id))]


@router.put("/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: int, body: McpServerIn, session: Session = Depends(get_session)
) -> McpServerOut:
    srv = session.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server 不存在")
    mcp_client.invalidate({"command": srv.command, "args": srv.args})
    srv.label = body.label
    srv.command = body.command
    srv.args = body.args
    srv.enabled = body.enabled
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return _out(srv)


@router.delete("/{server_id}")
def delete_server(server_id: int, session: Session = Depends(get_session)) -> dict:
    srv = session.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server 不存在")
    mcp_client.invalidate({"command": srv.command, "args": srv.args})
    session.delete(srv)
    session.commit()
    return {"deleted": server_id}


@router.patch("/{server_id}", response_model=McpServerOut)
def toggle_server(
    server_id: int, enabled: bool, session: Session = Depends(get_session)
) -> McpServerOut:
    srv = session.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server 不存在")
    if not enabled:
        mcp_client.invalidate({"command": srv.command, "args": srv.args})
    srv.enabled = enabled
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return _out(srv)


@router.post("/{server_id}/test", response_model=McpTestOut)
def test_server(server_id: int, session: Session = Depends(get_session)) -> McpTestOut:
    srv = session.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server 不存在")
    spec = {"command": srv.command, "args": srv.args}
    try:
        tools = mcp_client.list_tools(spec)
        names = [t["name"] for t in tools]
        return McpTestOut(ok=True, detail=f"连上了，发现 {len(names)} 个工具", tools=names)
    except Exception as e:  # noqa: BLE001
        return McpTestOut(ok=False, detail=f"{type(e).__name__}: {e}", tools=[])
