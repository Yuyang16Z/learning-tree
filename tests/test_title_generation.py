"""Title generation uses fake SDK clients; no database or network is accessed."""

import json
from types import SimpleNamespace as NS

import pytest

from app import title_generation as titles
from app.llm import LLMSpec


def spec(protocol="openai", **overrides):
    return LLMSpec(
        **{
            "label": "Title test",
            "base_url": "https://provider.test/v1",
            "llm_model": "configured-model",
            "api_key": "unit-test-key",
            "protocol": protocol,
            **overrides,
        }
    )


def response(protocol, title="Eval harness 入门", stop=None):
    if protocol == "anthropic":
        return NS(stop_reason=stop or "end_turn", content=[NS(type="text", text=title)])
    return NS(
        choices=[
            NS(
                finish_reason=stop or "stop",
                message=NS(content=title, tool_calls=None, refusal=None),
            )
        ]
    )


def fake_client(monkeypatch, protocol, result):
    calls = {"initializers": [], "requests": [], "closed": 0}

    class Client:
        def __init__(self, **kwargs):
            calls["initializers"].append(kwargs)
            self.chat = NS(completions=self)
            self.messages = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls["closed"] += 1

        def create(self, **kwargs):
            calls["requests"].append(kwargs)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(titles, "Anthropic" if protocol == "anthropic" else "OpenAI", Client)
    return calls


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_title_request_uses_question_and_only_bounded_source(monkeypatch, protocol):
    calls = fake_client(monkeypatch, protocol, response(protocol, " **“Eval harness 入门”** "))
    question = "能详细给我从最基础讲一下 Eval harness：评估框架 吗？"
    source = "AI 领域的 harness 工程，核心是模型本身只会预测下一个词。" * 100
    assert titles.summarize_question(spec(protocol), question, source) == "Eval harness 入门"
    assert len(calls["initializers"]) == len(calls["requests"]) == calls["closed"] == 1
    init = calls["initializers"][0]
    assert init["timeout"] == 12.0
    assert init["max_retries"] == 0
    assert init["api_key"] == "unit-test-key"
    assert init["base_url"] == (
        "https://provider.test" if protocol == "anthropic" else "https://provider.test/v1"
    )
    request = calls["requests"][0]
    assert request["model"] == "configured-model"
    assert request["stream"] is False
    assert "tools" not in request and "thinking" not in request
    expected_roles = ["user"] if protocol == "anthropic" else ["system", "user"]
    assert [message["role"] for message in request["messages"]] == expected_roles
    prompt = request.get("system", request["messages"][0]["content"])
    assert "NEW QUESTION determines the topic" in prompt
    assert "same language as the question" in prompt
    data = json.loads(request["messages"][-1]["content"])
    assert list(data) == ["NEW QUESTION", "SOURCE_QUOTE"]
    assert data["NEW QUESTION"] == question
    assert data["SOURCE_QUOTE"] == source[:800]
    if protocol == "anthropic":
        assert request["max_tokens"] == 160


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_no_source_or_history_is_needed(monkeypatch, protocol):
    calls = fake_client(monkeypatch, protocol, response(protocol, "How eval harness works"))
    assert titles.summarize_question(spec(protocol), "How does eval harness work?") == (
        "How eval harness works"
    )
    data = json.loads(calls["requests"][0]["messages"][-1]["content"])
    assert data == {"NEW QUESTION": "How does eval harness work?"}


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("error", [TimeoutError("private request"), RuntimeError("private key")])
def test_failure_is_silent_and_not_retried(monkeypatch, capsys, protocol, error):
    calls = fake_client(monkeypatch, protocol, error)
    assert titles.summarize_question(spec(protocol), "What is an eval harness?") is None
    assert len(calls["requests"]) == calls["closed"] == 1
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("protocol", "stop"),
    [
        ("openai", "length"),
        ("openai", "content_filter"),
        ("openai", "tool_calls"),
        ("anthropic", "max_tokens"),
        ("anthropic", "tool_use"),
        ("anthropic", "pause_turn"),
    ],
)
def test_incomplete_titles_are_not_saved(monkeypatch, protocol, stop):
    fake_client(monkeypatch, protocol, response(protocol, stop=stop))
    assert titles.summarize_question(spec(protocol), "What is an eval harness?") is None


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "**",
        "-",
        "\u200b",
        "x" * 65,
        "Title\nExplanation",
        "```\nTitle\n```",
        "Here is a suitable title: Eval harness",
        "以下是一个合适的标题：Eval harness 入门",
    ],
)
def test_invalid_outputs_keep_the_question_fallback(monkeypatch, protocol, title):
    fake_client(monkeypatch, protocol, response(protocol, title))
    assert titles.summarize_question(spec(protocol), "What is an eval harness?") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Eval harness 入门"', "Eval harness 入门"),
        ("### `Eval harness` 入门", "Eval harness 入门"),
        ("标题： **Eval harness 入门**", "Eval harness 入门"),
        ("Title: ‘Eval harness basics’", "Eval harness basics"),
        (" [Eval harness](https://example.test) basics ", "Eval harness basics"),
        ("Eval\tharness\u200b basics", "Eval harness basics"),
    ],
)
def test_title_normalization(monkeypatch, raw, expected):
    fake_client(monkeypatch, "openai", response("openai", raw))
    assert titles.summarize_question(spec(), "What is an eval harness?") == expected


