"""Protocol wire tests use real SDK serialization and an in-memory HTTP transport.

No credentials, live providers, or the user's SQLite database are used.
"""

import json
import os
from copy import deepcopy
from dataclasses import replace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import httpx
import pytest
from anthropic import Anthropic
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlalchemy import text
from sqlmodel import Session, create_engine, select

from app import anthropic_provider as adapter
from app import db, llm
from app.context_budget import HISTORY_OMISSION, ContextPolicy, estimate_request_tokens, fit_request
from app.llm import LLMSpec
from app.models import ModelConfig
from app.schemas import ModelConfigIn
from app.service import to_spec

SPEC = LLMSpec(
    "Claude test",
    "https://provider.test/proxy/v1/messages",
    "claude-sonnet-4-6",
    "unit-test-key",
    protocol="anthropic",
    max_tokens=4096,
)
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "mcp_1_lookup",
            "description": "查询",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }
]


def sse(blocks=None, stop="end_turn", *, terminal=True):
    blocks = blocks or [{"type": "text", "text": "你好"}]
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": SPEC.llm_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        }
    ]
    for index, block in enumerate(blocks):
        initial = dict(block)
        if block["type"] == "text":
            initial["text"] = ""
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif block["type"] == "thinking":
            initial.update(thinking="", signature="")
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        elif block["type"] == "redacted_thinking":
            deltas = []
        else:
            initial["input"] = {}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        events.append({"type": "content_block_start", "index": index, "content_block": initial})
        events.extend(
            {"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas
        )
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 12},
        }
    )
    if terminal:
        events.append({"type": "message_stop"})
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def mock_anthropic(monkeypatch, responses):
    requests, bodies, transports = [], [], []

    def handler(request):
        requests.append(request)
        bodies.append(json.loads(request.content))
        response = responses.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response)

    def client(spec):
        transport = httpx.Client(transport=httpx.MockTransport(handler))
        transports.append(transport)
        return Anthropic(
            base_url=adapter.base_url(spec.base_url),
            api_key=spec.api_key,
            http_client=transport,
            max_retries=0,
        )

    monkeypatch.setattr(adapter, "client", client)
    return requests, bodies, transports


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/", "/v1/messages"])
def test_anthropic_base_url_prevents_duplicate_api_path(suffix):
    assert adapter.base_url("https://provider.test/proxy" + suffix) == "https://provider.test/proxy"


def test_stream_wire_has_native_headers_context_and_images(monkeypatch):
    req, bodies, transports = mock_anthropic(monkeypatch, [sse()])
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "图中是什么"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
            ],
        },
        {"role": "assistant", "content": "前面的完整回答"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "那这张呢"},
                {"type": "image_url", "image_url": {"url": "https://images.test/sample.png"}},
            ],
        },
    ]
    assert list(llm.stream_chat(SPEC, "系统背景", history)) == [("text", "你好")]
    assert req[0].url.path == "/proxy/v1/messages"
    assert req[0].headers["x-api-key"] == "unit-test-key"
    assert "anthropic-version" in req[0].headers
    assert "authorization" not in req[0].headers
    payload = bodies[0]
    assert (
        payload["system"] == "系统背景"
        and payload["stream"] is True
        and payload["max_tokens"] == 4096
    )
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["messages"][0]["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": PNG,
    }
    assert payload["messages"][1]["content"][0]["text"] == "前面的完整回答"
    assert payload["messages"][2]["content"][1]["source"]["type"] == "url"
    assert all(transport.is_closed for transport in transports)


@pytest.mark.parametrize(
    "url", ["data:image/svg+xml;base64,YQ==", "data:image/png;base64,???", "file:///tmp/image.png"]
)
def test_invalid_image_is_explicit_and_never_silently_dropped(monkeypatch, url):
    _, bodies, _ = mock_anthropic(monkeypatch, [])
    with pytest.raises(ValueError):
        list(
            llm.stream_chat(
                SPEC,
                "",
                [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}],
            )
        )
    assert not bodies


