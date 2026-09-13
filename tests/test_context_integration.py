"""Budget/compaction route integration with temporary SQLite and offline model doubles."""

import copy
import json
import os
import threading

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.service as service
from app.context_budget import ContextPolicy, estimate_request_tokens
from app.context_sources import SOURCE_TOOL_NAME
from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'context-integration.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    calls = {name: [] for name in ("stream", "agent", "memory", "discover", "titles")}

    def stream(spec, system, messages, **kwargs):
        calls["stream"].append((spec, system, copy.deepcopy(messages)))
        yield "text", "合成回答。"

    def agent(spec, system, messages, tools, execute, **kwargs):
        calls["agent"].append((spec, system, copy.deepcopy(messages), copy.deepcopy(tools)))
        yield {"type": "delta", "text": "合成工具回答。"}

    def memory(*args, **kwargs):
        calls["memory"].append(kwargs)
        return ""

    def discover(session, requested):
        calls["discover"].append(requested)
        return [], {}

    monkeypatch.setattr(nodes, "stream_chat", stream)
    monkeypatch.setattr(nodes, "run_agent", agent)
    monkeypatch.setattr(nodes, "fetch_memory_note", memory)
    monkeypatch.setattr(nodes, "assemble_tools", discover)
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        nodes, "_queue_branch_title", lambda *args, **kwargs: calls["titles"].append(True)
    )
    nodes._GENERATIONS.clear()
    nodes._TITLE_JOBS.clear()
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="当前树"), KnowledgeTree(id=2, title="其他树")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="祖先", status="complete"),
                Node(id=2, tree_id=1, parent_id=1, title="当前分支", status="idle"),
                Node(id=3, tree_id=1, parent_id=1, title="兄弟分支", status="complete"),
                Node(id=4, tree_id=2, title="另一棵树", status="complete"),
            ]
        )
        session.add_all(
            [
                Message(id=1, node_id=1, role="user", content="前面的条件是什么？"),
                Message(id=2, node_id=1, role="assistant", content="只有获得明确许可才执行。"),
                Message(id=3, node_id=3, role="assistant", content="SYNTHETIC_SIBLING_SECRET"),
                Message(id=4, node_id=4, role="assistant", content="SYNTHETIC_FOREIGN_SECRET"),
                ModelConfig(
                    id=1,
                    label="Offline test",
                    base_url="https://offline.invalid/v1",
                    llm_model="synthetic",
                    api_key="synthetic-key-never-sent",
                    is_default=True,
                    context_window=8192,
                    max_tokens=1024,
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(nodes.router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[nodes.get_session] = session_override
    with TestClient(app) as client:
        yield client, engine, calls
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    nodes._GENERATIONS.clear()
    nodes._TITLE_JOBS.clear()
    engine.dispose()


def ask(client, question="继续解释条件。", **kwargs):
    response = client.post("/nodes/2/ask", json={"question": question, **kwargs})
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def make_history_long(engine):
    text = "历史背景。" * 3000 + "只有收到明确授权才执行，末尾条件不能猜测。"
    with Session(engine) as session:
        message = session.get(Message, 2)
        message.content = text
        session.add(message)
        session.commit()
    return text


def test_short_history_without_selected_tools_keeps_streaming_path(api):
    client, engine, calls = api
    events = ask(client)
    assert events[-1]["done"]
    assert len(calls["stream"]) == 1 and not calls["agent"]
    spec, system, messages = calls["stream"][0]
    assert messages[-1]["content"] == "继续解释条件。"
    assert any(message.get("content") == "只有获得明确许可才执行。" for message in messages)
    assert estimate_request_tokens(system, messages) <= ContextPolicy.from_spec(spec).input_budget
    assert calls["discover"] == [[]]


def test_long_history_uses_reader_and_rejects_sibling_and_foreign_sources(api, monkeypatch):
    client, engine, calls = api
    source = make_history_long(engine)
    readbacks = []

    def agent(spec, system, messages, tools, execute, **kwargs):
        assert SOURCE_TOOL_NAME in [item["function"]["name"] for item in tools]
        assert "结构化" in system and "read_learning_source" in system
        assert (
            estimate_request_tokens(system, messages, tools, spec.protocol)
            <= ContextPolicy.from_spec(spec).input_budget
        )
        readbacks.append(
            json.loads(
                execute(
                    SOURCE_TOOL_NAME,
                    {
                        "node_id": 1,
                        "section": "message",
                        "message_id": 2,
                        "start": len(source) - 30,
                        "max_chars": 30,
                    },
                )
            )
        )
        readbacks.append(
            json.loads(
                execute(SOURCE_TOOL_NAME, {"node_id": 3, "section": "message", "message_id": 3})
            )
        )
        readbacks.append(
            json.loads(
                execute(SOURCE_TOOL_NAME, {"node_id": 4, "section": "message", "message_id": 4})
            )
        )
        yield {"type": "delta", "text": "已按原文检查条件。"}

    monkeypatch.setattr(nodes, "run_agent", agent)
    events = ask(client)
    assert events[-1]["done"], events
    assert not calls["stream"]
    assert readbacks[0]["ok"] and readbacks[0]["text"] == source[-30:]
    assert all(not item["ok"] and item["error"] == "source_unavailable" for item in readbacks[1:])
    with Session(engine) as session:
        assert session.get(Message, 2).content == source


@pytest.mark.parametrize("overflow", ["question", "quote"])
def test_mandatory_overflow_saves_question_but_starts_no_model_or_mcp(api, overflow):
    client, engine, calls = api
    secret = "SYNTHETIC_PRIVATE_OVERFLOW"
    question = secret * 500 if overflow == "question" else "请解释本次选文。"
    if overflow == "quote":
        with Session(engine) as session:
            current = session.get(Node, 2)
            current.kind = "branch"
            current.seed_text = secret * 500
            session.add(current)
            session.commit()
    events = ask(client, question, tools=["mcp_server_88"])
    assert events[-1]["status"] == "error"
    assert "上下文预算" in events[-1]["error"]
    assert secret not in events[-1]["error"]
    assert not any(calls[name] for name in calls), (
        "even title or memory models must wait for valid mandatory input"
    )
    with Session(engine) as session:
        assert session.get(Node, 2).status == "error"
        assert session.get(Node, 2).title_state != "pending"
        saved = session.exec(
            select(Message).where(Message.node_id == 2, Message.role == "user")
        ).all()
        assert len(saved) == 1 and saved[0].content == question


def test_oversized_discovered_schema_fails_before_answer_or_title_model(api, monkeypatch):
    client, engine, calls = api
    huge = {
        "type": "function",
        "function": {
            "name": "oversized_tool",
            "description": "schema" * 3000,
            "parameters": {"type": "object", "properties": {}},
        },
    }

    def discover(session, requested):
        calls["discover"].append(requested)
        return [huge], {}

    monkeypatch.setattr(nodes, "assemble_tools", discover)
    with Session(engine) as session:
        current = session.get(Node, 2)
        current.seed_text = "短的当前选文"
        current.kind = "branch"
        session.add(current)
        session.commit()
    events = ask(client, tools=["mcp_server_88"])
    assert events[-1]["status"] == "error" and "上下文预算" in events[-1]["error"]
    assert calls["discover"] == [["mcp_server_88"]]
    assert not calls["stream"] and not calls["agent"] and not calls["titles"]


def test_large_recalled_memory_is_budgeted_with_history_and_source_tool_schema(api, monkeypatch):
    client, engine, calls = api
    memory = "以下是历史记忆：\n" + "\n".join(
        f"- [记忆 {index}；来源节点 1] 第 {index} 条合成条件只能在明确授权后使用。"
        for index in range(200)
    )
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: memory)
    events = ask(client)
    assert events[-1]["done"], events
    assert len(calls["agent"]) == 1 and not calls["stream"]
    spec, system, messages, tools = calls["agent"][0]
    assert "可能省略条目" in system
    assert messages[-1]["content"] == "继续解释条件。"
    assert (
        estimate_request_tokens(system, messages, tools, spec.protocol)
        <= ContextPolicy.from_spec(spec).input_budget
    )


def test_retry_reader_cannot_fetch_the_failed_attempt_or_duplicate_original_question(
    api, monkeypatch
):
    client, engine, calls = api
    make_history_long(engine)
    with Session(engine) as session:
        current = session.get(Node, 2)
        current.status = "error"
        session.add(current)
        session.add_all(
            [
                Message(id=5, node_id=2, role="user", content="原问题保持不变。"),
                Message(
                    id=6,
                    node_id=2,
                    role="assistant",
                    status="error",
                    content="SYNTHETIC_FAILED_PARTIAL",
                ),
            ]
        )
        session.commit()
    reads = []

    def agent(spec, system, messages, tools, execute, **kwargs):
        assert messages[-1]["content"] == "原问题保持不变。"
        assert "SYNTHETIC_FAILED_PARTIAL" not in system + str(messages)
        reads.append(
            json.loads(
                execute(SOURCE_TOOL_NAME, {"node_id": 2, "section": "message", "message_id": 6})
            )
        )
        yield {"type": "delta", "text": "重试的完整回答。"}

    monkeypatch.setattr(nodes, "run_agent", agent)
    events = ask(client, "", mode="retry")
    assert events[-1]["done"], events
    assert reads[0]["error"] == "source_unavailable"
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(Message).where(Message.node_id == 2, Message.role == "user")
                ).all()
            )
            == 1
        )
        assert session.get(Message, 6).content == "SYNTHETIC_FAILED_PARTIAL"


