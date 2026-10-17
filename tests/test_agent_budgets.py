"""Agent budgets: tool rounds end early on time or gathered-result size, then the model
answers without tools. Offline doubles only; the clock is simulated."""

import copy
import json
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.llm as llm
import app.service as service
from app.config import Settings, settings
from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes

SPEC = llm.LLMSpec(
    label="Offline",
    base_url="https://offline.invalid/v1",
    llm_model="synthetic",
    api_key="synthetic-key-never-sent",
)
TOOL = {
    "type": "function",
    "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}},
}
CLOSING = "工具调用已达本轮上限"


def tool_round(*names):
    return {
        "content": None,
        "tool_calls": [
            {"id": f"call_{index}", "name": name, "args": {}} for index, name in enumerate(names)
        ],
    }


def scripted(monkeypatch, *responses):
    requests = []

    def chat_once(spec, conversation, tools):
        requests.append((copy.deepcopy(conversation), [t["function"]["name"] for t in tools]))
        return responses[len(requests) - 1]

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    return requests


def run(execute, **kwargs):
    diagnostics = {}
    events = list(
        llm.run_agent(
            SPEC,
            "system",
            [{"role": "user", "content": "q"}],
            [TOOL],
            execute,
            diagnostics=diagnostics,
            **kwargs,
        )
    )
    return events, diagnostics


def test_token_budget_ends_tool_rounds_with_the_final_no_tool_round(monkeypatch):
    requests = scripted(
        monkeypatch, tool_round("lookup"), {"content": "据已有资料回答。", "tool_calls": []}
    )
    executed = []
    events, diagnostics = run(
        lambda name, args: executed.append(name) or "result " * 400,
        max_rounds=4,
        tool_token_budget=100,
    )
    assert executed == ["lookup"] and len(requests) == 2
    assert requests[0][1] == ["lookup"] and requests[1][1] == []
    assert CLOSING in requests[1][0][0]["content"]
    assert diagnostics["tool_budget_stop"] == "tokens" and diagnostics["tool_rounds"] == 1
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "据已有资料回答。"


def test_time_limit_finishes_the_running_round_but_issues_no_further_one(monkeypatch):
    clock = {"now": 1000.0}
    # Only the agent loop's view of time is simulated, not the process clock.
    monkeypatch.setattr(llm, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    requests = scripted(
        monkeypatch,
        tool_round("lookup", "lookup"),
        {"content": "超时前的资料已足够。", "tool_calls": []},
    )
    executed = []

    def slow(name, args):
        executed.append(name)
        clock["now"] += 150  # The first call alone passes the 180-second limit.
        return "small"

    _, diagnostics = run(slow, max_rounds=4, time_limit_seconds=180, tool_token_budget=10**6)
    assert executed == ["lookup", "lookup"], "a call in the same round is never cut off"
    assert len(requests) == 2 and requests[1][1] == []
    assert diagnostics["tool_budget_stop"] == "time"


def test_rounds_within_budget_or_ending_at_the_round_cap_record_no_budget_stop(monkeypatch):
    scripted(
        monkeypatch,
        tool_round("lookup"),
        tool_round("lookup"),
        {"content": "完成", "tool_calls": []},
    )
    _, diagnostics = run(
        lambda name, args: "small", max_rounds=4, time_limit_seconds=180, tool_token_budget=10**6
    )
    assert "tool_budget_stop" not in diagnostics and diagnostics["tool_rounds"] == 2
    # The round cap would have ended the loop anyway: that is not a budget stop.
    requests = scripted(monkeypatch, tool_round("lookup"), {"content": "完成", "tool_calls": []})
    _, diagnostics = run(lambda name, args: "x" * 5000, max_rounds=1, tool_token_budget=1)
    assert "tool_budget_stop" not in diagnostics and requests[1][1] == []


def test_budget_settings_have_documented_defaults_and_read_the_environment(monkeypatch):
    for name in ("AGENT_TIME_LIMIT_SECONDS", "AGENT_TOOL_TOKEN_BUDGET"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.agent_time_limit_seconds == 180
    assert defaults.agent_tool_token_budget == 60000
    monkeypatch.setenv("AGENT_TIME_LIMIT_SECONDS", "45")
    monkeypatch.setenv("AGENT_TOOL_TOKEN_BUDGET", "8000")
    configured = Settings(_env_file=None)
    assert (configured.agent_time_limit_seconds, configured.agent_tool_token_budget) == (45, 8000)
    monkeypatch.setenv("AGENT_TOOL_TOKEN_BUDGET", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'budgets.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args: ([copy.deepcopy(TOOL)], {}))
    monkeypatch.setattr(nodes, "make_executor", lambda _router: lambda name, args: "z " * 3000)
    monkeypatch.setattr(nodes, "run_agent", llm.run_agent)
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="Topic"))
        session.add(Node(id=1, tree_id=1, title="Root"))
        session.add(
            ModelConfig(
                id=1,
                label="Offline",
                base_url="https://offline.invalid/v1",
                llm_model="synthetic",
                api_key="synthetic-key-never-sent",
                is_default=True,
            )
        )
        session.commit()
    application = FastAPI()
    application.include_router(nodes.router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[nodes.get_session] = session_override
    yield TestClient(application), engine
    nodes._GENERATIONS.clear()
    engine.dispose()


def test_answer_diagnostics_record_the_budget_that_stopped_tool_rounds(client, monkeypatch):
    api, engine = client
    monkeypatch.setattr(settings, "agent_tool_token_budget", 500)
    requests = scripted(
        monkeypatch,
        tool_round("lookup"),
        {"content": "根据已取得的资料回答。", "tool_calls": []},
    )
    response = api.post("/nodes/1/ask", json={"question": "查资料", "tools": ["lookup"]})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line[:6] == "data: "]
    assert events[-1].get("done") is True, events[-1]
    assert len(requests) == 2 and requests[1][1] == []
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    assert report["tool_budget_stop"] == "tokens" and report["tool_rounds"] == 1
    with Session(engine) as session:
        answer = session.exec(select(Message).where(Message.role == "assistant")).one()
        assert answer.diagnostics["tool_budget_stop"] == "tokens"


def test_answers_within_budget_report_no_budget_stop(client, monkeypatch):
    api, _ = client
    scripted(monkeypatch, tool_round("lookup"), {"content": "完成。", "tool_calls": []})
    response = api.post("/nodes/1/ask", json={"question": "查资料", "tools": ["lookup"]})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line[:6] == "data: "]
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    assert report["tool_budget_stop"] is None and report["tool_rounds"] == 1