def test_provider_tool_content_is_rejected(monkeypatch):
    result = response("anthropic")
    result.content.append(NS(type="tool_use", name="ignored"))
    fake_client(monkeypatch, "anthropic", result)
    assert titles.summarize_question(spec("anthropic"), "What is an eval harness?") is None
    result = response("openai")
    result.choices[0].message.tool_calls = [NS(id="ignored")]
    fake_client(monkeypatch, "openai", result)
    assert titles.summarize_question(spec(), "What is an eval harness?") is None


def test_missing_completion_or_refusal_keeps_fallback(monkeypatch):
    for result in (NS(choices=[]), response("openai")):
        if result.choices:
            result.choices[0].finish_reason = None
        fake_client(monkeypatch, "openai", result)
        assert titles.summarize_question(spec(), "What is an eval harness?") is None
    result = response("openai")
    result.choices[0].message.refusal = "Unable to summarize"
    fake_client(monkeypatch, "openai", result)
    assert titles.summarize_question(spec(), "What is an eval harness?") is None
    result = response("anthropic")
    result.stop_reason = None
    fake_client(monkeypatch, "anthropic", result)
    assert titles.summarize_question(spec("anthropic"), "What is an eval harness?") is None


@pytest.mark.parametrize("overrides", [{"api_key": "mock"}, {"base_url": "mock://demo"}])
def test_mock_model_never_makes_a_request(monkeypatch, overrides):
    def unexpected(**kwargs):
        raise AssertionError("Mock title must not initialize a provider")

    monkeypatch.setattr(titles, "Anthropic", unexpected)
    monkeypatch.setattr(titles, "OpenAI", unexpected)
    for protocol in ("openai", "anthropic"):
        assert titles.summarize_question(spec(protocol, **overrides), "Question") is None


def test_empty_question_skips_provider(monkeypatch):
    calls = fake_client(monkeypatch, "openai", response("openai"))
    assert titles.summarize_question(spec(), "  \n\t ", "Source is not a question") is None
    assert calls["initializers"] == []


def test_fallback_is_plain_compact_question_only():
    assert titles.fallback_title("  **什么是 Eval harness？**\n请从基础开始。 ") == (
        "什么是 Eval harness？ 请从基础开始。"
    )
    assert titles.fallback_title("q" * 100) == "q" * 64
    assert titles.fallback_title(" # `torch_compile` 的作用是什么？ ") == (
        "torch_compile 的作用是什么？"
    )
