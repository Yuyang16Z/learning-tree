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


def sse(blocks=None, stop="end_turn", *, terminal=True, usage=None, start_usage=None):
    """Anthropic event stream; start_usage replaces message_start's usage verbatim."""
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
                "usage": (
                    start_usage
                    if start_usage is not None
                    else {"input_tokens": 10, "output_tokens": 1, **(usage or {})}
                ),
            },
        }
    ]
    for index, block in enumerate(blocks):
        initial = dict(block)
        if block["type"] == "text":
            # Some proxies put the first text in the block start; "prefix" emulates them.
            initial["text"] = initial.pop("prefix", "")
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


CACHE_PREFIX = "固定规则。" + "x" * 1100
CACHE_TAIL = "\n当前节点：当前问题（node=9）"


def test_cache_prefix_becomes_a_cached_system_block_with_a_top_level_marker(monkeypatch):
    monkeypatch.setattr(adapter, "_CACHE_CONTROL_UNSUPPORTED", set())
    _, bodies, _ = mock_anthropic(monkeypatch, [sse(), sse([TOOL_USE], stop="tool_use"), sse()])
    history = [{"role": "user", "content": "问题"}]
    diagnostics = {}
    assert list(
        llm.stream_chat(
            SPEC,
            CACHE_PREFIX + CACHE_TAIL,
            history,
            diagnostics=diagnostics,
            cache_prefix=CACHE_PREFIX,
        )
    ) == [("text", "你好")]
    assert bodies[0]["system"] == [
        {"type": "text", "text": CACHE_PREFIX, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": CACHE_TAIL},
    ]
    assert bodies[0]["cache_control"] == {"type": "ephemeral"}
    assert diagnostics["rounds"][0]["input_tokens"] == 10
    events = list(
        llm.run_agent(
            SPEC,
            CACHE_PREFIX + CACHE_TAIL,
            history,
            TOOLS,
            lambda *args: "资料",
            cache_prefix=CACHE_PREFIX,
        )
    )
    assert events[-1] == {"type": "delta", "text": "你好"}
    for body in bodies[1:]:
        assert body["system"][0] == {
            "type": "text",
            "text": CACHE_PREFIX,
            "cache_control": {"type": "ephemeral"},
        }
        assert body["system"][1]["text"].startswith(CACHE_TAIL)
        assert body["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(bodies[1]["messages"]), "stored blocks are untouched"


TOOL_USE = {"type": "tool_use", "id": "toolu_a", "name": "mcp_1_lookup", "input": {"q": "A"}}


@pytest.mark.parametrize(
    "prefix,system",
    [
        ("短前缀", "短前缀\n其余系统提示"),  # too short for its own breakpoint
        (CACHE_PREFIX, "另一段开头" + CACHE_PREFIX),  # fit_request replaced the system
        (None, CACHE_PREFIX + CACHE_TAIL),  # no prefix supplied
    ],
    ids=["short-prefix", "replaced-system", "no-prefix"],
)
def test_system_stays_a_plain_string_without_a_usable_cache_prefix(monkeypatch, prefix, system):
    monkeypatch.setattr(adapter, "_CACHE_CONTROL_UNSUPPORTED", set())
    _, bodies, _ = mock_anthropic(monkeypatch, [sse()])
    list(llm.stream_chat(SPEC, system, [{"role": "user", "content": "问题"}], cache_prefix=prefix))
    assert bodies[0]["system"] == system
    # An answer request (one that supplies a prefix) still caches its conversation.
    assert bodies[0].get("cache_control") == ({"type": "ephemeral"} if prefix else None)


def test_short_stable_heads_still_cache_the_conversation_of_answer_requests(monkeypatch):
    monkeypatch.setattr(adapter, "_CACHE_CONTROL_UNSUPPORTED", set())
    _, bodies, _ = mock_anthropic(
        monkeypatch, [sse(), sse([TOOL_USE], stop="tool_use"), sse(), sse()]
    )
    head = "固定规则。\n学习路径：根节点（node=1）"
    history = [{"role": "user", "content": "问题"}]
    list(llm.stream_chat(SPEC, head + CACHE_TAIL, history, cache_prefix=head))
    list(
        llm.run_agent(SPEC, head + CACHE_TAIL, history, TOOLS, lambda *a: "资料", cache_prefix=head)
    )
    assert llm.complete(SPEC, head, history) == "你好"
    answers, summary = bodies[:3], bodies[3]
    assert all(body["cache_control"] == {"type": "ephemeral"} for body in answers)
    assert all(isinstance(body["system"], str) for body in answers)
    assert "cache_control" not in summary, "requests without a prefix stay unmarked"


@pytest.mark.parametrize("path", ["stream", "agent"])
def test_cache_control_rejection_retries_plain_once_and_is_remembered(monkeypatch, path):
    monkeypatch.setattr(adapter, "_CACHE_CONTROL_UNSUPPORTED", set())
    rejection = httpx.Response(
        400,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "cache_control is not supported by this proxy; unit-test-key",
            },
        },
    )
    _, bodies, transports = mock_anthropic(monkeypatch, [rejection, sse(), sse()])
    history = [{"role": "user", "content": "问题"}]

    def run():
        if path == "stream":
            return list(
                llm.stream_chat(SPEC, CACHE_PREFIX + CACHE_TAIL, history, cache_prefix=CACHE_PREFIX)
            )
        return list(
            llm.run_agent(
                SPEC,
                CACHE_PREFIX + CACHE_TAIL,
                history,
                TOOLS,
                lambda *a: "",
                cache_prefix=CACHE_PREFIX,
            )
        )

    assert run()[-1] in (("text", "你好"), {"type": "delta", "text": "你好"})
    assert isinstance(bodies[0]["system"], list) and bodies[0]["cache_control"]
    assert bodies[1]["system"] == CACHE_PREFIX + CACHE_TAIL and "cache_control" not in bodies[1]
    assert run()[-1] in (("text", "你好"), {"type": "delta", "text": "你好"})
    assert len(bodies) == 3 and bodies[2]["system"] == CACHE_PREFIX + CACHE_TAIL
    assert adapter._CACHE_CONTROL_UNSUPPORTED == {SPEC.base_url}
    assert all(transport.is_closed for transport in transports)


