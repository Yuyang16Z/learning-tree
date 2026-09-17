"""Combined MCP tools use offline providers; transport discovery runs over real stdio."""

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, ErrorData

from app import mcp_client, tools
from integrations.mcp import web_research


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def unexpected(*args, **kwargs):
        pytest.fail("This test must provide an offline backend")

    monkeypatch.setattr(tools.httpx, "get", unexpected)
    monkeypatch.setattr(tools.httpx, "post", unexpected)
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=unexpected))
    monkeypatch.setattr(web_research, "_reader_backend", unexpected)


def call(name, arguments):
    return asyncio.run(web_research.call_tool(name, arguments))[0].text


def test_one_mcp_exposes_search_and_paged_reading_with_readonly_schemas():
    definitions = {tool.name: tool for tool in asyncio.run(web_research.list_tools())}
    assert set(definitions) == {"web_search", "read_webpage"}
    assert all(tool.annotations.readOnlyHint for tool in definitions.values())
    assert definitions["web_search"].inputSchema["required"] == ["query"]
    reading = definitions["read_webpage"].inputSchema
    assert reading["required"] == ["url"]
    assert reading["properties"]["max_length"]["default"] == 5000
    assert reading["properties"]["max_length"]["maximum"] == 12000
    assert reading["properties"]["start_index"]["minimum"] == 0
    assert "ignore_robots_txt" not in reading["properties"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("missing_tool", {}),
        ("web_search", {}),
        ("web_search", {"query": "   "}),
        ("web_search", {"query": "x" * 1001}),
        ("web_search", {"query": ["not a string"]}),
        ("read_webpage", {}),
        ("read_webpage", {"url": "file:///private.txt"}),
        ("read_webpage", {"url": "https://example.invalid", "start_index": -1}),
        ("read_webpage", {"url": "https://example.invalid", "max_length": 0}),
        ("read_webpage", {"url": "https://example.invalid", "max_length": 12001}),
        ("read_webpage", {"url": "https://example.invalid", "ignore_robots_txt": True}),
    ],
)
def test_invalid_requests_fail_before_any_provider_or_reader(name, args):
    with pytest.raises(McpError):
        call(name, args)


def test_search_uses_existing_key_free_backend_off_event_loop(monkeypatch):
    invoked = []
    caller = threading.get_ident()

    class Search:
        def __init__(self, **kwargs):
            invoked.append((threading.get_ident(), kwargs))

        def text(self, query, **kwargs):
            assert query == "Python tutorial" and kwargs == {"max_results": 5}
            return [
                {
                    "title": "Offline documentation",
                    "href": "https://example.invalid/docs",
                    "body": "Supplied fixture excerpt",
                }
            ]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=Search))
    result = call("web_search", {"query": "  Python tutorial  "})
    assert "Offline documentation" in result
    assert "https://example.invalid/docs" in result and "Supplied fixture excerpt" in result
    assert len(invoked) == 1 and invoked[0][0] != caller
    assert invoked[0][1] == {"timeout": 15}


def test_search_uses_explicit_tavily_key_without_returning_it(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "synthetic-search-key")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "results": [
                    {
                        "title": "Offline result",
                        "url": "https://example.invalid/source",
                        "content": "Search excerpt",
                    }
                ]
            },
        )

    monkeypatch.setattr(tools.httpx, "post", post)
    result = call("web_search", {"query": "fixture"})
    assert "Offline result" in result and "https://example.invalid/source" in result
    assert "synthetic-search-key" not in result
    assert len(calls) == 1 and calls[0][0] == "https://api.tavily.com/search"
    assert calls[0][1]["json"]["api_key"] == "synthetic-search-key"


def test_reading_checks_robots_first_and_supports_continuation_without_losing_text(monkeypatch):
    actions = []
    content = "Readable article content with a final condition."

    async def robots(url, user_agent):
        actions.append(("robots", url, user_agent))

    async def fetch(url, user_agent, *, force_raw):
        actions.append(("fetch", url, user_agent, force_raw))
        return content, ""

    monkeypatch.setattr(
        web_research,
        "_reader_backend",
        lambda: SimpleNamespace(
            DEFAULT_USER_AGENT_AUTONOMOUS="Offline fixture agent",
            check_may_autonomously_fetch_url=robots,
            fetch_url=fetch,
        ),
    )
    first = call("read_webpage", {"url": "https://example.invalid/article", "max_length": 20})
    assert content[:20] in first and "start_index=20" in first
    assert content[20:] not in first
    rest = call(
        "read_webpage", {"url": "https://example.invalid/article", "start_index": 20, "raw": True}
    )
    assert content[20:] in rest and "Content truncated" not in rest
    assert actions == [
        ("robots", "https://example.invalid/article", "Offline fixture agent"),
        ("fetch", "https://example.invalid/article", "Offline fixture agent", False),
        ("robots", "https://example.invalid/article", "Offline fixture agent"),
        ("fetch", "https://example.invalid/article", "Offline fixture agent", True),
    ]
    end = call(
        "read_webpage", {"url": "https://example.invalid/article", "start_index": len(content)}
    )
    assert "No more content" in end and "Content truncated" not in end


def test_robots_denial_stops_before_fetch_and_has_no_fallback(monkeypatch):
    async def denied(*args):
        raise McpError(
            ErrorData(code=INTERNAL_ERROR, message="robots.txt denies autonomous access")
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("A denied URL must never be fetched")

    monkeypatch.setattr(
        web_research,
        "_reader_backend",
        lambda: SimpleNamespace(
            DEFAULT_USER_AGENT_AUTONOMOUS="Offline fixture agent",
            check_may_autonomously_fetch_url=denied,
            fetch_url=forbidden,
        ),
    )
    with pytest.raises(McpError, match="robots.txt denies"):
        call("read_webpage", {"url": "https://example.invalid/private"})


def test_actual_stdio_discovery_and_validation_need_no_app_database_or_reader_runtime(tmp_path):
    script = Path(web_research.__file__).resolve()
    spec = {"command": sys.executable, "args": [str(script)]}
    try:
        names = {item["name"] for item in mcp_client.list_tools(spec)}
        assert names == {"web_search", "read_webpage"}
        rejected = mcp_client.call_tool(spec, "read_webpage", {"url": "file:///never-read.txt"})
        assert "MCP 工具报告错误" in rejected
        assert {item["name"] for item in mcp_client.list_tools(spec)} == names
    finally:
        mcp_client.invalidate(spec)