@pytest.mark.parametrize(
    "stop,terminal", [("max_tokens", True), ("pause_turn", True), ("end_turn", False)]
)
def test_stream_preserves_partial_text_but_rejects_truncation(monkeypatch, stop, terminal):
    _, _, transports = mock_anthropic(monkeypatch, [sse(stop=stop, terminal=terminal)])
    response = llm.stream_chat(SPEC, "", [{"role": "user", "content": "问题"}])
    assert next(response) == ("text", "你好")
    with pytest.raises(RuntimeError):
        list(response)
    assert all(transport.is_closed for transport in transports)


def test_generator_cancellation_closes_sdk_stream(monkeypatch):
    _, _, transports = mock_anthropic(monkeypatch, [sse()])
    response = llm.stream_chat(SPEC, "", [{"role": "user", "content": "问题"}])
    assert next(response) == ("text", "你好")
    response.close()
    assert transports[0].is_closed


def test_stream_error_preserves_prefix_and_does_not_retry(monkeypatch):
    prefix = sse(terminal=False)
    failure = b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"temporarily unavailable"}}\n\n'
    _, bodies, transports = mock_anthropic(monkeypatch, [prefix + failure])
    response = llm.stream_chat(SPEC, "", [{"role": "user", "content": "问题"}])
    assert next(response) == ("text", "你好")
    with pytest.raises(Exception):
        list(response)
    assert len(bodies) == 1 and transports[0].is_closed


def test_thinking_deltas_and_current_thinking_request(monkeypatch):
    _, bodies, _ = mock_anthropic(
        monkeypatch,
        [
            sse(
                [
                    {"type": "thinking", "thinking": "思考摘要", "signature": "signed"},
                    {"type": "text", "text": "答案"},
                ]
            )
        ],
    )
    assert list(llm.stream_chat(SPEC, "", [{"role": "user", "content": "问题"}], deep=True)) == [
        ("reasoning", "思考摘要"),
        ("text", "答案"),
    ]
    assert bodies[0]["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize(
    "model,thinking",
    [
        ("claude-sonnet-4-6", {"type": "adaptive"}),
        ("claude-sonnet-4-5", {"type": "enabled", "budget_tokens": 2048}),
    ],
)
def test_deep_tool_loop_keeps_thinking_budget_signatures_and_private_blocks(
    monkeypatch, model, thinking
):
    first_blocks = [
        {
            "type": "thinking",
            "thinking": "检索前的可见思考",
            "signature": "private-first-signature",
        },
        {"type": "redacted_thinking", "data": "opaque-private-thinking"},
        {"type": "tool_use", "id": "toolu_read", "name": "mcp_1_lookup", "input": {"q": "原文"}},
    ]
    final_blocks = [
        {"type": "thinking", "thinking": "依据原文整理", "signature": "private-final-signature"},
        {"type": "text", "text": "回答"},
    ]
    _, bodies, transports = mock_anthropic(
        monkeypatch, [sse(first_blocks, stop="tool_use"), sse(final_blocks)]
    )
    spec = replace(SPEC, llm_model=model, max_tokens=4096, context_window=12288)
    history = [
        {"role": "user", "content": "早期问题" * 6000},
        {"role": "assistant", "content": "早期回答" * 6000},
        {"role": "user", "content": "请深度思考并回读原文。"},
    ]
    original_history = deepcopy(history)
    executed = []

    def execute(name, arguments):
        executed.append((name, arguments))
        return "完整原文与限制条件。" * 5000

    events = list(llm.run_agent(spec, "固定规则", history, TOOLS, execute, deep=True))
    assert executed == [("mcp_1_lookup", {"q": "原文"})]
    assert [event["text"] for event in events if event["type"] == "reasoning"] == [
        "检索前的可见思考",
        "依据原文整理",
    ]
    assert events[-1] == {"type": "delta", "text": "回答"}
    assert history == original_history and len(bodies) == 2
    assert all(body["thinking"] == thinking and body["max_tokens"] == 4096 for body in bodies)
    if thinking["type"] == "enabled":
        assert 1024 <= thinking["budget_tokens"] < spec.max_tokens
    for body in bodies:
        assert body["system"].startswith("固定规则") and HISTORY_OMISSION in body["system"]
        assert (
            estimate_request_tokens(
                body["system"], body["messages"], body["tools"], protocol="anthropic"
            )
            <= ContextPolicy.from_spec(spec).input_budget
        )
    assert bodies[1]["messages"][-2]["content"] == first_blocks
    assert bodies[1]["messages"][-1]["content"][0]["tool_use_id"] == "toolu_read"
    visible = json.dumps(events, ensure_ascii=False)
    assert "private-first-signature" not in visible and "private-final-signature" not in visible
    assert "opaque-private-thinking" not in visible
    assert all(transport.is_closed for transport in transports)


