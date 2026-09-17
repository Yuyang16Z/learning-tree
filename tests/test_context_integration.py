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
from app import learning_summaries
from app.context_budget import ContextPolicy, estimate_request_tokens
from app.context_sources import SOURCE_TOOL_NAME
from app.models import ContextSummary, KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes, trees
from app.tool_catalog import SEARCH_TOOL_NAME, tool_definition_cost


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
    app.include_router(trees.router)

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


def _long_summary_history(engine):
    with Session(engine) as session:
        cfg = session.get(ModelConfig, 1)
        cfg.context_window = 20000
        session.add(cfg)
        message = session.get(Message, 2)
        message.content = "Historical background to a synthetic learning question. " * 100
        session.add(message)
        for index in range(3):
            session.add(Message(node_id=1, role="user", content=f"Earlier learning goal {index}?"))
            session.add(
                Message(
                    node_id=1,
                    role="assistant",
                    content=(
                        f"Background explanation {index}; only under the stated condition. " * 90
                    ),
                )
            )
        current = session.get(Node, 2)
        current.seed_text = "CURRENT_QUOTE_MUST_STAY_EXACT"
        current.learning_note = "CURRENT_NOTE_MUST_STAY_EXACT"
        session.add(current)
        session.commit()


def test_model_summary_is_only_resolved_once_after_tool_preflight_and_preserves_current(
    api, monkeypatch
):
    client, engine, calls = api
    _long_summary_history(engine)
    summaries = []

    def summarize(engine_, spec, sources, query, budget, **kwargs):
        assert calls["discover"], "Discovery and final schema budgeting must precede summaries"
        assert engine_ is engine and not kwargs["stop"].is_set()
        summaries.append((sources, query, budget))
        assert all(node.tree_id == 1 and node.id not in (3, 4) for node, _ in sources)
        return "MODEL_SUMMARY_FIXTURE [node=1,message=2]: Earlier explanation; uncertainty remains."

    monkeypatch.setattr(nodes, "summarize_history", summarize)
    events = ask(client, "CURRENT_QUESTION_MUST_STAY_EXACT")
    assert events[-1]["status"] == "complete", events
    assert len(summaries) == 1
    assert [event["context_status"] for event in events if "context_status" in event] == [
        "summarizing",
        "ready",
    ]
    _, system, messages, tools = calls["agent"][0]
    assert "MODEL_SUMMARY_FIXTURE" in system
    assert "CURRENT_QUOTE_MUST_STAY_EXACT" in system
    assert "CURRENT_NOTE_MUST_STAY_EXACT" in system
    assert messages[-1]["content"] == "CURRENT_QUESTION_MUST_STAY_EXACT"
    assert SOURCE_TOOL_NAME in [tool["function"]["name"] for tool in tools]
    assert "SYNTHETIC_SIBLING_SECRET" not in system
    assert "SYNTHETIC_FOREIGN_SECRET" not in system
    with Session(engine) as session:
        assert session.get(Message, 2).content.startswith("Historical background")


@pytest.mark.parametrize("failure", ["error", "oversized", "empty"])
def test_optional_summary_failures_keep_the_answer_and_source_access(api, monkeypatch, failure):
    client, engine, calls = api
    _long_summary_history(engine)

    def summarize(*args, **kwargs):
        if failure == "error":
            raise RuntimeError("SYNTHETIC_PROVIDER_SECRET")
        return "EXCESSIVE_SUMMARY" * 10000 if failure == "oversized" else None

    monkeypatch.setattr(nodes, "summarize_history", summarize)
    events = ask(client)
    assert events[-1]["status"] == "complete", events
    _, system, messages, tools = calls["agent"][0]
    assert "EXCESSIVE_SUMMARY" not in system and "SYNTHETIC_PROVIDER_SECRET" not in system
    assert "原文" in system and "node=1" in system
    assert SOURCE_TOOL_NAME in [tool["function"]["name"] for tool in tools]


