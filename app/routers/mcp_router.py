"""MCP server configuration: add, list, delete, enable/disable and test by listing tools.

Header values of remote servers are credentials: responses list header names only,
and a blank value on update keeps the stored one."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import mcp_client
from ..db import get_session
from ..models import McpServer
from ..schemas import McpServerIn, McpServerOut, McpTestOut

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _out(s: McpServer) -> McpServerOut:
    return McpServerOut(
        id=s.id,
        label=s.label,
        transport=s.transport or "stdio",
        command=s.command or "",
        args=s.args or [],
        url=s.url,
        header_names=list(s.headers or {}),
        allow_private=bool(s.allow_private),
        enabled=s.enabled,
        confirm_tools=s.confirm_tools or "writes",
    )


def _merged_headers(stored: dict | None, incoming: dict[str, str]) -> dict | None:
    """Apply submitted headers; a blank value keeps the stored value of that name."""
    kept = {name.lower(): value for name, value in (stored or {}).items()}
    merged = {}
    for name, value in incoming.items():
        if value == "":
            if name.lower() not in kept:
                raise HTTPException(422, "新的请求头需要填写值。")
            value = kept[name.lower()]
        merged[name] = value
    return merged or None


def _apply(srv: McpServer, body: McpServerIn) -> None:
    """Copy a validated body onto a row; header validation happens before any change."""
    headers = None
    if body.transport == "http":
        stored = srv.headers if srv.transport == "http" else None
        if "headers" in body.model_fields_set and body.headers is not None:
            headers = _merged_headers(stored, body.headers)
        else:
            headers = stored  # Omitted: keep every stored header.
    srv.label = body.label
    srv.enabled = body.enabled
    srv.confirm_tools = body.confirm_tools
    srv.transport = body.transport
    if body.transport == "stdio":
        # A local program needs no remote address or credentials; drop them.
        srv.command, srv.args = body.command, body.args
        srv.url, srv.headers, srv.allow_private = None, None, False
    else:
        srv.command, srv.args = "", None
        srv.url = (body.url or "").strip()
        srv.headers = headers
        srv.allow_private = body.allow_private


@router.post("", response_model=McpServerOut)
def add_server(body: McpServerIn, session: Session = Depends(get_session)) -> McpServerOut:
    srv = McpServer(label=body.label, command="")
    _apply(srv, body)
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
    # Close the session of the old identity before the row changes underneath it.
    mcp_client.invalidate(mcp_client.server_spec_for(srv))
    _apply(srv, body)
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return _out(srv)


@router.delete("/{server_id}")
def delete_server(server_id: int, session: Session = Depends(get_session)) -> dict:
    srv = session.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server 不存在")
    mcp_client.invalidate(mcp_client.server_spec_for(srv))
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
        mcp_client.invalidate(mcp_client.server_spec_for(srv))
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
    spec = mcp_client.server_spec_for(srv)
    try:
        # A test always lists again, so an upgraded server's tools show up at once.
        tools = mcp_client.list_tools(spec, refresh=True)
        names = [t["name"] for t in tools]
        return McpTestOut(ok=True, detail=f"连上了，发现 {len(names)} 个工具", tools=names)
    except Exception as e:  # noqa: BLE001
        # Fixed messages only: exception text could carry request details.
        return McpTestOut(ok=False, detail=mcp_client.describe_error(e, spec), tools=[])