def test_default_tool_loop_keeps_three_argument_adapters_compatible(monkeypatch):
    called = []

    def legacy_adapter(spec, messages, tools):
        called.append((spec, messages, tools))
        return {"content": "回答", "tool_calls": []}

    monkeypatch.setattr(adapter, "chat_once", legacy_adapter)
    events = list(
        llm.run_agent(
            SPEC, "固定规则", [{"role": "user", "content": "问题"}], TOOLS, lambda *args: ""
        )
    )
    assert events == [{"type": "delta", "text": "回答"}] and len(called) == 1

    called.clear()
    monkeypatch.setattr(llm, "_chat_once", legacy_adapter)
    events = list(
        llm.run_agent(
            SPEC, "固定规则", [{"role": "user", "content": "问题"}], TOOLS, lambda *args: ""
        )
    )
    assert events == [{"type": "delta", "text": "回答"}] and len(called) == 1


def test_parallel_tool_results_and_signed_blocks_are_preserved(monkeypatch):
    content = [
        {"type": "thinking", "thinking": "思考摘要", "signature": "untouched-signature"},
        {"type": "text", "text": "先查两个来源"},
        {"type": "tool_use", "id": "toolu_a", "name": "mcp_1_lookup", "input": {"q": "A"}},
        {"type": "tool_use", "id": "toolu_b", "name": "mcp_1_lookup", "input": {"q": "B"}},
    ]
    _, bodies, _ = mock_anthropic(monkeypatch, [sse(content, stop="tool_use"), sse()])
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return f"资料-{args['q']}"

    events = list(
        llm.run_agent(SPEC, "系统", [{"role": "user", "content": "查资料"}], TOOLS, execute)
    )
    assert executed == [("mcp_1_lookup", {"q": "A"}), ("mcp_1_lookup", {"q": "B"})]
    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_end",
        "tool_start",
        "tool_end",
        "delta",
    ]
    assert events[-1]["text"] == "你好"
    assert bodies[0]["tools"] == [
        {
            "name": "mcp_1_lookup",
            "description": "查询",
            "input_schema": TOOLS[0]["function"]["parameters"],
        }
    ]
    assert bodies[1]["messages"][-2]["content"] == content
    assert bodies[1]["messages"][-1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a", "content": "资料-A"},
            {"type": "tool_result", "tool_use_id": "toolu_b", "content": "资料-B"},
        ],
    }


def test_incomplete_tool_arguments_never_execute(monkeypatch):
    _, bodies, _ = mock_anthropic(
        monkeypatch,
        [
            sse(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_a",
                        "name": "mcp_1_lookup",
                        "input": {"q": "A"},
                    }
                ],
                stop="max_tokens",
            )
        ],
    )
    with pytest.raises(RuntimeError):
        list(
            llm.run_agent(
                SPEC,
                "",
                [{"role": "user", "content": "查"}],
                TOOLS,
                lambda *args: pytest.fail("an incomplete call must not execute"),
            )
        )
    assert len(bodies) == 1


def test_complete_and_memory_extraction_use_anthropic(monkeypatch):
    _, bodies, _ = mock_anthropic(
        monkeypatch,
        [sse(), sse([{"type": "text", "text": '{"preferences":["简洁"],"facts":["资料结论"]}'}])],
    )
    assert llm.complete(SPEC, "摘要指令", [{"role": "user", "content": "学习材料"}]) == "你好"
    assert llm.extract_memories(SPEC, "问题", "答案") == {
        "preferences": ["简洁"],
        "facts": ["资料结论"],
    }
    assert bodies[0]["system"] == "摘要指令"
    assert "记忆提取器" in bodies[1]["system"]