def test_short_question_does_not_request_a_summary(api, monkeypatch):
    client, _, _ = api
    monkeypatch.setattr(
        nodes, "summarize_history", lambda *a, **k: pytest.fail("Unnecessary summary call")
    )
    events = ask(client)
    assert events[-1]["status"] == "complete"
    assert not any("context_status" in event for event in events)


def test_legacy_revision_summarizes_and_reads_its_copied_history(api, monkeypatch):
    client, engine, _ = api
    _long_summary_history(engine)
    with Session(engine) as session:
        originals = session.exec(select(Message).where(Message.node_id == 1)).all()
        old_ids = {message.id for message in originals}
        for message in originals:
            message.node_id = 2
            session.add(message)
        last_question = Message(node_id=2, role="user", content="Original question to revise")
        session.add(last_question)
        session.commit()
        question_id = last_question.id
    payloads, readbacks = [], []

    def summarize(spec, sources, budget):
        payloads.append(sources)
        return "REVISED_HISTORY_SUMMARY: Earlier conditions remain relevant."

    def agent(spec, system, messages, tools, execute, **kwargs):
        assert "REVISED_HISTORY_SUMMARY" in system
        source_id = next(
            item["source_id"] for item in payloads[0] if "message=" in item["source_id"]
        )
        identifiers = dict(part.split("=") for part in source_id.split(","))
        assert int(identifiers["node"]) != 2
        assert int(identifiers["message"]) not in old_ids
        readbacks.append(
            json.loads(
                execute(
                    SOURCE_TOOL_NAME,
                    {
                        "node_id": int(identifiers["node"]),
                        "message_id": int(identifiers["message"]),
                        "section": "message",
                    },
                )
            )
        )
        yield {"type": "delta", "text": "Revised answer"}

    monkeypatch.setattr(learning_summaries, "summarize_learning_context", summarize)
    monkeypatch.setattr(nodes, "run_agent", agent)
    events = ask(client, "Revised question", mode="revise", question_message_id=question_id)
    assert events[-1]["status"] == "complete", events
    assert len(payloads) == 1 and readbacks[0]["ok"]
    with Session(engine) as session:
        assert session.get(Message, question_id).content == "Original question to revise"
        assert all(session.get(Message, identifier).node_id == 2 for identifier in old_ids)


@pytest.mark.parametrize(
    "operation,remaining",
    [
        ("note", {"sibling", "other-tree"}),
        ("node", {"sibling", "other-tree"}),
        ("tree", {"other-tree"}),
    ],
)
def test_source_mutation_routes_clear_only_dependent_summary_cache(api, operation, remaining):
    client, engine, _ = api
    with Session(engine) as session:
        for key, tree_id, node_id in (("current", 1, 2), ("sibling", 1, 3), ("other-tree", 2, 4)):
            session.add(
                ContextSummary(
                    key=key,
                    tree_id=tree_id,
                    source_node_ids=[node_id],
                    source_message_ids=[],
                    fingerprint="synthetic",
                    model_fingerprint="synthetic",
                    prompt_version="synthetic",
                    content="Disposable synthetic summary",
                )
            )
        session.commit()
    if operation == "note":
        response = client.patch("/nodes/2", json={"learning_note": "Corrected understanding"})
    elif operation == "node":
        response = client.delete("/nodes/2")
    else:
        response = client.delete("/trees/1")
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert {row.key for row in session.exec(select(ContextSummary)).all()} == remaining


def test_stop_during_summary_does_not_wait_for_provider_or_begin_answer(api, monkeypatch):
    client, engine, calls = api
    _long_summary_history(engine)
    entered, release, request_done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def summarize(*args, **kwargs):
        entered.set()
        release.wait(5)
        return "Late summary must not start an answer."

    monkeypatch.setattr(nodes, "summarize_history", summarize)

    def request():
        try:
            events = ask(client, request_id="stop-during-summary")
            assert events[-1]["status"] == "interrupted"
        except BaseException as exc:
            errors.append(exc)
        finally:
            request_done.set()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        generation = nodes._GENERATIONS[2]
        response = client.post("/nodes/2/stop", json={"request_id": "stop-during-summary"})
        assert response.status_code == 200
        assert request_done.wait(2), "Stopping must not wait for an optional summary"
    finally:
        release.set()
        worker.join(5)
    assert not errors and not worker.is_alive()
    while generation.events.get(timeout=3) is not None:
        pass
    assert not calls["stream"] and not calls["agent"]


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


