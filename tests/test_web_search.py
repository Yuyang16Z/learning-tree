"""Search regression tests: no real network requests or credentials."""

import sys
from types import SimpleNamespace

import httpx
import pytest

from app import tools


@pytest.fixture(autouse=True)
def isolated_search(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def unexpected_network(*args, **kwargs):
        pytest.fail("Search tests must explicitly mock every network request")

    monkeypatch.setattr(tools.httpx, "get", unexpected_network)
    monkeypatch.setattr(tools.httpx, "post", unexpected_network)
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=unexpected_network))


def test_search_without_key_returns_provider_results_and_source_links(monkeypatch):
    calls = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def text(self, query, **kwargs):
            calls.append(("text", query, kwargs))
            return [
                {
                    "title": "Python Tutorial",
                    "href": "https://docs.python.org/3/tutorial/",
                    "body": "Documentation result supplied by the mocked search provider.",
                },
                {
                    "title": "Invalid link",
                    "href": "javascript:alert(1)",
                    "body": "Must not become a result",
                },
            ]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))
    result = tools.execute_tool("web_search", {"query": "  Python tutorial  "})
    assert "Python Tutorial" in result
    assert "https://docs.python.org/3/tutorial/" in result
    assert "Documentation result supplied by the mocked search provider." in result
    assert "javascript:" not in result and "Invalid link" not in result
    assert calls == [("init", {"timeout": 15}), ("text", "Python tutorial", {"max_results": 5})]


def test_ddgs_failure_is_explicit_and_never_fabricates_hits(monkeypatch):
    class UnavailableDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            raise RuntimeError("provider is unavailable")

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=UnavailableDDGS))
    result = tools.execute_tool("web_search", {"query": "unique-search-query"})
    assert "不可用" in result and "RuntimeError" in result
    assert "未生成模拟搜索结果" in result
    assert "https://" not in result and "http://" not in result


def test_ddgs_empty_results_are_reported_as_empty(monkeypatch):
    class EmptyDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            return []

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=EmptyDDGS))
    result = tools.execute_tool("web_search", {"query": "unique-search-query"})
    assert "没有搜到结果" in result
    assert "https://" not in result and "http://" not in result


@pytest.mark.parametrize("query", ["", "  \t\n"])
@pytest.mark.parametrize("configured_key", [False, True])
def test_empty_query_never_contacts_a_provider(monkeypatch, query, configured_key):
    if configured_key:
        monkeypatch.setenv("TAVILY_API_KEY", "unit-test-tavily-token")
    result = tools.execute_tool("web_search", {"query": query})
    assert "请输入关键词" in result


def test_configured_tavily_is_still_used_and_returns_links(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "unit-test-tavily-token")
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "results": [
                    {
                        "title": "Official documentation",
                        "content": "Provider excerpt",
                        "url": "https://docs.python.org/3/",
                    },
                ]
            },
        )

    monkeypatch.setattr(tools.httpx, "post", post)
    result = tools.execute_tool("web_search", {"query": "Python documentation"})
    assert "Official documentation" in result and "Provider excerpt" in result
    assert "https://docs.python.org/3/" in result
    assert requests == [
        (
            "https://api.tavily.com/search",
            {
                "json": {
                    "api_key": "unit-test-tavily-token",
                    "query": "Python documentation",
                    "max_results": 3,
                },
                "timeout": 15,
            },
        )
    ]


@pytest.mark.parametrize("status", [401, 429, 503])
def test_tavily_http_failure_is_explicit(monkeypatch, status):
    monkeypatch.setenv("TAVILY_API_KEY", "unit-test-tavily-token")

    def post(url, **kwargs):
        return httpx.Response(
            status, request=httpx.Request("POST", url), json={"detail": "test failure"}
        )

    monkeypatch.setattr(tools.httpx, "post", post)
    result = tools.execute_tool("web_search", {"query": "Python documentation"})
    assert "失败" in result and "HTTPStatusError" in result
    assert str(status) in result
    assert "无结果" not in result and "unit-test-tavily-token" not in result