def test_anthropic_usage_sums_cache_tokens_into_input_and_reports_cached_reads(monkeypatch):
    cached = {"cache_creation_input_tokens": 3, "cache_read_input_tokens": 7}
    _, _, _ = mock_anthropic(monkeypatch, [sse(usage=cached), sse(), sse()])
    diagnostics = {}
    assert list(
        llm.stream_chat(
            SPEC, "系统", [{"role": "user", "content": "问题"}], diagnostics=diagnostics
        )
    ) == [("text", "你好")]
    (round_one,) = diagnostics["rounds"]
    # Total input = uncached + cache creation + cache read; output comes from message_delta.
    assert round_one["input_tokens"] == 20 and round_one["cached_input_tokens"] == 7
    assert round_one["output_tokens"] == 12
    assert round_one["estimated_input_tokens"] == estimate_request_tokens(
        "系统", [{"role": "user", "content": "问题"}], protocol="anthropic"
    )
    plain = {}
    list(llm.stream_chat(SPEC, "系统", [{"role": "user", "content": "问题"}], diagnostics=plain))
    assert (
        plain["rounds"][0]["input_tokens"] == 10 and plain["rounds"][0]["cached_input_tokens"] == 0
    )
    assert llm.complete(SPEC, "系统", [{"role": "user", "content": "问题"}], diagnostics=plain)
    assert len(plain["rounds"]) == 2 and plain["rounds"][1]["output_tokens"] == 12


