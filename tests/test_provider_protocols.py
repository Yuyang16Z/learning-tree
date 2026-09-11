"""Protocol wire tests use real SDK serialization and an in-memory HTTP transport.

No credentials, live providers, or the user's SQLite database are used.
"""

import json
import os

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
