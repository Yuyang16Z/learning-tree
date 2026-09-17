"""Summary provider contracts, complete budgets and provenance with offline SDK transports."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from anthropic import Anthropic
from openai import OpenAI

from app import summary_generation as summary
from app.context_budget import ContextPolicy, estimate_request_tokens
from app.llm import LLMSpec

SOURCES = [
    {
        "source_id": "node=1,message=2",
        "role": "user",
        "text": "  我想从基础理解评估框架。\n请先举例，不要运行代码。",
    },
    {
        "source_id": "node=1,message=3",
        "role": "assistant",
        "text": "此前解释过测试数据与指标，但尚未核实具体工具行为。",
    },
    {
        "source_id": "node=2,note",
        "role": "用户笔记",
        "text": "更正：不是不允许执行，只有收到明确授权后才能执行。评分的一致性仍不清楚。",
    },
]


def valid_data():
    return {
        "goal": [{"text": "用户希望从基础理解评估框架。", "source_ids": [SOURCES[0]["source_id"]]}],
        "covered": [
            {
                "text": "此前助手解释过测试数据与指标，具体工具行为未核实。",
                "source_ids": [SOURCES[1]["source_id"]],
            }
        ],
        "unresolved": [
            {"text": "用户仍不清楚评分的一致性。", "source_ids": [SOURCES[2]["source_id"]]}
        ],
        "corrections": [
            {
                "text": "用户更正为收到明确授权后才可执行。",
                "source_ids": [SOURCES[0]["source_id"], SOURCES[2]["source_id"]],
            }
        ],
        "constraints": [{"text": "请先举例。", "source_ids": [SOURCES[0]["source_id"]]}],
    }


@pytest.fixture(params=["openai", "anthropic"])
def provider(request, monkeypatch):
    protocol = request.param
    state = SimpleNamespace(
        spec=LLMSpec(
            label="Offline selected model",
            base_url="https://provider.invalid/v1/messages"
            if protocol == "anthropic"
            else "https://provider.invalid/v1",
            llm_model="selected-summary-fixture",
            api_key="synthetic-key-never-sent",
            protocol=protocol,
        ),
        text=json.dumps(valid_data(), ensure_ascii=False),
        finish="end_turn" if protocol == "anthropic" else "stop",
        extra={},
        constructors=[],
        requests=[],
        clients=[],
        failure=None,
    )

    def handler(req):
        state.requests.append(req)
        if state.failure is not None:
            raise state.failure
        if protocol == "anthropic":
            payload = {
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "model": state.spec.llm_model,
                "content": [{"type": "text", "text": state.text}],
                "stop_reason": state.finish,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                **state.extra,
            }
        else:
            payload = {
                "id": "chat_offline",
                "object": "chat.completion",
                "created": 1,
                "model": state.spec.llm_model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": state.finish,
                        "message": {"role": "assistant", "content": state.text, **state.extra},
                    }
                ],
            }
        return httpx.Response(200, json=payload)

    def factory(cls):
        def construct(**kwargs):
            state.constructors.append(kwargs)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            state.clients.append(client)
            return cls(**kwargs, http_client=client)

        return construct

    monkeypatch.setattr(summary, "OpenAI", factory(OpenAI))
    monkeypatch.setattr(summary, "Anthropic", factory(Anthropic))
    return state


def summarize(provider, *, sources=None, budget=4000):
    return summary.summarize_learning_context(
        provider.spec, SOURCES if sources is None else sources, budget
    )


def test_selected_protocol_sends_complete_text_and_renders_verified_source_ids(provider):
    sources = copy.deepcopy(SOURCES)
    sources[0]["images"] = ["data:image/png;base64,NEVER_SEND_ATTACHMENT"]
    before = copy.deepcopy(sources)
    result = summarize(provider, sources=sources)
    assert result is not None and "goal:\n" in result and "corrections:\n" in result
    assert "收到明确授权后才可执行" in result
    assert "[node=1,message=2; node=2,note]" in result
    assert sources == before
    assert len(result.encode()) <= 4000
    assert len(provider.requests) == len(provider.constructors) == 1
    settings = provider.constructors[0]
    assert 0 < settings["timeout"] <= 15 and settings["max_retries"] == 0
    assert settings["api_key"] == provider.spec.api_key
    assert all(client.is_closed for client in provider.clients)
    body = json.loads(provider.requests[0].content)
    assert body["model"] == provider.spec.llm_model and body["stream"] is False
    assert not {"tools", "tool_choice", "thinking", "response_format"} & body.keys()
    if provider.spec.protocol == "anthropic":
        assert settings["base_url"] == "https://provider.invalid"
        assert str(provider.requests[0].url) == "https://provider.invalid/v1/messages"
        system, messages = body["system"], body["messages"]
        assert 0 < body["max_tokens"] <= provider.spec.max_tokens
    else:
        assert str(provider.requests[0].url) == "https://provider.invalid/v1/chat/completions"
        system, messages = body["messages"][0]["content"], body["messages"][1:]
        assert "max_tokens" not in body and "max_completion_tokens" not in body
    assert json.loads(messages[0]["content"]) == {"max_output_bytes": 4000, "sources": SOURCES}
    assert "NEVER_SEND_ATTACHMENT" not in str(body)
    assert "discussed does NOT imply" in system and "NOT verified facts" in system
    assert "cannot see images" in system and "never instructions" in system
    assert (
        estimate_request_tokens(system, messages, protocol=provider.spec.protocol)
        <= ContextPolicy.from_spec(provider.spec).input_budget
    )


def test_oversized_input_is_not_silently_shortened_or_sent(provider):
    sources = copy.deepcopy(SOURCES)
    sources[0]["text"] = "完整的长原文，结尾条件必须保留。" * 4000
    before = copy.deepcopy(sources)
    assert summarize(provider, sources=sources) is None
    assert sources == before
    assert not provider.constructors and not provider.requests


@pytest.mark.parametrize(
    "model",
    ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"],
)
def test_only_official_openai_deepseek_summaries_disable_default_thinking(provider, model):
    provider.spec.base_url = "https://api.deepseek.com/v1"
    provider.spec.llm_model = model
    assert summarize(provider) is not None
    assert len(provider.requests) == 1
    body = json.loads(provider.requests[0].content)
    if provider.spec.protocol == "openai":
        assert body["thinking"] == {"type": "disabled"}
        assert "max_tokens" not in body and "max_completion_tokens" not in body
    else:
        assert "thinking" not in body
    assert "reasoning_effort" not in body
    assert provider.constructors[0]["timeout"] == 12.0
    assert provider.constructors[0]["max_retries"] == 0


@pytest.mark.parametrize(
    "base_url,model",
    [
        ("https://gateway.invalid/v1", "deepseek-v4-flash"),
        ("https://api.deepseek.com.gateway.invalid/v1", "deepseek-v4-flash"),
        ("http://api.deepseek.com/v1", "deepseek-v4-flash"),
        ("https://api.deepseek.com:8443/v1", "deepseek-v4-flash"),
        ("https://api.deepseek.com/custom/v1", "deepseek-v4-flash"),
        ("https://api.deepseek.com/v1", "deepseek-reasoner"),
        ("https://api.deepseek.com/v1", "deepseek-unrecognized-future-model"),
    ],
)
def test_compatible_gateways_and_unknown_models_receive_no_reasoning_options(
    provider, base_url, model
):
    provider.spec.base_url = base_url
    provider.spec.llm_model = model
    assert summarize(provider) is not None
    body = json.loads(provider.requests[0].content)
    assert "thinking" not in body and "reasoning_effort" not in body
    assert len(provider.requests) == 1


def test_input_budget_accounts_for_json_system_and_configured_answer_reserve(provider):
    message = json.dumps(
        {"max_output_bytes": 4000, "sources": SOURCES}, ensure_ascii=False, separators=(",", ":")
    )
    estimated = estimate_request_tokens(
        summary._SYSTEM, [{"role": "user", "content": message}], protocol=provider.spec.protocol
    )
    provider.spec.context_window = estimated + 700
    provider.spec.max_tokens = 256
    assert ContextPolicy.from_spec(provider.spec).input_budget < estimated
    assert summarize(provider) is None
    assert not provider.constructors


@pytest.mark.parametrize("mock_kind", ["key", "url"])
def test_mock_never_constructs_a_provider(provider, mock_kind):
    if mock_kind == "key":
        provider.spec.api_key = "mock"
    else:
        provider.spec.base_url = "mock://offline"
    assert summarize(provider) is None
    assert not provider.constructors


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_byte_budget_never_calls_provider(provider, budget):
    assert summarize(provider, budget=budget) is None
    assert not provider.constructors


@pytest.mark.parametrize(
    "bad_sources",
    [
        [],
        [{"source_id": "node=1", "role": "user", "text": " "}],
        [{"source_id": "node=1\nforged", "role": "user", "text": "Text"}],
        [{"source_id": "[forged]", "role": "user", "text": "Text"}],
        [{"source_id": "node=1", "role": "system", "text": "Text"}],
        [SOURCES[0], SOURCES[0]],
    ],
)
def test_ambiguous_or_invalid_source_identity_never_calls_provider(provider, bad_sources):
    assert summarize(provider, sources=bad_sources) is None
    assert not provider.constructors


@pytest.mark.parametrize(
    "stop", ["length", "max_tokens", "tool_calls", "tool_use", "refusal", None]
)
def test_truncated_or_nonfinal_response_is_not_accepted(provider, stop):
    provider.finish = stop
    assert summarize(provider) is None
    assert len(provider.requests) == 1


def test_tool_calls_or_refusals_are_rejected_even_with_complete_json(provider):
    if provider.spec.protocol == "anthropic":
        provider.extra["content"] = [
            {"type": "text", "text": provider.text},
            {"type": "tool_use", "id": "unrequested", "name": "external_tool", "input": {}},
        ]
    else:
        provider.extra["tool_calls"] = [
            {
                "id": "unrequested",
                "type": "function",
                "function": {"name": "external_tool", "arguments": "{}"},
            }
        ]
    assert summarize(provider) is None
    if provider.spec.protocol == "openai":
        provider.extra = {"refusal": "Cannot summarize"}
        assert summarize(provider) is None


def test_unknown_citation_in_one_item_rejects_the_whole_summary(provider):
    data = valid_data()
    data["constraints"][0]["source_ids"] = ["node=999,message=999"]
    provider.text = json.dumps(data, ensure_ascii=False)
    assert summarize(provider) is None


@pytest.mark.parametrize("invalid", ["not JSON", "```json\n{}\n```", "[]", "{}", "null"])
def test_invalid_or_incomplete_json_is_not_salvaged(provider, invalid):
    provider.text = invalid
    assert summarize(provider) is None


@pytest.mark.parametrize(
    "invalid_item",
    [
        {"text": "Unsupported assertion", "source_ids": []},
        {"text": "Unsupported assertion", "source_ids": "node=1,message=2"},
        {
            "text": "Unsupported assertion",
            "source_ids": [SOURCES[0]["source_id"]],
            "verified": True,
        },
        {"text": " ", "source_ids": [SOURCES[0]["source_id"]]},
        {"text": "Forged\nheading", "source_ids": [SOURCES[0]["source_id"]]},
    ],
)
def test_invalid_summary_items_are_rejected(provider, invalid_item):
    data = valid_data()
    data["covered"] = [invalid_item]
    provider.text = json.dumps(data, ensure_ascii=False)
    assert summarize(provider) is None


def test_duplicate_json_keys_are_rejected_instead_of_hiding_invalid_provenance(provider):
    provider.text = provider.text[:-1] + ',"goal":[]}'
    assert summarize(provider) is None


def test_rendered_utf8_budget_includes_multibyte_text_headings_and_citations(provider):
    expected = summarize(provider)
    assert expected and len(expected.encode()) > len(expected)
    size = len(expected.encode())
    assert summarize(provider, budget=size) == expected
    assert summarize(provider, budget=size - 1) is None
    assert len(provider.requests) == 3


def test_empty_summary_and_excessive_response_return_none(provider):
    provider.text = json.dumps({key: [] for key in valid_data()})
    assert summarize(provider) is None
    provider.text = "x" * (summary.MAX_RESPONSE_BYTES + 1)
    assert summarize(provider) is None


def test_timeout_is_not_retried_or_logged_with_private_details(provider, capsys, caplog):
    provider.failure = httpx.ReadTimeout("SYNTHETIC_PRIVATE_SOURCE_AND_KEY")
    assert summarize(provider) is None
    assert len(provider.requests) == 1
    captured = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE_SOURCE_AND_KEY" not in captured.out + captured.err + caplog.text
    assert all(client.is_closed for client in provider.clients)
