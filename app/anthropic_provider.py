"""Native Anthropic Messages adapter; the rest of the app keeps its message format.

Protocol references:
https://platform.claude.com/docs/en/build-with-claude/streaming
https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
https://platform.claude.com/docs/en/build-with-claude/vision
"""

import base64
import json
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from anthropic import Anthropic


def base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    for suffix in ("/v1/messages", "/v1"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Anthropic 地址应为 http(s) 服务根地址，如 https://api.anthropic.com。")
    return value


def client(spec) -> Anthropic:
    return Anthropic(
        base_url=base_url(spec.base_url), api_key=spec.api_key, timeout=45.0, max_retries=0
    )


# Base URLs whose provider answered HTTP 400 to cache_control; remembered per process.
_CACHE_CONTROL_UNSUPPORTED: set[str] = set()
# Shorter prefixes are below the provider's cacheable minimum and would only cost a write.
CACHE_PREFIX_MIN_CHARS = 1024


def _cache_rejected(exc: Exception) -> bool:
    # Inspected only; provider error text is never logged or returned to a client.
    return getattr(exc, "status_code", None) == 400 and "cache_control" in str(exc)


def _without_cache(kwargs: dict) -> dict:
    plain = {key: value for key, value in kwargs.items() if key != "cache_control"}
    if isinstance(plain.get("system"), list):
        plain["system"] = "".join(block["text"] for block in plain["system"])
    return plain


def _open_stream(api, spec, kwargs: dict):
    """Enter a message stream; a proxy that rejects cache markers gets one plain retry."""
    manager = api.messages.stream(**kwargs)
    try:
        return manager, manager.__enter__()
    except Exception as exc:
        if "cache_control" not in kwargs or not _cache_rejected(exc):
            raise
        _CACHE_CONTROL_UNSUPPORTED.add(spec.base_url)
        manager = api.messages.stream(**_without_cache(kwargs))
        return manager, manager.__enter__()


def _image(url: str) -> dict:
    if url.startswith("data:"):
        match = re.fullmatch(r"data:(image/(?:jpeg|png|gif|webp));base64,(.+)", url, re.S)
        if not match:
            raise ValueError("Anthropic 图片须为 JPEG、PNG、GIF 或 WebP 的 base64 数据。")
        try:
            base64.b64decode(match[2], validate=True)
        except ValueError as error:
            raise ValueError("图片的 base64 数据无效，请重新上传。") from error
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": match[1], "data": match[2]},
        }
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise ValueError("Anthropic 图片须使用 base64 数据或公开的 http(s) 图片地址。")


def _content(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if content is None:
        return []
    result = []
    for block in content:
        if block.get("type") == "text":
            if block.get("text"):
                result.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image_url":
            result.append(_image(block["image_url"]["url"]))
        else:
            raise ValueError("当前 Anthropic 配置不支持这类消息附件。")
    return result


def messages(conversation: list[dict]) -> tuple[str, list[dict]]:
    """Keep ancestry and images; adjacent tool results share one user message."""
    system = []
    converted = []
    for message in conversation:
        role = message["role"]
        if role == "system":
            system.append(message.get("content") or "")
            continue
        if role == "tool":
            role = "user"
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message.get("content") or "",
                }
            ]
        elif role in ("assistant", "user"):
            # Retain the complete original assistant blocks during a tool loop,
            # including signed thinking and the order of text and tool calls.
            content = message.get("_anthropic_content")
            if content is None:
                content = _content(message.get("content"))
                for call in message.get("tool_calls", []):
                    function = call["function"]
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": function["name"],
                            "input": json.loads(function.get("arguments") or "{}"),
                        }
                    )
        else:
            raise ValueError("Anthropic 消息角色无效。")
        if not content:
            continue
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(content)
        else:
            converted.append({"role": role, "content": list(content)})
    return "\n\n".join(system), converted


