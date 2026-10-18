"""Persistent MCP sessions with a thread-safe synchronous public API.

A server is a local program (stdio) or a remote Streamable HTTP endpoint. Each
server has one queue actor on a dedicated event-loop thread. The actor opens and
closes every SDK context manager in the same task, as AnyIO requires.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import copy
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import StreamableHTTPError, streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from . import url_guard

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 12.0
MAX_OUTPUT_CHARS = 24_000
# The complete text kept beside the bounded model text, for storage and paging. It
# stays above the store's 2,000,000-character limit so the store can flag a cut.
MAX_FULL_OUTPUT_CHARS = 2_100_000
MAX_PENDING_REQUESTS = 32
# A worker reuses its listed tools this long, unless the server announces a change
# (notifications/tools/list_changed) or a connection test asks for a fresh list.
TOOLS_CACHE_SECONDS = 60.0
# Remote servers: 30 s to connect and write, five minutes between streamed events.
HTTP_TIMEOUT = httpx.Timeout(30.0, read=300.0)
# The SDK reports a POST answered 404 with this code: an expired server session, or,
# for initialize, which carries no session ID yet, a URL without an MCP endpoint.
SESSION_TERMINATED = 32600
# Looked up at call time so tests can substitute a resolver.
RESOLVER = socket.getaddrinfo

# Fixed connection-test messages; exception text never reaches a client.
REMOTE_URL_INVALID = "MCP 远程地址无效：只支持不含用户名和密码的 http(s) 地址。"
REMOTE_URL_PRIVATE = "MCP 远程地址指向本机或内网；如果是本机开发服务，请在配置中允许内网地址。"
REMOTE_HOST_UNRESOLVED = "无法解析 MCP 远程地址的主机名，请检查服务地址。"
REMOTE_UNREACHABLE = "无法连接 MCP 远程服务，请检查服务地址和网络后重试。"
REMOTE_TIMEOUT = "MCP 远程服务连接超时，请检查服务地址和网络后重试。"
REMOTE_DENIED = "MCP 远程服务拒绝访问，请检查请求头中的凭据。"
REMOTE_NOT_FOUND = "MCP 远程地址不存在或不是 MCP 服务，请检查服务地址。"
REMOTE_REDIRECT = "MCP 远程服务要求重定向；为安全起见不会跟随，请填写最终地址。"
REMOTE_FAILED = "MCP 远程服务返回错误，请稍后重试。"
CONNECTION_FAILED = "MCP 连接失败，请检查配置后重试。"
# A request was sent, or may have been, and no answer arrived: the server may have run
# it, so a tool with side effects must be checked before it is called again.
CALL_TIMED_OUT = "MCP 请求超时；执行结果未确认，请先检查状态再决定是否重试。"
CALL_CLOSED = "MCP 连接已关闭；执行结果未确认。"
CALL_INTERRUPTED = "MCP 连接中断；执行结果未确认。"
WAIT_TIMED_OUT = "MCP 等待超时；执行结果未确认，请检查状态后再重试。"
OUTCOME_UNKNOWN = frozenset({CALL_TIMED_OUT, CALL_CLOSED, CALL_INTERRUPTED, WAIT_TIMED_OUT})


def server_spec(
    command: str = "",
    args: list[str] | None = None,
    *,
    transport: str = "stdio",
    url: str | None = None,
    headers: dict | None = None,
    allow_private: bool = False,
) -> dict:
    """Share the same launch identity between execution, testing and invalidation."""
    if transport == "http":
        return {
            "transport": "http",
            "url": (url or "").strip(),
            "headers": {str(name): str(value) for name, value in (headers or {}).items()},
            "allow_private": bool(allow_private),
        }
    spec: dict = {"transport": "stdio", "command": command, "args": args}
    launcher = Path(__file__).resolve().parents[1] / "integrations/mcp/launch.py"
    if (
        args
        and len(args) == 2
        and args[1] == "web-research"
        and Path(command).expanduser().resolve() == Path(sys.executable).resolve()
        and Path(args[0]).expanduser().resolve() == launcher
        and (key := os.environ.get("TAVILY_API_KEY"))
    ):
        # Search credentials are not inherited by arbitrary or other preset servers.
        spec["env"] = {"TAVILY_API_KEY": key}
    return spec


def server_spec_for(server) -> dict:
    """The identity of a stored McpServer row, built from every field that reaches the
    transport. Fixtures without the newer fields read as stdio servers."""
    return server_spec(
        getattr(server, "command", "") or "",
        getattr(server, "args", None),
        transport=getattr(server, "transport", None) or "stdio",
        url=getattr(server, "url", None),
        headers=getattr(server, "headers", None),
        allow_private=bool(getattr(server, "allow_private", False)),
    )


class MCPConnectionError(RuntimeError):
    """A lost transport allows a read-only discovery request to reconnect."""


class MCPTimeoutError(TimeoutError):
    """The execution outcome is unknown, so a tool must not be replayed."""


class MCPConfigError(RuntimeError):
    """The configuration cannot be used (for example a refused address); retrying cannot help."""


class MCPCommandNotFound(FileNotFoundError):
    """The local launch command does not exist; the message names only that command."""


class MCPRemoteStatusError(MCPConnectionError):
    """A remote server answered with an HTTP error status. Only the status is kept: the
    original exception text names the URL and could grow to include more. Mid-session it
    can mean the session is gone, so discovery reconnects once; a 3xx or 4xx answer to a
    new connection is final."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        if status_code in (401, 403):
            message = REMOTE_DENIED
        elif status_code in (404, 405):
            message = REMOTE_NOT_FOUND
        elif 300 <= status_code < 400:
            message = REMOTE_REDIRECT
        else:
            message = REMOTE_FAILED
        super().__init__(message)