@pytest.mark.parametrize("path", ["stream", "agent"])
def test_anthropic_usage_without_input_counts_is_unknown_rather_than_zero(monkeypatch, path):
    # A proxy that leaves the input counts out of message_start reports nothing.
    mock_anthropic(monkeypatch, [sse(start_usage={"output_tokens": 1})])
    history = [{"role": "user", "content": "问题"}]
    diagnostics = {}
    if path == "stream":
        list(llm.stream_chat(SPEC, "系统", history, diagnostics=diagnostics))
    else:
        list(llm.run_agent(SPEC, "系统", history, TOOLS, lambda *a: "", diagnostics=diagnostics))
    (round_one,) = diagnostics["rounds"]
    assert round_one["input_tokens"] is None and round_one["cached_input_tokens"] is None
    assert round_one["output_tokens"] == 12
    # A reported cache read alone still counts as input.
    mock_anthropic(monkeypatch, [sse(start_usage={"cache_read_input_tokens": 7})])
    partial = {}
    list(llm.stream_chat(SPEC, "系统", history, diagnostics=partial))
    assert partial["rounds"][0]["input_tokens"] == 7
    assert partial["rounds"][0]["cached_input_tokens"] == 7


def test_anthropic_tool_loop_records_every_round_and_counts_tool_rounds(monkeypatch):
    first = [{"type": "tool_use", "id": "toolu_a", "name": "mcp_1_lookup", "input": {"q": "A"}}]
    _, _, _ = mock_anthropic(monkeypatch, [sse(first, stop="tool_use"), sse()])
    diagnostics = {}
    events = list(
        llm.run_agent(
            SPEC,
            "系统",
            [{"role": "user", "content": "查资料"}],
            TOOLS,
            lambda *args: "资料",
            diagnostics=diagnostics,
        )
    )
    assert events[-1] == {"type": "delta", "text": "你好"}
    assert diagnostics["tool_rounds"] == 1 and len(diagnostics["rounds"]) == 2
    assert all(r["input_tokens"] == 10 and r["output_tokens"] == 12 for r in diagnostics["rounds"])
    assert (
        diagnostics["rounds"][1]["estimated_input_tokens"]
        > (diagnostics["rounds"][0]["estimated_input_tokens"])
    )


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
    # Narration streams before the calls run; a paragraph break separates it from
    # the next round, whose text arrives as it streams instead of re-chunked.
    assert [event["type"] for event in events] == [
        "delta",
        "tool_start",
        "tool_end",
        "tool_start",
        "tool_end",
        "delta",
        "delta",
    ]
    assert events[0]["text"] == "先查两个来源" and events[-2]["text"] == "\n\n"
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


TOOL_HISTORY_REJECTION = (
    "Requests which include `tool_use` or `tool_result` blocks must define tools."
)