def test_anthropic_connection_test_and_errors_do_not_echo_secrets(monkeypatch):
    response = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": SPEC.llm_model,
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "max_tokens",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    _, bodies, _ = mock_anthropic(
        monkeypatch,
        [
            httpx.Response(200, json=response),
            httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "unit-test-key must never leak",
                    },
                },
            ),
        ],
    )
    assert llm.test_connection(SPEC) == (True, "连接成功")
    assert bodies[0]["max_tokens"] == 1 and "stream" not in bodies[0]
    ok, detail = llm.test_connection(SPEC)
    assert not ok and "401" in detail and "unit-test-key" not in detail and "leak" not in detail


def test_openai_wire_compatibility_and_default_protocol(monkeypatch):
    requests, bodies = [], []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        bodies.append(body)
        response = {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "created": 1,
            "model": "custom-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "旧协议仍可用"},
                }
            ],
        }
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        llm,
        "_client",
        lambda spec: OpenAI(
            base_url=spec.base_url,
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    assert spec.protocol == "openai"
    assert llm.complete(spec, "系统", [{"role": "user", "content": "问题"}]) == "旧协议仍可用"
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer synthetic-key"
    assert bodies[0]["messages"][0] == {"role": "system", "content": "系统"}
    assert "max_tokens" not in bodies[0] and "max_completion_tokens" not in bodies[0]


def test_openai_stream_and_tool_loop_keep_existing_wire_format(monkeypatch):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            chunks = [
                {
                    "id": "chat_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "custom-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": None,
                            "delta": {"role": "assistant", "content": "原流式"},
                        }
                    ],
                },
                {
                    "id": "chat_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "custom-model",
                    "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
                },
            ]
            content = (
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=content
            )
        has_result = any(message["role"] == "tool" for message in body["messages"])
        message = {"role": "assistant", "content": "工具答案" if has_result else None}
        if not has_result:
            message["tool_calls"] = [
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "mcp_1_lookup", "arguments": '{"q":"A"}'},
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": "chat_test",
                "object": "chat.completion",
                "created": 1,
                "model": "custom-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop" if has_result else "tool_calls",
                        "message": message,
                    }
                ],
            },
        )

    monkeypatch.setattr(
        llm,
        "_client",
        lambda spec: OpenAI(
            base_url=spec.base_url,
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    assert list(llm.stream_chat(spec, "系统", [{"role": "user", "content": "问题"}])) == [
        ("text", "原流式")
    ]
    events = list(
        llm.run_agent(
            spec, "系统", [{"role": "user", "content": "问题"}], TOOLS, lambda *args: "查到资料"
        )
    )
    assert events[-1] == {"type": "delta", "text": "工具答案"}
    assert bodies[-1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_one",
        "content": "查到资料",
    }
    assert all("max_tokens" not in body and "max_completion_tokens" not in body for body in bodies)


@pytest.mark.parametrize("operation", ["stream", "complete"])
def test_non_tool_requests_budget_history_before_provider_conversion(monkeypatch, operation):
    _, bodies, _ = mock_anthropic(monkeypatch, [sse()])
    spec = replace(SPEC, max_tokens=1024, context_window=8192)
    history = [
        {"role": "user", "content": "早期问题" * 6000},
        {"role": "assistant", "content": "早期回答" * 6000},
        {"role": "user", "content": "当前问题的末尾条件不可丢失。"},
    ]
    original_history = deepcopy(history)
    if operation == "stream":
        assert list(llm.stream_chat(spec, "固定指令", history)) == [("text", "你好")]
    else:
        assert llm.complete(spec, "固定指令", history) == "你好"
    assert history == original_history
    assert len(bodies) == 1 and bodies[0]["system"].startswith("固定指令")
    assert HISTORY_OMISSION in bodies[0]["system"]
    assert bodies[0]["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": history[-1]["content"]}],
    }
    assert "早期问题" * 6000 not in json.dumps(bodies[0], ensure_ascii=False)
    assert (
        estimate_request_tokens(bodies[0]["system"], bodies[0]["messages"], protocol="anthropic")
        <= ContextPolicy.from_spec(spec).input_budget
    )