def _params(spec: dict) -> StdioServerParameters:
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        str(Path.home() / ".local/bin"),
        str(Path.home() / ".cargo/bin"),
        str(Path.home() / ".bun/bin"),
    ]
    configured_env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    parts = configured_env.get("PATH", os.environ.get("PATH", "")).split(os.pathsep)
    for part in extra:
        if part not in parts and Path(part).is_dir():
            parts.append(part)
    path = os.pathsep.join(part for part in parts if part)
    raw_command = os.path.expanduser(spec["command"])
    command = shutil.which(raw_command, path=path)
    if command is None:
        raise MCPCommandNotFound(f"未找到 MCP 启动命令：{raw_command}。请确认已经安装。")
    args = [
        os.path.expanduser(str(arg)) if str(arg).startswith("~") else str(arg)
        for arg in (spec.get("args") or [])
    ]
    # Inherit only the SDK's conservative environment, not application secrets.
    env = {**get_default_environment(), **configured_env, "PATH": path}
    cwd = os.path.expanduser(spec["cwd"]) if spec.get("cwd") else None
    return StdioServerParameters(command=command, args=args, env=env, cwd=cwd)


def _identity(spec: dict) -> dict:
    if spec.get("transport") == "http":
        return {
            "transport": "http",
            "url": spec.get("url"),
            "headers": spec.get("headers") or {},
            "allow_private": bool(spec.get("allow_private")),
        }
    return {key: spec.get(key) for key in ("command", "args", "env", "cwd")}


def _key(spec: dict) -> str:
    # Environment and header values must not appear in logs, registry keys or thread
    # names, so only this digest of the identity is ever used.
    return hashlib.sha256(json.dumps(_identity(spec), sort_keys=True).encode()).hexdigest()


def _check_remote(spec: dict) -> None:
    """Refuse a remote URL before connecting: http(s) only, no credentials, and every
    resolved address public unless the server allows private addresses. The client
    resolves the name again when it connects, so DNS rebinding after this check is
    not prevented; redirects are never followed."""
    url = spec.get("url") or ""
    try:
        url_guard.validate_url(url)
    except url_guard.BlockedURL:
        raise MCPConfigError(REMOTE_URL_INVALID) from None
    if spec.get("allow_private"):
        return
    try:
        url_guard.resolve_public(url, RESOLVER)
    except url_guard.UnresolvedHost:
        raise MCPConfigError(REMOTE_HOST_UNRESOLVED) from None
    except url_guard.BlockedURL:
        raise MCPConfigError(REMOTE_URL_PRIVATE) from None