@pytest.mark.parametrize("deep", [False, True])
def test_anthropic_final_round_keeps_tool_definitions_and_forbids_calls(monkeypatch, deep):
    bodies = []
    responses = [sse([TOOL_USE], stop="tool_use"), sse([{"type": "text", "text": "根据资料作答"}])]

    def handler(request):
        # Anthropic refuses tool blocks in a request that defines no tools.
        body = json.loads(request.content)
        bodies.append(body)
        blocks = [
            block
            for message in body["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
        ]
        if not body.get("tools") and any(b["type"] in ("tool_use", "tool_result") for b in blocks):
            error = {"type": "invalid_request_error", "message": TOOL_HISTORY_REJECTION}
            return httpx.Response(400, json={"type": "error", "error": error})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=responses.pop(0)
        )

    monkeypatch.setattr(
        adapter,
        "client",
        lambda spec: Anthropic(
            base_url=adapter.base_url(spec.base_url),
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )
    events = list(
        llm.run_agent(
            SPEC,
            "系统",
            [{"role": "user", "content": "查资料"}],
            TOOLS,
            lambda *args: "资料",
            max_rounds=1,
            deep=deep,
        )
    )
    assert events[-1] == {"type": "delta", "text": "根据资料作答"}
    assert len(bodies) == 2 and "tool_choice" not in bodies[0]
    # The synthesis round still defines the same tools, so the tool history stays valid
    # and the cached prefix (tools first) still matches, but no call is allowed.
    assert bodies[1]["tools"] == bodies[0]["tools"]
    assert bodies[1]["tool_choice"] == {"type": "none"}
    assert "工具调用已达本轮上限" in bodies[1]["system"]
    assert bodies[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_anthropic_tool_turns_keep_text_a_proxy_sends_in_the_block_start(monkeypatch):
    first = [{"type": "text", "prefix": "我先", "text": "查一下。"}, TOOL_USE]
    final = [{"type": "text", "prefix": "前缀", "text": "正文"}]
    _, bodies, _ = mock_anthropic(monkeypatch, [sse(first, stop="tool_use"), sse(final)])
    events = list(
        llm.run_agent(SPEC, "系统", [{"role": "user", "content": "查"}], TOOLS, lambda *a: "资料")
    )
    deltas = [event["text"] for event in events if event["type"] == "delta"]
    assert "".join(deltas) == "我先查一下。\n\n前缀正文"
    assert bodies[1]["messages"][-2]["content"][0] == {"type": "text", "text": "我先查一下。"}
    # The plain stream already relayed such a prefix; both paths now agree.
    _, _, _ = mock_anthropic(monkeypatch, [sse(final)])
    assert list(llm.stream_chat(SPEC, "系统", [{"role": "user", "content": "问"}])) == [
        ("text", "前缀"),
        ("text", "正文"),
    ]


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


def openai_client(handler):
    return lambda spec: OpenAI(
        base_url=spec.base_url,
        api_key=spec.api_key,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def tool_call_fragments(calls):
    """Split each (index, id, name, arguments) call into id/name and argument fragments."""
    opening = [
        {
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": ""},
        }
        for index, call_id, name, _ in calls
    ]
    halves = [
        [
            {"index": index, "function": {"arguments": arguments[: len(arguments) // 2]}}
            for index, _, _, arguments in calls
        ],
        [
            {"index": index, "function": {"arguments": arguments[len(arguments) // 2 :]}}
            for index, _, _, arguments in calls
        ],
    ]
    return [
        ({"role": "assistant", "content": None, "tool_calls": opening}, None),
        ({"tool_calls": halves[0]}, None),
        ({"tool_calls": halves[1]}, None),
        ({}, "tool_calls"),
    ]


def openai_stream(chunks, usage=None):
    frames = [
        {
            "id": "chat_test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "custom-model",
            "choices": [{"index": 0, "finish_reason": finish, "delta": delta}],
        }
        for delta, finish in chunks
    ]
    if usage is not None:
        # The usage chunk carries an empty choices list before the terminal frame.
        frames.append(
            {
                "id": "chat_test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "custom-model",
                "choices": [],
                "usage": usage,
            }
        )
    content = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=content + "data: [DONE]\n\n"
    )


@pytest.mark.parametrize(
    "usage,cached",
    [
        (
            {
                "prompt_tokens": 120,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 64},
            },
            64,
        ),
        (
            {
                "prompt_tokens": 120,
                "completion_tokens": 7,
                "prompt_cache_hit_tokens": 48,
                "prompt_cache_miss_tokens": 72,
            },
            48,
        ),
        ({"prompt_tokens": 120, "completion_tokens": 7}, 0),
    ],
)
def test_openai_stream_requests_and_reads_the_usage_chunk(monkeypatch, usage, cached):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return openai_stream([({"role": "assistant", "content": "答"}, None), ({}, "stop")], usage)

    monkeypatch.setattr(llm, "_client", openai_client(handler))
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    diagnostics = {}
    assert list(
        llm.stream_chat(
            spec, "系统", [{"role": "user", "content": "问题"}], diagnostics=diagnostics
        )
    ) == [("text", "答")]
    assert bodies[0]["stream_options"] == {"include_usage": True}
    (round_one,) = diagnostics["rounds"]
    assert round_one["input_tokens"] == 120 and round_one["output_tokens"] == 7
    assert round_one["cached_input_tokens"] == cached
    assert round_one["estimated_input_tokens"] == estimate_request_tokens(
        "系统", [{"role": "user", "content": "问题"}]
    )


def test_openai_usage_without_prompt_tokens_is_unknown_rather_than_zero(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    answer = [({"role": "assistant", "content": "答"}, None), ({}, "stop")]
    monkeypatch.setattr(
        llm,
        "_client",
        openai_client(lambda request: openai_stream(answer, {"completion_tokens": 3})),
    )
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    diagnostics = {}
    list(
        llm.stream_chat(
            spec, "系统", [{"role": "user", "content": "问题"}], diagnostics=diagnostics
        )
    )
    (round_one,) = diagnostics["rounds"]
    assert round_one["input_tokens"] is None and round_one["cached_input_tokens"] is None
    assert round_one["output_tokens"] == 3


def test_openai_stream_options_rejection_falls_back_once_per_base_url(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "stream_options" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "Unknown parameter: stream_options", "type": "invalid"}},
            )
        return openai_stream([({"role": "assistant", "content": "答"}, None), ({}, "stop")])

    monkeypatch.setattr(llm, "_client", openai_client(handler))
    spec = LLMSpec("兼容", "https://gateway.test/v1", "custom-model", "synthetic-key")
    diagnostics = {}
    assert list(
        llm.stream_chat(
            spec, "系统", [{"role": "user", "content": "问题"}], diagnostics=diagnostics
        )
    ) == [("text", "答")]
    assert ["stream_options" in body for body in bodies] == [True, False]
    assert diagnostics["rounds"][0]["input_tokens"] is None
    list(llm.stream_chat(spec, "系统", [{"role": "user", "content": "再问"}]))
    assert len(bodies) == 3 and "stream_options" not in bodies[2]
    assert "gateway.test" in "".join(llm._STREAM_OPTIONS_UNSUPPORTED)

    def other_errors(request):
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    monkeypatch.setattr(llm, "_client", openai_client(other_errors))
    with pytest.raises(Exception):
        list(llm.stream_chat(replace(spec, base_url="https://other.test/v1"), "系统", []))
    assert "other.test" not in "".join(llm._STREAM_OPTIONS_UNSUPPORTED)


def test_openai_stream_and_tool_loop_keep_existing_wire_format(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        if not body.get("tools"):
            return openai_stream([({"role": "assistant", "content": "原流式"}, None), ({}, "stop")])
        has_result = any(message["role"] == "tool" for message in body["messages"])
        if has_result:
            return openai_stream(
                [({"role": "assistant", "content": "工具答案"}, None), ({}, "stop")],
                {"prompt_tokens": 30, "completion_tokens": 5},
            )
        return openai_stream(
            tool_call_fragments([(0, "call_one", "mcp_1_lookup", '{"q":"A"}')]),
            {"prompt_tokens": 20, "completion_tokens": 5},
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
    diagnostics = {}
    events = list(
        llm.run_agent(
            spec,
            "系统",
            [{"role": "user", "content": "问题"}],
            TOOLS,
            lambda *args: "查到资料",
            diagnostics=diagnostics,
        )
    )
    assert events[-1] == {"type": "delta", "text": "工具答案"}
    assert [event["type"] for event in events] == ["tool_start", "tool_end", "delta"]
    assert [r["input_tokens"] for r in diagnostics["rounds"]] == [20, 30]
    assert diagnostics["tool_rounds"] == 1
    assert bodies[-1]["messages"][-2]["tool_calls"] == [
        {
            "id": "call_one",
            "type": "function",
            "function": {"name": "mcp_1_lookup", "arguments": '{"q": "A"}'},
        }
    ]
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
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        round_number = len(bodies)
        if round_number < 3:
            call = (
                0,
                f"call_{round_number}",
                "mcp_1_lookup",
                json.dumps({"q": f"来源{round_number}"}),
            )
            return openai_stream(tool_call_fragments([call]))
        return openai_stream(
            [({"role": "assistant", "content": "有依据的答案"}, None), ({}, "stop")]
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


def test_openai_streamed_tool_arguments_are_assembled_by_index(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if any(message["role"] == "tool" for message in body["messages"]):
            # The final answer arrives in two pieces and must reach the caller unchanged.
            return openai_stream(
                [
                    ({"role": "assistant", "content": "根据两份资料，"}, None),
                    ({"content": "结论成立。"}, None),
                    ({}, "stop"),
                ]
            )
        # Two parallel calls whose argument fragments interleave across chunks.
        return openai_stream(
            [
                ({"role": "assistant", "content": "先查"}, None),
                (
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "mcp_1_lookup", "arguments": ""},
                            }
                        ]
                    },
                    None,
                ),
                (
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"q":"'}},
                            {
                                "index": 1,
                                "id": "call_b",
                                "type": "function",
                                "function": {"name": "mcp_1_lookup", "arguments": '{"q":"B"'},
                            },
                        ]
                    },
                    None,
                ),
                (
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'A"}'}},
                            {"index": 1, "function": {"arguments": "}"}},
                        ]
                    },
                    None,
                ),
                ({}, "tool_calls"),
            ],
            {"prompt_tokens": 40, "completion_tokens": 9},
        )

    monkeypatch.setattr(llm, "_client", openai_client(handler))
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return f"资料-{args['q']}"

    diagnostics = {}
    events = list(
        llm.run_agent(
            spec,
            "系统",
            [{"role": "user", "content": "查两份资料"}],
            TOOLS,
            execute,
            diagnostics=diagnostics,
        )
    )
    assert executed == [("mcp_1_lookup", {"q": "A"}), ("mcp_1_lookup", {"q": "B"})]
    assert [event["type"] for event in events] == [
        "delta",
        "tool_start",
        "tool_end",
        "tool_start",
        "tool_end",
        "delta",
        "delta",
        "delta",
    ]
    assert [event["text"] for event in events if event["type"] == "delta"] == [
        "先查",
        "\n\n",
        "根据两份资料，",
        "结论成立。",
    ]
    assert [event["name"] for event in events if event["type"] == "tool_start"] == [
        "mcp_1_lookup"
    ] * 2
    assistant = bodies[1]["messages"][-3]
    assert assistant["content"] == "先查"
    assert [
        (call["id"], json.loads(call["function"]["arguments"])) for call in assistant["tool_calls"]
    ] == [
        ("call_a", {"q": "A"}),
        ("call_b", {"q": "B"}),
    ]
    assert [m["tool_call_id"] for m in bodies[1]["messages"] if m["role"] == "tool"] == [
        "call_a",
        "call_b",
    ]
    assert diagnostics["rounds"][0]["input_tokens"] == 40 and diagnostics["tool_rounds"] == 1


