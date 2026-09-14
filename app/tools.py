"""Tool registry exposing function calling to models (web search, fetch, etc.).

Each tool pairs an OpenAI-compatible definition with an execution function.
The same structure maps to MCP tools: register the schemas and execution proxies
exposed by an MCP server (see the README extension notes).

- fetch: retrieve a real URL with httpx; no API key required, testable with a local server.
- web_search: use Tavily when TAVILY_API_KEY is set, otherwise use key-free DDGS search.
"""

import os

import httpx

# OpenAI-compatible tool definitions sent to the model to expose available tools.
TOOL_DEFS: dict[str, dict] = {
    "fetch": {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": "抓取一个网页 URL 的正文内容，用于阅读具体页面。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的完整 URL"}},
                "required": ["url"],
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索，返回若干相关结果的标题与摘要。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
}


def tool_defs_for(names: list[str]) -> list[dict]:
    return [TOOL_DEFS[n] for n in names if n in TOOL_DEFS]


def mcp_tool_def(oai_name: str, tool: dict) -> dict:
    """Convert an MCP tool (name/description/schema) into an OpenAI-compatible definition."""
    return {
        "type": "function",
        "function": {
            "name": oai_name,
            "description": tool.get("description", ""),
            "parameters": tool.get("schema") or {"type": "object", "properties": {}},
        },
    }


def _fetch(url: str) -> str:
    if not url:
        return "fetch 失败：没有提供 url"
    try:
        resp = httpx.get(
            url, timeout=10, follow_redirects=True, headers={"User-Agent": "branch-learning/0.2"}
        )
        text = resp.text
        if len(text) > 2000:
            text = text[:2000] + "…（已截断）"
        return f"[fetch {url} → HTTP {resp.status_code}]\n{text}"
    except Exception as e:  # noqa: BLE001
        return f"fetch 失败：{type(e).__name__}: {e}"


def _web_search(query: str) -> str:
    if not query or not query.strip():
        return "搜索失败：请输入关键词。"
    key = os.environ.get("TAVILY_API_KEY")
    if key:
        try:
            resp = httpx.post(
                "https://api.tavily.com/search",
                json={"api_key": key, "query": query, "max_results": 3},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("results", [])
            lines = [
                f"- {h.get('title')}：{h.get('content', '')[:160]}（{h.get('url')}）" for h in hits
            ]
            return f"[web_search {query}]\n" + "\n".join(lines) if lines else "无结果"
        except Exception as e:  # noqa: BLE001
            return f"web_search 失败：{type(e).__name__}: {e}"
    try:
        from ddgs import DDGS

        hits = list(DDGS(timeout=15).text(query.strip(), max_results=5))
        rows = [
            f"[{i + 1}] {h.get('title', '')}\n{h.get('href', '')}\n{h.get('body', '')[:700]}"
            for i, h in enumerate(hits)
            if str(h.get("href", "")).startswith(("https://", "http://"))
        ]
        return (
            "[联网搜索]\n" + "\n\n".join(rows) if rows else "没有搜到结果。可以换一组关键词再试。"
        )
    except Exception as exc:
        return f"联网搜索暂时不可用（{type(exc).__name__}），请稍后重试。未生成模拟搜索结果。"


def execute_tool(name: str, args: dict) -> str:
    if name == "fetch":
        return _fetch(args.get("url", ""))
    if name == "web_search":
        return _web_search(args.get("query", ""))
    return f"未知工具：{name}"