def _http_client(spec: dict) -> httpx.AsyncClient:
    # The SDK's default client follows redirects, which could lead a remote server's
    # answer to a loopback or private address; this one never does.
    return httpx.AsyncClient(
        headers=spec.get("headers") or {}, follow_redirects=False, timeout=HTTP_TIMEOUT
    )


def _bounded(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n[输出过长，已截断；请缩小查询范围后继续。]"
    return text[: max(0, limit - len(suffix))] + suffix


def _safe_structure(value: Any, depth: int = 0, *, complete: bool = False) -> Any:
    """Structured content without binary payloads; bounded unless `complete`."""
    if depth > 6:
        return "[结构过深，已省略]"
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:100]:
            return "[二进制 data URL 已省略]"
        return value if complete else _bounded(value, 2000)
    limit = None if complete else 40
    if isinstance(value, dict):
        return {
            str(k): (
                "[二进制数据已省略]"
                if str(k).lower() in {"data", "base64", "blob"}
                and isinstance(v, str)
                and len(v) > 1000
                else _safe_structure(v, depth + 1, complete=complete)
            )
            for k, v in list(value.items())[:limit]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_structure(item, depth + 1, complete=complete) for item in value[:limit]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[{type(value).__name__}]"


def _block_text(block: Any) -> str:
    kind = getattr(block, "type", "")
    if kind == "text":
        return block.text
    if kind in {"image", "audio"}:
        label = "图片" if kind == "image" else "音频"
        return (
            f"[{label}：{getattr(block, 'mimeType', '未知格式')}；"
            "当前工具通道仅传递文字，二进制内容未发送给模型。]"
        )
    if kind == "resource":
        resource = getattr(block, "resource", None)
        return (
            f"[资源 {getattr(resource, 'uri', '')}]\n"
            f"{getattr(resource, 'text', None) or '[二进制资源内容已省略]'}"
        )
    if kind == "resource_link":
        return f"[资源 {getattr(block, 'name', '')}] {getattr(block, 'uri', '')}"
    return f"[未展示的 MCP 内容类型：{kind or type(block).__name__}]"


def _result_texts(result: Any) -> tuple[str, str]:
    """(model text bounded to MAX_OUTPUT_CHARS, complete text up to MAX_FULL_OUTPUT_CHARS).

    Both drop binary content the same way; only the complete text keeps every block
    and the whole of long ones, for storage and paging."""
    parts: list[str] = []
    complete: list[str] = []
    length = complete_length = 0
    for block in result.content:
        part = _block_text(block)
        if length < MAX_OUTPUT_CHARS:
            parts.append(_bounded(part))
            length += len(parts[-1])
        if complete_length < MAX_FULL_OUTPUT_CHARS:
            complete.append(part)
            complete_length += len(part) + 1
        else:
            break
    structured = getattr(result, "structuredContent", None)
    if not parts and structured is not None:
        parts.append(json.dumps(_safe_structure(structured), ensure_ascii=False))
        complete.append(json.dumps(_safe_structure(structured, complete=True), ensure_ascii=False))
    text = "\n".join(parts) or "（工具无输出）"
    full = "\n".join(complete) or "（工具无输出）"
    if getattr(result, "isError", False):
        text = "MCP 工具报告错误：\n" + text
        full = "MCP 工具报告错误：\n" + full
    return _bounded(text), full[:MAX_FULL_OUTPUT_CHARS]


def _result_text(result: Any) -> str:
    return _result_texts(result)[0]


def _transport_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_transport_error(child) for child in exc.exceptions)
    return (
        isinstance(
            exc,
            (
                ConnectionError,
                EOFError,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
                httpx.TransportError,
                httpx.HTTPStatusError,
                StreamableHTTPError,
            ),
        )
        or isinstance(exc, McpError)
        and exc.error.code in (CONNECTION_CLOSED, SESSION_TERMINATED)
    )