def test_openai_malformed_or_truncated_tool_arguments_never_execute(monkeypatch):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    responses = [
        openai_stream(
            [
                (
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "mcp_1_lookup", "arguments": '{"q":'},
                            }
                        ],
                    },
                    None,
                ),
                ({}, "tool_calls"),
            ]
        ),
        openai_stream(
            [
                (
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "mcp_1_lookup", "arguments": '{"q":"A"}'},
                            }
                        ],
                    },
                    None,
                ),
                ({}, "length"),
            ]
        ),
    ]
    monkeypatch.setattr(llm, "_client", openai_client(lambda request: responses.pop(0)))
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            list(
                llm.run_agent(
                    spec,
                    "系统",
                    [{"role": "user", "content": "查"}],
                    TOOLS,
                    lambda *args: pytest.fail("an incomplete call must not execute"),
                )
            )
    assert not responses


def lookup_fragment(arguments, call_id=None, index=None, name="mcp_1_lookup"):
    fragment = {"function": {"arguments": arguments}}
    if name:
        fragment["function"]["name"] = name
        fragment["type"] = "function"
    if call_id:
        fragment["id"] = call_id
    if index is not None:
        fragment["index"] = index
    return fragment


@pytest.mark.parametrize(
    "fragments,expected",
    [
        # Qwen3 behind some OpenAI-compatible servers opens a call at index 1 and
        # streams its continuations at index 0 without an id or name.
        (
            [
                lookup_fragment('{"q":', "call_a", 1),
                lookup_fragment('"A"}', index=0, name=None),
            ],
            [("call_a", {"q": "A"})],
        ),
        # Two complete calls that share index 0 but carry different ids.
        (
            [
                lookup_fragment('{"q":"A"}', "call_a", 0),
                lookup_fragment('{"q":"B"}', "call_b", 0),
            ],
            [("call_a", {"q": "A"}), ("call_b", {"q": "B"})],
        ),
        # Argument pieces of one call that carry no index at all.
        (
            [lookup_fragment('{"q":', "call_a"), lookup_fragment('"A"}', name=None)],
            [("call_a", {"q": "A"})],
        ),
        # Complete calls without an index, as Gemini- and Ollama-style servers send them.
        (
            [lookup_fragment('{"q":"A"}', "call_a"), lookup_fragment('{"q":"B"}', "call_b")],
            [("call_a", {"q": "A"}), ("call_b", {"q": "B"})],
        ),
        (
            [lookup_fragment('{"q":"A"}'), lookup_fragment('{"q":"B"}')],
            [("call_0", {"q": "A"}), ("call_1", {"q": "B"})],
        ),
    ],
    ids=[
        "index-drift",
        "shared-index",
        "split-without-index",
        "complete-without-index",
        "complete-without-index-or-id",
    ],
)
def test_openai_tool_fragments_survive_shifted_reused_or_missing_indexes(
    monkeypatch, fragments, expected
):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) > 1:
            return openai_stream([({"role": "assistant", "content": "答案"}, None), ({}, "stop")])
        chunks = [({"role": "assistant", "content": None, "tool_calls": fragments[:1]}, None)]
        chunks += [({"tool_calls": [fragment]}, None) for fragment in fragments[1:]]
        return openai_stream([*chunks, ({}, "tool_calls")])

    monkeypatch.setattr(llm, "_client", openai_client(handler))
    spec = LLMSpec("兼容", "https://provider.test/v1", "qwen3-32b", "synthetic-key")
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return f"资料-{args['q']}"

    events = list(llm.run_agent(spec, "系统", [{"role": "user", "content": "查"}], TOOLS, execute))
    assert executed == [("mcp_1_lookup", args) for _, args in expected]
    assert events[-1] == {"type": "delta", "text": "答案"}
    calls = bodies[1]["messages"][-1 - len(expected)]["tool_calls"]
    assert [(c["id"], json.loads(c["function"]["arguments"])) for c in calls] == expected
    results = [m["tool_call_id"] for m in bodies[1]["messages"] if m["role"] == "tool"]
    assert results == [call_id for call_id, _ in expected]


