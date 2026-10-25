"""Tools that may change things wait for the learner's approval before they run.

Offline doubles only: no MCP process is started and no model is called.
"""

import asyncio
import copy
import json
import os
import threading
import time
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.types import Tool, ToolAnnotations
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

import app.llm as llm
import app.service as service
from app import db, mcp_client
from app.models import KnowledgeTree, McpServer, Message, ModelConfig, Node
from app.routers import mcp_router, nodes

WRITE = "mcp_1_write_file"
READ = "mcp_1_read_file"
ARGS = {"path": "notes.md", "content": "Synthetic note"}


def test_listing_keeps_whether_a_tool_is_read_only():
    class Listing:
        async def list_tools(self, *, params=None):
            return SimpleNamespace(
                tools=[
                    Tool(
                        name="read_file",
                        inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=True),
                    ),
                    Tool(
                        name="write_file",
                        inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
                    ),
                    Tool(name="unmarked", inputSchema={"type": "object"}),
                ],
                nextCursor=None,
            )

    listed = asyncio.run(mcp_client._SessionWorker._list(Listing()))
    assert {tool["name"]: tool["read_only"] for tool in listed} == {
        "read_file": True,
        "write_file": False,
        "unmarked": False,
    }


@pytest.mark.parametrize(
    "policy,asks",
    [
        ("writes", {"read_file": False, "write_file": True, "unmarked": True}),
        ("always", {"read_file": True, "write_file": True, "unmarked": True}),
        ("never", {"read_file": False, "write_file": False, "unmarked": False}),
    ],
)
def test_the_server_policy_decides_which_calls_ask_first(monkeypatch, policy, asks):
    server = SimpleNamespace(
        id=1,
        label="Files",
        enabled=True,
        command="npx",
        args=[],
        transport="stdio",
        url=None,
        headers=None,
        allow_private=False,
        confirm_tools=policy,
    )
    session = SimpleNamespace(get=lambda model, sid: server if sid == 1 else None)
    monkeypatch.setattr(
        mcp_client,
        "list_tools",
        lambda spec, **kwargs: [
            {"name": "read_file", "description": "", "schema": {}, "read_only": True},
            {"name": "write_file", "description": "", "schema": {}, "read_only": False},
            {"name": "unmarked", "description": "", "schema": {}},
        ],
    )
    _, routes = service.assemble_tools(session, ["mcp_server_1", "web_search"])
    assert {
        name[len("mcp_1_") :]: service.needs_confirmation(routes, name)
        for name in routes
        if name.startswith("mcp_")
    } == asks
    assert service.needs_confirmation(routes, "web_search") is False, "built-in tools only read"
    assert service.needs_confirmation(routes, "missing") is False


@pytest.fixture
def ask_api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'confirmation.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    state = {"calls": [], "returned": [], "agent": []}
    routes = {
        WRITE: ("mcp", {"transport": "stdio"}, "write_file", True),
        READ: ("mcp", {"transport": "stdio"}, "read_file", False),
    }
    definitions = [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in routes
    ]
    monkeypatch.setattr(nodes, "assemble_tools", lambda session, requested: (definitions, routes))
    monkeypatch.setattr(
        mcp_client,
        "call_tool",
        lambda spec, name, args, full=None: state["calls"].append((name, args)) or "written",
    )
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    state["tool"] = WRITE

    def agent(spec, system, messages, tools, execute, **kwargs):
        state["agent"].append(kwargs)
        yield {"type": "tool_start", "name": state["tool"], "args": ARGS}
        result = execute(state["tool"], copy.deepcopy(ARGS))
        state["returned"].append(result)
        yield {"type": "tool_end", "name": state["tool"], "result": result}
        yield {"type": "delta", "text": "完成。"}

    monkeypatch.setattr(nodes, "run_agent", agent)
    nodes._GENERATIONS.clear()
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="Topic"))
        session.add(Node(id=1, tree_id=1, title="Root", status="idle"))
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
    with TestClient(application) as client:
        yield client, engine, state
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    nodes._GENERATIONS.clear()
    engine.dispose()