def _timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_timeout_error(child) for child in exc.exceptions)
    # The installed MCP SDK represents its read deadline as HTTP status 408.
    return (
        isinstance(exc, (TimeoutError, httpx.TimeoutException))
        or isinstance(exc, McpError)
        and exc.error.code == 408
    )


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, BaseExceptionGroup):
        return next(
            (status for child in exc.exceptions if (status := _http_status(child)) is not None),
            None,
        )
    if isinstance(exc, MCPRemoteStatusError):
        return exc.status_code
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None


def _finish(
    future: concurrent.futures.Future, value: Any = None, error: BaseException | None = None
) -> None:
    try:
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)
    except concurrent.futures.InvalidStateError:
        pass  # A waiting request may already have been cancelled.


@dataclass
class _Request:
    operation: str
    name: str
    args: dict
    future: concurrent.futures.Future


class _SessionWorker:
    def __init__(self, spec: dict):
        self.spec = json.loads(json.dumps(spec))
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.actor: asyncio.Task | None = None
        self.ready = threading.Event()
        self.accepting = True
        self.cancel_sent = False
        self.error: BaseException | None = None
        self.active: _Request | None = None
        # Set once the session is initialized; failures before then are connection setup.
        self.connected = False
        # Tool-list cache, owned by the actor. The session's message handler only sets
        # the stale flag (on this loop), never touching the registry lock.
        self.tools: list[dict] | None = None
        self.tools_listed_at = 0.0
        self.tools_stale = False
        self.thread = threading.Thread(target=self._run, name="learning-tree-mcp", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            if self.error is None:
                if _timeout_error(exc):
                    self.error = MCPTimeoutError("MCP 连接超时，请检查启动命令和服务状态后重试。")
                elif (status := _http_status(exc)) is not None:
                    self.error = MCPRemoteStatusError(status)
                elif isinstance(exc, asyncio.CancelledError) or _transport_error(exc):
                    self.error = MCPConnectionError("MCP 连接已关闭")
                else:
                    self.error = exc
        finally:
            self.accepting = False
            self.error = self.error or MCPConnectionError("MCP 连接已关闭")
            if self.active is not None:
                _finish(self.active.future, error=self.error)
            if self.queue is not None:
                while not self.queue.empty():
                    _finish(self.queue.get_nowait().future, error=self.error)
            self.ready.set()

    async def _serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.actor = asyncio.current_task()
        self.queue = asyncio.Queue(maxsize=MAX_PENDING_REQUESTS)
        self.ready.set()
        if not self.accepting:
            return
        async with AsyncExitStack() as stack:
            # Enter/exit on this actor, never wait_for(context.__aenter__()).
            async with asyncio.timeout(CONNECT_TIMEOUT):
                if self.spec.get("transport") == "http":
                    # DNS lookups block; keep this loop free to receive a stop request.
                    await asyncio.to_thread(_check_remote, self.spec)
                    client = await stack.enter_async_context(_http_client(self.spec))
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.spec["url"], http_client=client)
                    )
                else:
                    read, write = await stack.enter_async_context(stdio_client(_params(self.spec)))
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
                        message_handler=self._on_message,
                    )
                )
                try:
                    await session.initialize()
                except McpError as exc:
                    # The SDK reports any 404 as "Session terminated", but initialize
                    # carries no session ID yet: the URL has no MCP endpoint.
                    if (
                        exc.error.code == SESSION_TERMINATED
                        and self.spec.get("transport") == "http"
                    ):
                        raise MCPRemoteStatusError(404) from None
                    raise
                self.connected = True
            while self.accepting:
                request = await self.queue.get()
                if not request.future.set_running_or_notify_cancel():
                    continue
                self.active = request
                try:
                    async with asyncio.timeout(CALL_TIMEOUT):
                        if request.operation == "list":
                            result = await self._cached_list(session, request.args.get("refresh"))
                        else:
                            response = await session.call_tool(
                                request.name,
                                request.args,
                                read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
                            )
                            result = _result_texts(response)
                    _finish(request.future, value=result)
                except TimeoutError:
                    self.error = MCPTimeoutError(CALL_TIMED_OUT)
                    self.accepting = False
                    _finish(request.future, error=self.error)
                except asyncio.CancelledError:
                    _finish(request.future, error=MCPConnectionError(CALL_CLOSED))
                    raise
                except Exception as exc:
                    if _timeout_error(exc):
                        self.error = MCPTimeoutError(CALL_TIMED_OUT)
                        self.accepting = False
                        _finish(request.future, error=self.error)
                    elif _transport_error(exc):
                        self.error = MCPConnectionError(CALL_INTERRUPTED)
                        self.accepting = False
                        _finish(request.future, error=self.error)
                    else:
                        _finish(request.future, error=exc)
                finally:
                    self.active = None

    async def _on_message(self, message) -> None:
        """ClientSession forwards server notifications here; a tool-list change is not
        handled by the SDK itself, so mark the cached list stale for the next request."""
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            self.tools_stale = True

    async def _cached_list(self, session: ClientSession, refresh: bool = False) -> list[dict]:
        expired = time.monotonic() - self.tools_listed_at >= TOOLS_CACHE_SECONDS
        if refresh or self.tools is None or self.tools_stale or expired:
            # Clear first: a failed listing leaves no stale copy behind, and a change
            # announced while listing marks the new list stale again.
            self.tools, self.tools_stale = None, False
            self.tools = await self._list(session)
            self.tools_listed_at = time.monotonic()
        return copy.deepcopy(self.tools)

    @staticmethod
    async def _list(session: ClientSession) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            response = await session.list_tools(
                params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools.extend(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "schema": tool.inputSchema or {"type": "object"},
                }
                for tool in response.tools
            )
            cursor = response.nextCursor
            if not cursor:
                return tools
            if cursor in seen:
                raise RuntimeError("MCP 工具清单返回了重复的分页游标")
            seen.add(cursor)
        raise RuntimeError("MCP 工具清单超过 100 页")

    def submit(self, operation: str, name: str = "", args: dict | None = None) -> Any:
        if not self.ready.wait(CONNECT_TIMEOUT):
            raise MCPTimeoutError("MCP 连接启动超时")
        future: concurrent.futures.Future = concurrent.futures.Future()
        request = _Request(operation, name, args or {}, future)

        def enqueue() -> None:
            if not self.accepting:
                _finish(future, error=self.error or MCPConnectionError("MCP 连接已关闭"))
                return
            try:
                self.queue.put_nowait(request)
            except asyncio.QueueFull:
                _finish(future, error=RuntimeError("该 MCP 正忙，请稍后再试"))

        try:
            self.loop.call_soon_threadsafe(enqueue)
        except RuntimeError:
            raise self.error or MCPConnectionError("MCP 连接已关闭") from None
        try:
            return future.result(timeout=CONNECT_TIMEOUT + CALL_TIMEOUT + SHUTDOWN_TIMEOUT)
        except concurrent.futures.TimeoutError:
            if future.done():
                raise
            # A queued request must not execute after its caller timed out.
            future.cancel()
            raise MCPTimeoutError(WAIT_TIMED_OUT) from None

    def stop(self, *, wait: bool = True) -> None:
        was_accepting = self.accepting
        self.accepting = False
        if (
            was_accepting
            and not self.cancel_sent
            and self.loop is not None
            and self.actor is not None
            and not self.loop.is_closed()
        ):
            self.cancel_sent = True
            try:
                self.loop.call_soon_threadsafe(self.actor.cancel)
            except RuntimeError:
                pass
        if wait and threading.current_thread() is not self.thread:
            self.thread.join(timeout=SHUTDOWN_TIMEOUT)