def _kwargs(spec, conversation: list[dict], tools: list[dict] | None = None) -> dict:
    system, converted = messages(conversation)
    kwargs = {"model": spec.llm_model, "max_tokens": spec.max_tokens, "messages": converted}
    prefix = getattr(spec, "cache_prefix", None)
    if (
        system
        and prefix
        and len(prefix) >= CACHE_PREFIX_MIN_CHARS
        and system.startswith(prefix)
        and spec.base_url not in _CACHE_CONTROL_UNSUPPORTED
    ):
        # Block 1 is the stable head (rules and path) with an explicit breakpoint;
        # the top-level marker lets the provider also cache the growing turn tail.
        remainder = system[len(prefix) :]
        kwargs["system"] = [
            {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
            *([{"type": "text", "text": remainder}] if remainder else []),
        ]
        kwargs["cache_control"] = {"type": "ephemeral"}
    elif system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = [
            {
                "name": item["function"]["name"],
                "description": item["function"].get("description", ""),
                "input_schema": item["function"].get("parameters", {"type": "object"}),
            }
            for item in tools
        ]
    return kwargs


def _check_stop(reason: str | None, *, tools: bool = False) -> None:
    accepted = {"end_turn", "stop_sequence"}
    if tools:
        accepted.add("tool_use")
    if reason not in accepted:
        if reason == "max_tokens":
            raise RuntimeError("回答达到输出上限，可在模型设置里提高最大输出后重试。")
        raise RuntimeError(f"模型回答未完整结束（{reason or '连接中断'}），可以重试。")


def _usage_parts(usage) -> dict:
    return {
        "input": getattr(usage, "input_tokens", None),
        "creation": getattr(usage, "cache_creation_input_tokens", None),
        "read": getattr(usage, "cache_read_input_tokens", None),
        "output": getattr(usage, "output_tokens", None),
    }


def _counts(parts: dict) -> dict:
    # Total input is the sum of uncached, cache-creation and cache-read tokens.
    read = int(parts.get("read") or 0)
    creation = int(parts.get("creation") or 0)
    return {
        "input_tokens": int(parts.get("input") or 0) + creation + read,
        "cached_input_tokens": read,
        "output_tokens": int(parts.get("output") or 0),
    }


def usage_counts(usage) -> dict:
    """Normalize a final message's usage into input/cached/output token counts."""
    return _counts(_usage_parts(usage))


def _thinking(spec) -> dict:
    # Older Claude generations use fixed budgets. Current models use adaptive
    # thinking. Custom model aliases follow the current protocol default.
    legacy = re.search(
        r"claude-(?:3[-.]7|(?:sonnet|opus|haiku)-4(?:[-.][015])?(?:-|$))", spec.llm_model
    )
    modern = re.search(r"claude-(?:sonnet|opus|haiku)-4[-.][6-9]", spec.llm_model)
    if legacy and not modern:
        if spec.max_tokens <= 1024:
            raise ValueError("该模型启用深度思考时，最大输出须大于 1024。")
        return {"type": "enabled", "budget_tokens": max(1024, min(2048, spec.max_tokens // 2))}
    return {"type": "adaptive"}


def stream_chat(
    spec, system: str, history: list[dict], deep: bool = False
) -> Iterator[tuple[str, str]]:
    """Yield (kind, text) chunks; the generator returns the usage counts when reported."""
    kwargs = _kwargs(spec, [{"role": "system", "content": system}, *history])
    if deep:
        kwargs["thinking"] = _thinking(spec)
    stopped, reason = False, None
    parts: dict | None = None
    with client(spec) as api:
        try:
            stream = api.messages.create(**kwargs, stream=True)
        except Exception as exc:
            if "cache_control" not in kwargs or not _cache_rejected(exc):
                raise
            _CACHE_CONTROL_UNSUPPORTED.add(spec.base_url)
            stream = api.messages.create(**_without_cache(kwargs), stream=True)
        with stream:
            for event in stream:
                if event.type == "message_start":
                    parts = _usage_parts(event.message.usage)
                elif event.type == "content_block_start" and event.content_block.type == "text":
                    if event.content_block.text:
                        yield "text", event.content_block.text
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield "text", event.delta.text
                    elif event.delta.type == "thinking_delta":
                        yield "reasoning", event.delta.thinking
                elif event.type == "message_delta":
                    reason = event.delta.stop_reason or reason
                    delta_usage = getattr(event, "usage", None)
                    if delta_usage is not None:
                        # Delta counts are cumulative; the start event's output count
                        # is a placeholder, so later values replace earlier ones.
                        parts = parts or {}
                        for key, value in _usage_parts(delta_usage).items():
                            if value is not None:
                                parts[key] = value
                elif event.type == "message_stop":
                    stopped = True
            if not stopped:
                raise RuntimeError("模型连接在回答完成前断开，可以重试。")
            _check_stop(reason)
    return _counts(parts) if parts else None


def chat_once(spec, conversation: list[dict], tools: list[dict], deep: bool = False):
    """Stream one tool-capable turn: yield (kind, text) chunks, then return the result dict.

    Tool calls are only final once the message is complete and its stop reason
    was accepted, so the caller sees them in the returned dict, never mid-stream.
    """
    kwargs = _kwargs(spec, conversation, tools)
    if deep:
        kwargs["thinking"] = _thinking(spec)
    stopped = False
    with client(spec) as api:
        manager, stream = _open_stream(api, spec, kwargs)
        try:
            for event in stream:
                # One helper event family only; raw deltas would double the text.
                if event.type == "text":
                    yield "text", event.text
                elif event.type == "thinking":
                    yield "reasoning", event.thinking
                elif event.type == "message_stop":
                    stopped = True
            if not stopped:
                raise RuntimeError("模型连接在回答完成前断开，可以重试。")
            response = stream.get_final_message()
        finally:
            manager.__exit__(None, None, None)
    _check_stop(response.stop_reason, tools=bool(tools))
    calls = [
        {"id": block.id, "name": block.name, "args": block.input}
        for block in response.content
        if block.type == "tool_use"
    ]
    if response.stop_reason == "tool_use" and not calls:
        raise RuntimeError("模型没有返回完整的工具参数，可以重试。")
    return {
        "content": "".join(block.text for block in response.content if block.type == "text"),
        # Opaque redacted-thinking data and signatures are retained only in the
        # protocol blocks below, never exposed as user-visible reasoning.
        "reasoning": "".join(
            block.thinking for block in response.content if block.type == "thinking"
        ),
        "tool_calls": calls,
        "_anthropic_content": [
            block.model_dump(mode="json", exclude_none=True) for block in response.content
        ],
        "usage": usage_counts(response.usage),
    }


def complete(spec, system: str, history: list[dict]) -> str:
    return "".join(value for kind, value in stream_chat(spec, system, history) if kind == "text")


def model_limits(spec) -> dict:
    """Published limits for the configured model id, from the provider's model info."""
    with client(spec) as api:
        info = api.models.retrieve(spec.llm_model)
    return {
        "context_window": getattr(info, "max_input_tokens", None),
        "max_output_tokens": getattr(info, "max_tokens", None),
    }


def test_connection(spec) -> None:
    with client(spec) as api:
        api.messages.create(
            model=spec.llm_model, max_tokens=1, messages=[{"role": "user", "content": "ping"}]
        )