@pytest.mark.parametrize("arguments", ["null", "[]", '"text"', "3"])
def test_tool_arguments_that_are_not_objects_never_execute(monkeypatch, arguments):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return openai_stream(tool_call_fragments([(0, "call_a", "mcp_1_lookup", arguments)]))
        return openai_stream(
            [({"role": "assistant", "content": "已按定义重试"}, None), ({}, "stop")]
        )

    monkeypatch.setattr(llm, "_client", openai_client(handler))
    spec = LLMSpec("兼容", "https://provider.test/v1", "custom-model", "synthetic-key")
    events = list(
        llm.run_agent(
            spec,
            "系统",
            [{"role": "user", "content": "查"}],
            TOOLS,
            lambda *args: pytest.fail("non-object arguments must not execute"),
        )
    )
    assert [event["type"] for event in events] == ["tool_start", "tool_end", "delta"]
    assert events[0]["args"] == json.loads(arguments)
    # The model gets a fixed error it can recover from instead of a failed answer.
    assert json.loads(events[1]["result"])["error"] == "invalid_arguments"
    assert bodies[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_a",
        "content": events[1]["result"],
    }
    assert events[-1]["text"] == "已按定义重试"


REASONING_REJECTION = "The `reasoning_content` in the thinking mode must be passed back to the API."