_workers: dict[str, _SessionWorker] = {}
_registry_lock = threading.Lock()


def _worker(spec: dict) -> _SessionWorker:
    key = _key(spec)
    with _registry_lock:
        worker = _workers.get(key)
        if worker is None or not worker.accepting:
            worker = _SessionWorker(spec)
            _workers[key] = worker
        return worker


def list_tools(spec: dict, *, refresh: bool = False) -> list[dict]:
    """Read every page; reconnect once after transport loss, never replay tools.

    A 3xx or 4xx answer to a new connection (wrong URL or credentials, a redirect) is
    final and not retried. The worker reuses its last list for TOOLS_CACHE_SECONDS unless
    the server announced a change; refresh=True (a connection test) always lists again."""
    worker = _worker(spec)
    try:
        return worker.submit("list", args={"refresh": refresh})
    except MCPConnectionError as exc:
        invalidate(spec, _expected=worker)
        if isinstance(exc, MCPRemoteStatusError) and exc.status_code < 500 and not worker.connected:
            raise  # The server's answer to a new connection; asking again changes nothing.
        return _worker(spec).submit("list", args={"refresh": refresh})


def call_tool(spec: dict, name: str, args: dict, full: list[str] | None = None) -> str:
    """Return bounded text. A failed or timed-out tool is never retried here.

    `full`, an optional out-parameter, receives the complete text (up to
    MAX_FULL_OUTPUT_CHARS) so a caller can store what the bound cut off."""
    try:
        text, complete = _worker(spec).submit("call", name, args)
    except Exception as exc:
        if spec.get("transport") == "http":
            # Remote failures carry no exception text: it could echo request details.
            text = f"MCP 工具 {name} 调用失败：{describe_call_error(exc, spec)}"
        else:
            text = _bounded(f"MCP 工具 {name} 调用失败：{type(exc).__name__}: {exc}", 1600)
        complete = text
    if full is not None:
        full.append(complete)
    return text


