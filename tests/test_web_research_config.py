"""Search credentials belong only to LearningTree's combined network preset."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest

from app import mcp_client, service

LAUNCHER = Path(__file__).resolve().parents[1] / "integrations/mcp/launch.py"


@pytest.mark.parametrize(
    "args, receives_key",
    [
        ([str(LAUNCHER), "web-research"], True),
        ([str(LAUNCHER), "fetch"], False),
        ([str(LAUNCHER), "memory"], False),
        (["/custom/integrations/mcp/launch.py", "web-research"], False),
        ([], False),
    ],
)
def test_only_combined_preset_receives_search_key(monkeypatch, args, receives_key):
    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-model-key")
    spec = mcp_client.server_spec(sys.executable, args)
    params = mcp_client._params(spec)
    assert params.env.get("TAVILY_API_KEY") == ("test-search-key" if receives_key else None)
    assert "OPENAI_API_KEY" not in params.env
    assert "test-search-key" not in mcp_client._key(spec)


def test_unconfigured_search_uses_key_free_runtime(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    spec = mcp_client.server_spec(sys.executable, [str(LAUNCHER), "web-research"])
    assert "env" not in spec


def test_custom_command_cannot_receive_search_key_with_managed_arguments(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    spec = mcp_client.server_spec("/custom/program", [str(LAUNCHER), "web-research"])
    assert "env" not in spec


def test_one_selection_exposes_both_capabilities_and_one_execution_identity(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    server = SimpleNamespace(
        id=12,
        command=sys.executable,
        args=[str(LAUNCHER), "web-research"],
        label="联网搜索",
        enabled=True,
    )
    session = SimpleNamespace(get=lambda model, sid: server if sid == server.id else None)
    listed = []

    def list_tools(spec):
        listed.append(spec)
        return [{"name": name} for name in ("web_search", "read_webpage")]

    monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    definitions, routes = service.assemble_tools(session, ["mcp_server_12", "mcp_server_12"])
    assert [item["function"]["name"] for item in definitions] == [
        "mcp_12_web_search",
        "mcp_12_read_webpage",
    ]
    expected = mcp_client.server_spec(server.command, server.args)
    assert listed == [expected]
    calls = []
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: calls.append(args) or "result")
    execute = service.make_executor(routes)
    assert execute("mcp_12_web_search", {"query": "test"}) == "result"
    assert execute("mcp_12_read_webpage", {"url": "https://example.com"}) == "result"
    assert all(call[0] == expected for call in calls)
    server.enabled = False
    assert service.assemble_tools(session, ["mcp_server_12"]) == ([], {})