def test_stop_during_slow_context_preflight_prevents_memory_discovery_and_models(api, monkeypatch):
    client, engine, calls = api
    entered, release, request_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_build = nodes.build_context

    def slow_build(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(nodes, "build_context", slow_build)

    def request():
        try:
            events = ask(client, request_id="slow-context-stop", tools=["mcp_server_88"])
            assert events[-1]["status"] == "interrupted"
        except BaseException as exc:
            errors.append(exc)
        finally:
            request_done.set()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    generation = None
    try:
        assert entered.wait(3)
        generation = nodes._GENERATIONS[2]
        stopped = client.post("/nodes/2/stop", json={"request_id": "slow-context-stop"})
        assert stopped.status_code == 200
        assert request_done.wait(2), "stop must complete while compaction is still paused"
    finally:
        release.set()
        worker.join(5)
        if generation is not None:
            assert generation.events.get(timeout=3) is None
    assert not worker.is_alive() and not errors
    assert not any(calls[name] for name in calls), (
        "cancelled preflight must not start downstream work"
    )


def test_compacted_history_can_complete_a_real_source_read_tool_round(api, monkeypatch):
    """Use the actual agent loop so the second request's budget is exercised."""
    import app.llm as llm

    client, engine, calls = api
    source = "".join(
        f"只有第{index}个条件得到明确确认，才允许执行对应的第{index}项操作。"
        for index in range(300)
    )
    with Session(engine) as session:
        message = session.get(Message, 2)
        message.content = source
        session.add(message)
        session.commit()
    requests = []

    def chat_once(spec, conversation, tools):
        requests.append(copy.deepcopy(conversation))
        system = "\n".join(
            message.get("content", "") for message in conversation if message["role"] == "system"
        )
        messages = [message for message in conversation if message["role"] != "system"]
        assert (
            estimate_request_tokens(system, messages, tools, spec.protocol)
            <= ContextPolicy.from_spec(spec).input_budget
        )
        if len(requests) == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "source_read",
                        "name": SOURCE_TOOL_NAME,
                        "args": {
                            "node_id": 1,
                            "section": "message",
                            "message_id": 2,
                            "max_chars": 1200,
                        },
                    }
                ],
            }
        assert conversation[-1]["role"] == "tool"
        assert conversation[-1]["tool_call_id"] == "source_read"
        assert "第0个条件" in conversation[-1]["content"], (
            "the model needs actual reread evidence, not only an omission notice"
        )
        return {"content": "已根据读回的第0个条件回答。", "tool_calls": []}

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    monkeypatch.setattr(nodes, "run_agent", llm.run_agent)
    events = ask(client, "请读取原文中遗漏的具体条件。")
    assert events[-1].get("done") is True, events[-1]
    assert len(requests) == 2
    assert any("tool_end" in event for event in events)
    with Session(engine) as session:
        assert session.get(Message, 2).content == source