_FIXED_MESSAGES = frozenset(
    {
        "MCP 连接超时，请检查启动命令和服务状态后重试。",
        "MCP 连接已关闭",
        "MCP 工具清单返回了重复的分页游标",
        "MCP 工具清单超过 100 页",
        "MCP 连接启动超时",
        "该 MCP 正忙，请稍后再试",
        *OUTCOME_UNKNOWN,
    }
)


def describe_error(exc: BaseException, spec: dict) -> str:
    """A fixed, credential-free message for a failed connection; never exception text."""
    if isinstance(exc, (MCPConfigError, MCPRemoteStatusError, MCPCommandNotFound)):
        return str(exc)
    if spec.get("transport") == "http":
        if isinstance(exc, MCPTimeoutError):
            return REMOTE_TIMEOUT
        if isinstance(exc, MCPConnectionError):
            return REMOTE_UNREACHABLE
    message = str(exc)
    return message if message in _FIXED_MESSAGES else CONNECTION_FAILED


def describe_call_error(exc: BaseException, spec: dict) -> str:
    """A fixed, credential-free message for a failed tool call.

    A call that was sent, or may have been, and got no answer keeps its outcome-unknown
    wording: the server may have run it, and a retryable connection message would invite
    the model to run a tool with side effects again. A call that failed before anything
    was sent reads like any other failed connection."""
    message = str(exc)
    if isinstance(exc, (MCPTimeoutError, MCPConnectionError)) and message in OUTCOME_UNKNOWN:
        return message
    return describe_error(exc, spec)


def invalidate(spec: dict, *, _expected: _SessionWorker | None = None) -> None:
    """Close the old spec after editing, disabling or deleting a configuration."""
    with _registry_lock:
        worker = _workers.get(_key(spec))
        if _expected is not None and worker is not _expected:
            return
        _workers.pop(_key(spec), None)
    if worker is not None:
        worker.stop()


def close_all() -> None:
    """Lifespan integration: await asyncio.to_thread(close_all) on shutdown."""
    with _registry_lock:
        workers = list(_workers.values())
        _workers.clear()
    for worker in workers:
        worker.stop(wait=False)
    for worker in workers:
        worker.thread.join(timeout=SHUTDOWN_TIMEOUT)


atexit.register(close_all)