def _ask(client, box, request_id="confirm-request"):
    response = client.post(
        "/nodes/1/ask",
        json={"question": "写入笔记", "tools": ["mcp_server_1"], "request_id": request_id},
    )
    box["events"] = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def _start(client, request_id="confirm-request"):
    box: dict = {}
    worker = threading.Thread(target=_ask, args=(client, box, request_id), daemon=True)
    worker.start()
    return worker, box


def _pending(timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        generation = nodes._GENERATIONS.get(1)
        if generation is not None and generation.confirmation is not None:
            return generation
        time.sleep(0.01)
    raise AssertionError("no confirmation was requested")


def _decide(client, allow, request_id="confirm-request", index=1):
    return client.post(
        "/nodes/1/tool-confirm", json={"request_id": request_id, "index": index, "allow": allow}
    )


def _steps(engine):
    with Session(engine) as session:
        answer = session.exec(
            select(Message).where(Message.node_id == 1, Message.role == "assistant")
        ).one()
        return answer.steps


def test_a_write_waits_for_approval_and_runs_once_allowed(ask_api):
    client, engine, state = ask_api
    worker, box = _start(client)
    generation = _pending()
    assert generation.confirmation == {"index": 1, "name": WRITE, "args": ARGS}
    assert state["calls"] == [], "nothing runs before the decision"
    time.sleep(0.12)
    assert _decide(client, True).status_code == 200
    worker.join(5)
    events = box["events"]
    assert events[-1]["status"] == "complete", events
    confirm = [event["tool_confirm"] for event in events if "tool_confirm" in event]
    assert confirm == [{"index": 1, "name": WRITE, "args": ARGS}]
    done = [event["tool_confirm_done"] for event in events if "tool_confirm_done" in event]
    assert done == [{"index": 1, "outcome": "allowed"}]
    assert state["calls"] == [("write_file", ARGS)] and state["returned"] == ["written"]
    (end,) = [event["tool_end"] for event in events if "tool_end" in event]
    assert end["confirmation"] == "allowed"
    assert _steps(engine)[0]["confirmation"] == "allowed"
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    assert report["timings_ms"]["approval"] >= 100, "the wait is reported on its own"
    assert state["agent"][0]["waited"]() >= 0.1, "and kept out of the agent's time budget"


@pytest.mark.parametrize("outcome", ["declined", "expired"])
def test_a_write_that_is_not_allowed_never_runs_and_the_model_is_told(
    ask_api, monkeypatch, outcome
):
    client, engine, state = ask_api
    if outcome == "expired":
        monkeypatch.setattr(nodes.settings, "tool_confirm_timeout_seconds", 0.2)
    worker, box = _start(client)
    _pending()
    if outcome == "declined":
        assert _decide(client, False).status_code == 200
    worker.join(5)
    events = box["events"]
    assert events[-1]["status"] == "complete", events
    assert state["calls"] == []
    (told,) = state["returned"]
    assert ("没有允许" if outcome == "declined" else "超时") in told and "没有执行" in told
    done = [event["tool_confirm_done"] for event in events if "tool_confirm_done" in event]
    assert done == [{"index": 1, "outcome": outcome}]
    assert _steps(engine)[0]["confirmation"] == outcome


def test_stop_while_waiting_ends_the_answer_without_running_the_tool(ask_api):
    client, _, state = ask_api
    worker, box = _start(client)
    _pending()
    started = time.monotonic()
    response = client.post("/nodes/1/stop", json={"request_id": "confirm-request"})
    assert response.status_code == 200
    worker.join(5)
    assert time.monotonic() - started < 2, "Stop does not wait for the confirmation timeout"
    assert box["events"][-1]["status"] == "interrupted"
    assert state["calls"] == [] and state["returned"] == []


def test_read_only_tools_run_without_asking(ask_api):
    client, _, state = ask_api
    state["tool"] = READ
    box: dict = {}
    _ask(client, box)
    assert box["events"][-1]["status"] == "complete"
    assert not any("tool_confirm" in event for event in box["events"])
    assert state["calls"] == [("read_file", ARGS)]


def test_the_confirmation_endpoint_only_answers_the_waiting_call(ask_api):
    client, _, state = ask_api
    assert _decide(client, True).status_code == 409, "no answer is running"
    worker, box = _start(client)
    _pending()
    assert _decide(client, True, request_id="another-request").status_code == 409
    assert _decide(client, True, index=2).status_code == 409
    assert state["calls"] == []
    assert _decide(client, True).status_code == 200
    worker.join(5)
    assert box["events"][-1]["status"] == "complete"
    assert _decide(client, True).status_code == 409, "a decided call is not waiting any more"


def test_time_spent_waiting_for_approval_is_not_charged_to_the_agent(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(llm, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    responses = [
        {"content": None, "tool_calls": [{"id": "call_1", "name": WRITE, "args": {}}]},
        {"content": None, "tool_calls": [{"id": "call_2", "name": WRITE, "args": {}}]},
        {"content": "完成", "tool_calls": []},
    ]
    requests = []

    def chat_once(spec, conversation, tools):
        requests.append([tool["function"]["name"] for tool in tools])
        return responses[len(requests) - 1]

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    waited = {"seconds": 0.0}

    def execute(name, args):
        clock["now"] += 150  # The learner took 150 s to approve; the tool itself was instant.
        waited["seconds"] += 150
        return "written"

    spec = llm.LLMSpec("Offline", "https://offline.invalid/v1", "synthetic", "synthetic-key")
    tool = {"type": "function", "function": {"name": WRITE, "parameters": {"type": "object"}}}
    diagnostics: dict = {}
    list(
        llm.run_agent(
            spec,
            "system",
            [{"role": "user", "content": "q"}],
            [tool],
            execute,
            max_rounds=4,
            diagnostics=diagnostics,
            time_limit_seconds=180,
            waited=lambda: waited["seconds"],
        )
    )
    assert "tool_budget_stop" not in diagnostics
    assert requests[1] == [WRITE], "the second round still offers tools"


@pytest.fixture
def mcp_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-policy.db'}")
    with engine.begin() as connection:
        # A server saved before the policy existed.
        connection.execute(
            text(
                "CREATE TABLE mcpserver (id INTEGER PRIMARY KEY, label VARCHAR NOT NULL, "
                "command VARCHAR NOT NULL, args JSON, enabled BOOLEAN NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO mcpserver VALUES (5, 'Legacy files', 'npx', '[]', 1, "
                "'2026-01-01 00:00:00')"
            )
        )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    application = FastAPI()
    application.include_router(mcp_router.router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[db.get_session] = session_override
    yield TestClient(application), engine
    engine.dispose()


def test_servers_store_their_confirmation_policy(mcp_api):
    client, engine = mcp_api
    with Session(engine) as session:
        assert session.get(McpServer, 5).confirm_tools == "writes", "existing servers ask"
    assert client.get("/mcp").json()[0]["confirm_tools"] == "writes"
    body = {"label": "Notes", "command": "npx", "args": [], "confirm_tools": "always"}
    created = client.post("/mcp", json=body)
    assert created.status_code == 200 and created.json()["confirm_tools"] == "always"
    server_id = created.json()["id"]
    assert (
        client.post("/mcp", json={"label": "Default", "command": "npx"}).json()["confirm_tools"]
        == "writes"
    )
    updated = client.put(f"/mcp/{server_id}", json={**body, "confirm_tools": "never"})
    assert updated.status_code == 200 and updated.json()["confirm_tools"] == "never"
    assert (
        client.put(f"/mcp/{server_id}", json={**body, "confirm_tools": "sometimes"}).status_code
        == 422
    )


def test_a_stop_that_comes_before_the_wait_is_not_lost():
    generation = nodes.Generation("stopped-early")
    generation.stop.set()  # Stop pressed between the executor's check and the wait.
    started = time.monotonic()
    assert generation.await_confirmation(1, WRITE, ARGS, timeout=5) == "stopped"
    assert time.monotonic() - started < 1, "the wait does not run to its timeout"
    assert generation.confirmation is None
    assert generation.log[-1] == {"tool_confirm_done": {"index": 1, "outcome": "stopped"}}