def reasoning_endpoint(bodies, first_round, *, thinking):
    """Emulate the two OpenAI-compatible message rules around reasoning_content.

    A thinking endpoint (DeepSeek) streams the field and, once a request no longer ends
    on a user message, rejects it unless every assistant message carries the field (an
    empty string is accepted). A strict endpoint (OpenAI) never streams it and rejects
    any message that carries it.
    """

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        messages = body["messages"]
        carried = ["reasoning_content" in m for m in messages if m["role"] == "assistant"]
        if thinking and messages[-1]["role"] != "user" and not all(carried):
            return httpx.Response(400, json={"error": {"message": REASONING_REJECTION}})
        if not thinking and any("reasoning_content" in m for m in messages):
            return httpx.Response(
                400,
                json={"error": {"message": "Additional properties are not allowed"}},
            )
        if len(bodies) == 1:
            return openai_stream(first_round)
        final = [({"role": "assistant", "content": "有依据的答案"}, None), ({}, "stop")]
        if thinking:
            final.insert(
                0, ({"role": "assistant", "content": None, "reasoning_content": "整理"}, None)
            )
        return openai_stream(final)

    return handler


LOOKUP_CALL = {
    "index": 0,
    "id": "call_a",
    "type": "function",
    "function": {"name": "mcp_1_lookup", "arguments": '{"q":"A"}'},
}


