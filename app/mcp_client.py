"""Persistent stdio MCP sessions with a thread-safe synchronous public API.

Each server has one queue actor on a dedicated event-loop thread. The actor
opens and closes every SDK context manager in the same task, as AnyIO requires.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import hashlib
import json
import os
import shutil
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 12.0
MAX_OUTPUT_CHARS = 24_000
MAX_PENDING_REQUESTS = 32


class MCPConnectionError(RuntimeError):
    """A lost transport allows a read-only discovery request to reconnect."""


class MCPTimeoutError(TimeoutError):
    """The execution outcome is unknown, so a tool must not be replayed."""


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
        raise FileNotFoundError(f"未找到 MCP 启动命令：{raw_command}。请确认已经安装。")
    args = [
        os.path.expanduser(str(arg)) if str(arg).startswith("~") else str(arg)
        for arg in (spec.get("args") or [])
    ]
    # Inherit only the SDK's conservative environment, not application secrets.
    env = {**get_default_environment(), **configured_env, "PATH": path}
    cwd = os.path.expanduser(spec["cwd"]) if spec.get("cwd") else None
    return StdioServerParameters(command=command, args=args, env=env, cwd=cwd)


def _key(spec: dict) -> str:
    identity = {key: spec.get(key) for key in ("command", "args", "env", "cwd")}
    # Environment values must not appear in logs, registry keys or thread names.
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _bounded(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n[输出过长，已截断；请缩小查询范围后继续。]"
    return text[: max(0, limit - len(suffix))] + suffix


def _safe_structure(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[结构过深，已省略]"
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:100]:
            return "[二进制 data URL 已省略]"
        return _bounded(value, 2000)
    if isinstance(value, dict):
        return {
            str(k): (
                "[二进制数据已省略]"
                if str(k).lower() in {"data", "base64", "blob"}
                and isinstance(v, str)
                and len(v) > 1000
                else _safe_structure(v, depth + 1)
            )
            for k, v in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_structure(item, depth + 1) for item in value[:40]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[{type(value).__name__}]"


def _result_text(result: Any) -> str:
    parts: list[str] = []
    length = 0
    for block in result.content:
        kind = getattr(block, "type", "")
        if kind == "text":
            part = block.text
        elif kind in {"image", "audio"}:
            label = "图片" if kind == "image" else "音频"
            part = (
                f"[{label}：{getattr(block, 'mimeType', '未知格式')}；"
                "当前工具通道仅传递文字，二进制内容未发送给模型。]"
            )
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            part = (
                f"[资源 {getattr(resource, 'uri', '')}]\n"
                f"{getattr(resource, 'text', None) or '[二进制资源内容已省略]'}"
            )
        elif kind == "resource_link":
            part = f"[资源 {getattr(block, 'name', '')}] {getattr(block, 'uri', '')}"
        else:
            part = f"[未展示的 MCP 内容类型：{kind or type(block).__name__}]"
        parts.append(_bounded(part))
        length += len(parts[-1])
        if length >= MAX_OUTPUT_CHARS:
            break
    if not parts and getattr(result, "structuredContent", None) is not None:
        parts.append(json.dumps(_safe_structure(result.structuredContent), ensure_ascii=False))
    text = "\n".join(parts) or "（工具无输出）"
    if getattr(result, "isError", False):
        text = "MCP 工具报告错误：\n" + text
    return _bounded(text)


def _transport_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_transport_error(child) for child in exc.exceptions)
    return (
        isinstance(
            exc, (ConnectionError, EOFError, anyio.BrokenResourceError, anyio.ClosedResourceError)
        )
        or isinstance(exc, McpError)
        and exc.error.code == CONNECTION_CLOSED
    )


def _timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_timeout_error(child) for child in exc.exceptions)
    # The installed MCP SDK represents its read deadline as HTTP status 408.
    return isinstance(exc, TimeoutError) or isinstance(exc, McpError) and exc.error.code == 408


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
        self.thread = threading.Thread(target=self._run, name="learning-tree-mcp", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            if self.error is None:
                if _timeout_error(exc):
                    self.error = MCPTimeoutError("MCP 连接超时，请检查启动命令和服务状态后重试。")
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
                read, write = await stack.enter_async_context(stdio_client(_params(self.spec)))
                session = await stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT))
                )
                await session.initialize()
            while self.accepting:
                request = await self.queue.get()
                if not request.future.set_running_or_notify_cancel():
                    continue
                self.active = request
                try:
                    async with asyncio.timeout(CALL_TIMEOUT):
                        if request.operation == "list":
                            result = await self._list(session)
                        else:
                            response = await session.call_tool(
                                request.name,
                                request.args,
                                read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
                            )
                            result = _result_text(response)
                    _finish(request.future, value=result)
                except TimeoutError:
                    self.error = MCPTimeoutError(
                        "MCP 请求超时；执行结果未确认，请先检查状态再决定是否重试。"
                    )
                    self.accepting = False
                    _finish(request.future, error=self.error)
                except asyncio.CancelledError:
                    _finish(
                        request.future, error=MCPConnectionError("MCP 连接已关闭；执行结果未确认。")
                    )
                    raise
                except Exception as exc:
                    if _timeout_error(exc):
                        self.error = MCPTimeoutError(
                            "MCP 请求超时；执行结果未确认，请先检查状态再决定是否重试。"
                        )
                        self.accepting = False
                        _finish(request.future, error=self.error)
                    elif _transport_error(exc):
                        self.error = MCPConnectionError("MCP 连接中断；执行结果未确认。")
                        self.accepting = False
                        _finish(request.future, error=self.error)
                    else:
                        _finish(request.future, error=exc)
                finally:
                    self.active = None

    @staticmethod
    async def _list(session: ClientSession) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            response = await session.list_tools(cursor=cursor)
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
            raise MCPTimeoutError("MCP 等待超时；执行结果未确认，请检查状态后再重试。") from None

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


def list_tools(spec: dict) -> list[dict]:
    """Read every page; reconnect once after transport loss, never replay tools."""
    worker = _worker(spec)
    try:
        return worker.submit("list")
    except MCPConnectionError:
        invalidate(spec, _expected=worker)
        return _worker(spec).submit("list")


def call_tool(spec: dict, name: str, args: dict) -> str:
    """Return bounded text. A failed or timed-out tool is never retried here."""
    try:
        return _worker(spec).submit("call", name, args)
    except Exception as exc:
        return _bounded(f"MCP 工具 {name} 调用失败：{type(exc).__name__}: {exc}", 1600)


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
