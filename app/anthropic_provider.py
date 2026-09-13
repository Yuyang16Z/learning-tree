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
    if system:
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
    kwargs = _kwargs(spec, [{"role": "system", "content": system}, *history])
    if deep:
        kwargs["thinking"] = _thinking(spec)
    stopped, reason = False, None
    with client(spec) as api:
        with api.messages.create(**kwargs, stream=True) as stream:
            for event in stream:
                if event.type == "content_block_start" and event.content_block.type == "text":
                    if event.content_block.text:
                        yield "text", event.content_block.text
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield "text", event.delta.text
                    elif event.delta.type == "thinking_delta":
                        yield "reasoning", event.delta.thinking
                elif event.type == "message_delta":
                    reason = event.delta.stop_reason or reason
                elif event.type == "message_stop":
                    stopped = True
            if not stopped:
                raise RuntimeError("模型连接在回答完成前断开，可以重试。")
            _check_stop(reason)


def chat_once(spec, conversation: list[dict], tools: list[dict], deep: bool = False) -> dict:
    kwargs = _kwargs(spec, conversation, tools)
    if deep:
        kwargs["thinking"] = _thinking(spec)
    with client(spec) as api:
        # Streaming internally avoids SDK limits on long non-streaming requests.
        with api.messages.stream(**kwargs) as stream:
            stopped = any(event.type == "message_stop" for event in stream)
            if not stopped:
                raise RuntimeError("模型连接在回答完成前断开，可以重试。")
            response = stream.get_final_message()
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
    }


def complete(spec, system: str, history: list[dict]) -> str:
    return "".join(value for kind, value in stream_chat(spec, system, history) if kind == "text")


def test_connection(spec) -> None:
    with client(spec) as api:
        api.messages.create(
            model=spec.llm_model, max_tokens=1, messages=[{"role": "user", "content": "ping"}]
        )