@pytest.mark.parametrize(
    "first_round,thinking,echoed",
    [
        (
            [
                ({"role": "assistant", "content": None, "reasoning_content": "先想想，"}, None),
                ({"reasoning_content": "需要查资料。"}, None),
                ({"tool_calls": [LOOKUP_CALL]}, None),
                ({}, "tool_calls"),
            ],
            True,
            "先想想，需要查资料。",
        ),
        # An empty chain of thought still announces the field, as null.
        (
            [
                (
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": None,
                        "tool_calls": [LOOKUP_CALL],
                    },
                    None,
                ),
                ({}, "tool_calls"),
            ],
            True,
            "",
        ),
        (
            [
                ({"role": "assistant", "content": None, "tool_calls": [LOOKUP_CALL]}, None),
                ({}, "tool_calls"),
            ],
            False,
            None,
        ),
    ],
    ids=["thinking", "empty-thinking", "strict"],
)
def test_openai_tool_rounds_pass_reasoning_content_back_only_to_endpoints_that_send_it(
    monkeypatch, first_round, thinking, echoed
):
    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    bodies = []
    monkeypatch.setattr(
        llm, "_client", openai_client(reasoning_endpoint(bodies, first_round, thinking=thinking))
    )
    spec = LLMSpec("兼容", "https://provider.test/v1", "deepseek-flash", "synthetic-key")
    history = [
        {"role": "user", "content": "早先的问题"},
        {"role": "assistant", "content": "早先的回答"},
        {"role": "user", "content": "现在的问题"},
    ]
    original_history = deepcopy(history)
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return "资料"

    events = list(llm.run_agent(spec, "系统", history, TOOLS, execute))
    assert executed == [("mcp_1_lookup", {"q": "A"})]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "有依据的答案"
    assert history == original_history and len(bodies) == 2
    assistants = [m for m in bodies[1]["messages"] if m["role"] == "assistant"]
    assert [m["content"] for m in assistants] == ["早先的回答", None]
    if echoed is None:
        assert not any("reasoning_content" in m for body in bodies for m in body["messages"])
    else:
        # The tool-call turn returns its own reasoning; an earlier answer, whose
        # reasoning is never replayed, carries the field empty.
        assert [m["reasoning_content"] for m in assistants] == ["", echoed]


def test_anthropic_narration_streams_before_tool_start_and_final_text_is_not_rechunked(monkeypatch):
    first = [
        {"type": "text", "text": "我先查一下来源。"},
        {"type": "tool_use", "id": "toolu_a", "name": "mcp_1_lookup", "input": {"q": "A"}},
    ]
    final_text = "这是一个很长的最终回答，必须作为整段送达，而不是被切成四个字符的片段。"
    _, bodies, _ = mock_anthropic(
        monkeypatch, [sse(first, stop="tool_use"), sse([{"type": "text", "text": final_text}])]
    )
    started = []

    def execute(name, args):
        started.append(len([event for event in seen if event["type"] == "tool_start"]))
        return "资料"

    seen = []
    for event in llm.run_agent(SPEC, "系统", [{"role": "user", "content": "查"}], TOOLS, execute):
        seen.append(event)
    assert [event["type"] for event in seen] == [
        "delta",
        "tool_start",
        "tool_end",
        "delta",
        "delta",
    ]
    assert seen[0]["text"] == "我先查一下来源。", "narration is relayed before the call starts"
    assert started == [1], "the call ran after tool_start, once the message was complete"
    assert seen[-1]["text"] == final_text and len(final_text) > 4
    assert bodies[1]["messages"][-2]["content"] == first
    assert bodies[1]["messages"][-1]["content"][0]["tool_use_id"] == "toolu_a"


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