def test_oversized_discovered_schema_is_deferred_and_reported_when_searched(api, monkeypatch):
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

    def agent(spec, system, messages, tools, execute, **kwargs):
        calls["agent"].append(True)
        assert SEARCH_TOOL_NAME in [item["function"]["name"] for item in tools]
        assert "oversized_tool" not in [item["function"]["name"] for item in tools]
        catalog = kwargs["tool_catalog"]
        result = json.loads(catalog.search({"query": "oversized_tool", "limit": 1}))
        assert result["tools"][0]["status"] == "tool_too_large"
        assert not result["loaded"] and "tool_too_large" in result
        assert calls["titles"], "valid catalog context permits the independent title job"
        assert (
            estimate_request_tokens(system, messages, tools, spec.protocol)
            <= ContextPolicy.from_spec(spec).input_budget
        )
        yield {"type": "delta", "text": "这个工具的完整定义仍然太大，可以换用较小的工具。"}

    monkeypatch.setattr(nodes, "run_agent", agent)
    with Session(engine) as session:
        current = session.get(Node, 2)
        current.seed_text = "短的当前选文"
        current.kind = "branch"
        session.add(current)
        session.commit()
    events = ask(client, tools=["mcp_server_88"])
    assert events[-1].get("done") is True, events
    assert calls["discover"] == [["mcp_server_88"]]
    assert not calls["stream"] and calls["agent"] and calls["titles"]


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


def catalog_definitions():
    definitions = [
        {
            "type": "function",
            "function": {
                "name": f"mcp_88_auxiliary_{index}",
                "description": f"Auxiliary operation {index}. " + "Detailed schema guidance. " * 32,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
        for index in range(52)
    ]
    definitions.append(
        {
            "type": "function",
            "function": {
                "name": "mcp_88_inspect_fixture",
                "description": "Inspect a bounded fixture using its original argument schema.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "mode": {"type": "string", "enum": ["read"]},
                    },
                    "required": ["path", "mode"],
                    "additionalProperties": False,
                },
            },
        }
    )
    return definitions


def model_response(protocol, calls=(), content=None):
    result = {"content": content, "tool_calls": list(calls)}
    if protocol == "anthropic":
        result["_anthropic_content"] = [
            {
                "type": "thinking",
                "thinking": "Synthetic tool planning",
                "signature": "opaque-test-signature",
            },
            *[
                {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]}
                for call in calls
            ],
            *([{"type": "text", "text": content}] if content else []),
        ]
    return result