def test_openai_budget_is_applied_after_each_large_tool_result(monkeypatch):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        round_number = len(bodies)
        message = {"role": "assistant", "content": "有依据的答案"}
        if round_number < 3:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{round_number}",
                        "type": "function",
                        "function": {
                            "name": "mcp_1_lookup",
                            "arguments": json.dumps({"q": f"来源{round_number}"}),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "chat_budget",
                "object": "chat.completion",
                "created": 1,
                "model": "custom-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if round_number < 3 else "stop",
                        "message": message,
                    }
                ],
            },
        )

    monkeypatch.setattr(
        llm,
        "_client",
        lambda spec: OpenAI(
            base_url=spec.base_url,
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )
    spec = LLMSpec(
        "预算测试",
        "https://provider.test/v1",
        "custom-model",
        "synthetic-key",
        max_tokens=1024,
        context_window=8192,
    )
    question = "比较两个来源，保留它们的关键限制。"
    history = [
        {"role": "user", "content": "很早的问题" * 3000},
        {"role": "assistant", "content": "很早的回答" * 3000},
        {"role": "user", "content": question},
    ]
    original_history = deepcopy(history)
    original_tools = deepcopy(TOOLS)
    large_result = "工具检索的完整材料。" * 6000
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return large_result

    events = list(llm.run_agent(spec, "固定助教规则", history, TOOLS, execute))
    assert executed == [
        ("mcp_1_lookup", {"q": "来源1"}),
        ("mcp_1_lookup", {"q": "来源2"}),
    ]
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "有依据的答案"
    assert [event["result"] for event in events if event["type"] == "tool_end"] == [
        large_result,
        large_result,
    ]
    assert history == original_history and TOOLS == original_tools
    assert len(bodies) == 3
    for body in bodies:
        messages = body["messages"]
        assert messages[0]["content"].startswith("固定助教规则")
        assert HISTORY_OMISSION in messages[0]["content"]
        assert next(m for m in reversed(messages) if m["role"] == "user")["content"] == question
        assert body["tools"] == TOOLS
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        # The actual SDK wire request is already within the shared input budget.
        checked = fit_request(
            messages[0]["content"],
            messages[1:],
            body["tools"],
            policy=ContextPolicy.from_spec(spec),
            protocol="openai",
        )
        assert checked.estimated_input_tokens <= ContextPolicy.from_spec(spec).input_budget
        assert checked.system == messages[0]["content"]
        assert checked.messages == messages[1:]
        for index, message in enumerate(messages):
            if message.get("tool_calls"):
                calls = message["tool_calls"]
                results = messages[index + 1 : index + 1 + len(calls)]
                assert [result["tool_call_id"] for result in results] == [
                    call["id"] for call in calls
                ]
                assert all(result["role"] == "tool" for result in results)
    assert any(
        len(message["content"]) < len(large_result)
        for body in bodies[1:]
        for message in body["messages"]
        if message["role"] == "tool"
    )


def test_anthropic_budget_preserves_signed_blocks_images_and_tool_ids(monkeypatch):
    signed_blocks = [
        {"type": "thinking", "thinking": "检查两个来源", "signature": "unaltered-signature"},
        {"type": "text", "text": "先查询"},
        {"type": "tool_use", "id": "toolu_a", "name": "mcp_1_lookup", "input": {"q": "A"}},
        {"type": "tool_use", "id": "toolu_b", "name": "mcp_1_lookup", "input": {"q": "B"}},
    ]
    _, bodies, transports = mock_anthropic(
        monkeypatch, [sse(signed_blocks, stop="tool_use"), sse()]
    )
    spec = replace(SPEC, max_tokens=1024, context_window=8192)
    question = [
        {"type": "text", "text": "结合当前图片比较两个来源。"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
    ]
    history = [
        {"role": "user", "content": "过期背景问题" * 3000},
        {"role": "assistant", "content": "过期背景回答" * 3000},
        {"role": "user", "content": question},
    ]
    original_history = deepcopy(history)
    large_result = "查询结果：具体结论与条件。" * 6000
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return large_result

    prepared_requests = []

    def capture_request(*args, **kwargs):
        prepared = fit_request(*args, **kwargs)
        prepared_requests.append(prepared)
        return prepared

    monkeypatch.setattr(llm, "fit_request", capture_request)
    events = list(llm.run_agent(spec, "固定系统指令", history, TOOLS, execute))
    assert executed == [("mcp_1_lookup", {"q": "A"}), ("mcp_1_lookup", {"q": "B"})]
    assert events[-1] == {"type": "delta", "text": "你好"}
    assert history == original_history and len(bodies) == 2
    assert len(prepared_requests) == 2
    assert all(
        prepared.estimated_input_tokens <= ContextPolicy.from_spec(spec).input_budget
        for prepared in prepared_requests
    )
    for body in bodies:
        assert body["system"].startswith("固定系统指令")
        assert HISTORY_OMISSION in body["system"]
        assert (
            estimate_request_tokens(
                body["system"], body["messages"], body["tools"], protocol="anthropic"
            )
            <= ContextPolicy.from_spec(spec).input_budget
        )
        blocks = [block for message in body["messages"] for block in message["content"]]
        assert {"type": "text", "text": question[0]["text"]} in blocks
        assert {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG},
        } in blocks
    assert bodies[1]["messages"][-2]["content"] == signed_blocks
    results = bodies[1]["messages"][-1]["content"]
    assert [result["tool_use_id"] for result in results] == ["toolu_a", "toolu_b"]
    assert all(result["type"] == "tool_result" for result in results)
    assert all(len(result["content"]) < len(large_result) for result in results)
    assert all(transport.is_closed for transport in transports)


def test_existing_database_and_model_api_keep_keys_and_ids(tmp_path, monkeypatch):
    from app.main import app

    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE modelconfig (id INTEGER PRIMARY KEY, label TEXT NOT NULL, base_url TEXT NOT NULL, llm_model TEXT NOT NULL, api_key TEXT NOT NULL, is_default BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO modelconfig VALUES (7, '原模型', 'https://old.test/v1', 'old-model', 'synthetic-secret', 1, '2026-01-01')"
            )
        )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db.get_session] = session_override
    try:
        api = TestClient(app)
        original = api.get("/models").json()[0]
        assert (
            original["id"] == 7
            and original["protocol"] == "openai"
            and original["max_tokens"] == 4096
        )
        assert "synthetic-secret" not in json.dumps(original) and "api_key" not in original
        payload = {
            "label": "原模型",
            "base_url": "https://proxy.test",
            "llm_model": "claude-custom",
            "api_key": "",
            "protocol": "anthropic",
            "max_tokens": 8192,
            "is_default": True,
        }
        changed = api.put("/models/7", json=payload)
        assert changed.status_code == 200 and changed.json()["protocol"] == "anthropic"
        with Session(engine) as session:
            configs = session.exec(select(ModelConfig)).all()
            assert (
                len(configs) == 1
                and configs[0].id == 7
                and configs[0].api_key == "synthetic-secret"
            )
            assert (
                to_spec(configs[0]).protocol == "anthropic"
                and to_spec(configs[0]).max_tokens == 8192
            )
        payload.update(label="新配置", api_key="new-synthetic-key")
        created = api.post("/models", json=payload)
        assert created.status_code == 200 and created.json()["protocol"] == "anthropic"
        assert "new-synthetic-key" not in created.text
        assert not api.get("/models").json()[0]["is_default"]
        payload["protocol"] = "unsupported"
        assert api.post("/models", json=payload).status_code == 422
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize("limit", [0, -1, 131073])
def test_model_output_limits_are_validated(limit):
    with pytest.raises(ValueError):
        ModelConfigIn(
            label="测试",
            base_url="https://test",
            llm_model="test",
            api_key="test",
            max_tokens=limit,
        )