def enable_catalog_fixture(api, monkeypatch, protocol):
    import app.llm as llm

    _client, engine, _calls = api
    definitions = catalog_definitions()
    with Session(engine) as session:
        cfg = session.get(ModelConfig, 1)
        cfg.protocol, cfg.context_window, cfg.max_tokens = protocol, 32768, 4096
        session.add(cfg)
        session.commit()
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args: (copy.deepcopy(definitions), {}))
    monkeypatch.setattr(nodes, "run_agent", llm.run_agent)
    return definitions


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_many_selected_tools_discover_exact_schema_then_execute_with_bounded_requests(
    api, monkeypatch, protocol
):
    import app.llm as llm

    client, engine, _calls = api
    definitions = enable_catalog_fixture(api, monkeypatch, protocol)
    target = definitions[-1]
    name = target["function"]["name"]
    source = make_history_long(engine)
    with Session(engine) as session:
        originals = {m.id: m.model_dump() for m in session.exec(select(Message)).all()}
    requests, executed = [], []

    def execute(tool, args):
        executed.append((tool, copy.deepcopy(args)))
        return "SYNTHETIC_FIXTURE_EVIDENCE: the verified result is forty-two."

    monkeypatch.setattr(nodes, "make_executor", lambda _router: execute)

    def chat_once(spec, conversation, tools):
        request = copy.deepcopy((conversation, tools))
        requests.append(request)
        system, messages = conversation[0]["content"], conversation[1:]
        assert (
            estimate_request_tokens(system, messages, tools, protocol)
            <= ContextPolicy.from_spec(spec).input_budget
        )
        assert "SYNTHETIC_SIBLING_SECRET" not in str(request)
        assert "SYNTHETIC_FOREIGN_SECRET" not in str(request)
        advertised = {item["function"]["name"]: item for item in tools}
        assert SOURCE_TOOL_NAME in advertised
        if len(requests) == 1:
            assert tool_definition_cost(definitions) > ContextPolicy.from_spec(spec).input_budget
            assert SEARCH_TOOL_NAME in advertised and name not in advertised
            return model_response(
                protocol,
                [
                    {
                        "id": "find_fixture",
                        "name": SEARCH_TOOL_NAME,
                        "args": {"query": name, "limit": 1},
                    }
                ],
            )
        if len(requests) == 2:
            assert advertised[name] == target, "discovery must expose the original exact schema"
            assert executed == [], "searching is metadata discovery, never tool execution"
            assert conversation[-1]["tool_call_id"] == "find_fixture"
            assert json.loads(conversation[-1]["content"])["loaded"] == [name]
            return model_response(
                protocol,
                [
                    {
                        "id": "read_fixture",
                        "name": name,
                        "args": {"path": "fixture.txt", "mode": "read"},
                    }
                ],
            )
        assert len(requests) == 3
        assert conversation[-1]["tool_call_id"] == "read_fixture"
        assert "SYNTHETIC_FIXTURE_EVIDENCE" in conversation[-1]["content"]
        if protocol == "anthropic":
            native = [m["_anthropic_content"] for m in conversation if "_anthropic_content" in m]
            assert [blocks[1]["id"] for blocks in native] == ["find_fixture", "read_fixture"]
            assert all(blocks[0]["signature"] == "opaque-test-signature" for blocks in native)
        return model_response(protocol, content="根据工具证据，结果是 forty-two。")

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    events = ask(client, "请检查已选工具，并根据返回的信息作答。", tools=["mcp_server_88"])
    assert events[-1].get("done") is True, events[-1]
    assert len(requests) == 3
    assert executed == [(name, {"path": "fixture.txt", "mode": "read"})]
    assert "opaque-test-signature" not in json.dumps(events)
    with Session(engine) as session:
        assert session.get(Message, 2).content == source
        assert all(
            session.get(Message, mid).model_dump() == original
            for mid, original in originals.items()
        )
        assert session.get(Node, 2).status == "complete"
        answers = session.exec(
            select(Message).where(Message.node_id == 2, Message.role == "assistant")
        ).all()
        assert len(answers) == 1 and "forty-two" in answers[0].content


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_discovery_does_not_authorize_same_response_or_unselected_tool_calls(
    api, monkeypatch, protocol
):
    import app.llm as llm

    client, _engine, _calls = api
    definitions = enable_catalog_fixture(api, monkeypatch, protocol)
    name = definitions[-1]["function"]["name"]
    executed, requests = [], []
    monkeypatch.setattr(
        nodes, "make_executor", lambda _router: lambda tool, args: executed.append((tool, args))
    )

    def chat_once(spec, conversation, tools):
        requests.append(copy.deepcopy(conversation))
        assert (
            estimate_request_tokens(conversation[0]["content"], conversation[1:], tools, protocol)
            <= ContextPolicy.from_spec(spec).input_budget
        )
        if len(requests) == 1:
            assert name not in [item["function"]["name"] for item in tools]
            return model_response(
                protocol,
                [
                    {"id": "discover", "name": SEARCH_TOOL_NAME, "args": {"query": name}},
                    {
                        "id": "too_early",
                        "name": name,
                        "args": {"path": "fixture.txt", "mode": "read"},
                    },
                    {
                        "id": "not_selected",
                        "name": "mcp_999_write_external_file",
                        "args": {"path": "forbidden.txt"},
                    },
                ],
            )
        assert executed == [], (
            "neither a same-round discovered tool nor an unselected tool may execute"
        )
        results = {m["tool_call_id"]: m["content"] for m in conversation if m["role"] == "tool"}
        assert set(results) == {"discover", "too_early", "not_selected"}
        assert "error" in results["too_early"] and "error" in results["not_selected"]
        return model_response(protocol, content="未执行未提供的工具。")

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    events = ask(client, "请检查可用能力。", tools=["mcp_server_88"])
    assert events[-1].get("done") is True, events[-1]
    assert executed == [] and len(requests) == 2


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("ignores_final_instruction", [False, True])
def test_bounded_tool_rounds_synthesize_without_replaying_actions(
    monkeypatch, protocol, ignores_final_instruction
):
    import app.llm as llm
    from app.context_budget import TOOL_OMISSION
    from app.tool_catalog import ToolCatalog

    spec = llm.LLMSpec(
        label="Offline fixture",
        base_url="https://offline.invalid/v1",
        llm_model="synthetic",
        api_key="synthetic-key-never-sent",
        protocol=protocol,
    )
    policy = ContextPolicy.from_spec(spec)
    definitions = catalog_definitions()
    name = definitions[-1]["function"]["name"]
    catalog = ToolCatalog(definitions, budget=policy.input_budget // 3)
    messages = [{"role": "user", "content": "只执行一次，再总结结果。"}]
    original = copy.deepcopy(messages)
    full_result = "EVIDENCE_START " + "synthetic body " * 4000 + " EVIDENCE_END"
    requests, executed = [], []

    def execute(tool, args):
        executed.append((tool, copy.deepcopy(args)))
        return full_result

    def chat_once(spec, conversation, tools):
        requests.append(copy.deepcopy((conversation, tools)))
        assert (
            estimate_request_tokens(conversation[0]["content"], conversation[1:], tools, protocol)
            <= policy.input_budget
        )
        assert original[0] in conversation
        if len(requests) == 1:
            assert SEARCH_TOOL_NAME in [item["function"]["name"] for item in tools]
            return model_response(
                protocol,
                [{"id": "search_once", "name": SEARCH_TOOL_NAME, "args": {"query": name}}],
            )
        if len(requests) == 2:
            assert definitions[-1] in tools and executed == []
            return model_response(
                protocol,
                [
                    {
                        "id": "execute_once",
                        "name": name,
                        "args": {"path": "fixture.txt", "mode": "read"},
                    }
                ],
            )
        assert len(requests) == 3 and tools == []
        assert conversation[-1]["tool_call_id"] == "execute_once"
        assert TOOL_OMISSION in conversation[-1]["content"]
        assert "EVIDENCE_START" in conversation[-1]["content"]
        assert "EVIDENCE_END" in conversation[-1]["content"]
        if ignores_final_instruction:
            return model_response(
                protocol,
                [
                    {
                        "id": "do_not_execute",
                        "name": name,
                        "args": {"path": "fixture.txt", "mode": "read"},
                    }
                ],
            )
        return model_response(protocol, content="已执行一次；结果中间部分未附送，无法核实。")

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    output = llm.run_agent(
        spec,
        "使用返回的证据回答，不能推断省略的信息。",
        messages,
        catalog.definitions,
        execute,
        max_rounds=2,
        tool_catalog=catalog,
    )
    if ignores_final_instruction:
        with pytest.raises(RuntimeError, match="最终回答"):
            list(output)
    else:
        events = list(output)
        assert "已执行一次" in "".join(e.get("text", "") for e in events)
    assert len(requests) == 3
    assert executed == [(name, {"path": "fixture.txt", "mode": "read"})]
    assert messages == original
