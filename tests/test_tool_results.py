"""Complete tool results: stored per call, paged by the model and the UI, bound to one
answer, removed with their node or topic and kept out of exports. Offline doubles only."""

import copy
import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, ImageContent, TextContent
from sqlmodel import Session, SQLModel, create_engine, select

import app.llm as llm
import app.service as service
from app import mcp_client
from app.models import KnowledgeTree, Message, ModelConfig, Node, ToolResult
from app.routers import nodes, trees
from app.tool_catalog import SEARCH_TOOL_NAME, ToolCatalog
from app.tool_results import (
    MAX_STORED_CHARS,
    TOOL_RESULT_TOOL_DEF,
    TOOL_RESULT_TOOL_NAME,
    make_tool_result_reader,
    portable_steps,
)
from app.tools import ToolText

FULL = "".join(f"SEGMENT-{index:05d} " for index in range(5000))  # 70,000 characters
LOOKUP = {
    "type": "function",
    "function": {
        "name": "mcp_5_lookup",
        "description": "Look up a synthetic fixture document.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool-results.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args: ([copy.deepcopy(LOOKUP)], {}))
    monkeypatch.setattr(nodes, "run_agent", llm.run_agent)
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return ToolText(mcp_client._bounded(FULL), FULL)

    monkeypatch.setattr(nodes, "make_executor", lambda _router: execute)
    nodes._GENERATIONS.clear()
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="Topic"), KnowledgeTree(id=2, title="Other")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="Root", status="complete"),
                Node(id=2, tree_id=1, parent_id=1, title="Branch"),
                Node(id=4, tree_id=2, title="Other root", status="complete"),
                ModelConfig(
                    id=1,
                    label="Offline",
                    base_url="https://offline.invalid/v1",
                    llm_model="synthetic",
                    api_key="synthetic-key-never-sent",
                    is_default=True,
                ),
            ]
        )
        session.commit()
    application = FastAPI()
    application.include_router(nodes.router)
    application.include_router(trees.router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[nodes.get_session] = session_override
    yield TestClient(application), engine, executed
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    nodes._GENERATIONS.clear()
    engine.dispose()


def ask(client, node_id=2, **body):
    response = client.post(f"/nodes/{node_id}/ask", json={"question": "查一下资料", **body})
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def scripted_model(monkeypatch, rounds):
    requests = []

    def chat_once(spec, conversation, tools):
        requests.append((copy.deepcopy(conversation), [t["function"]["name"] for t in tools]))
        return rounds[len(requests) - 1](conversation, requests[-1][1])

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    return requests


def call(name, args, call_id):
    return {"content": None, "tool_calls": [{"id": call_id, "name": name, "args": args}]}


def test_bounded_result_is_stored_complete_and_paged_by_the_model(api, monkeypatch):
    client, engine, executed = api

    def first(conversation, tools):
        assert "mcp_5_lookup" in tools and TOOL_RESULT_TOOL_NAME not in tools
        return call("mcp_5_lookup", {"q": "fixture"}, "lookup")

    def second(conversation, tools):
        assert TOOL_RESULT_TOOL_NAME in tools, "the reader joins once a tool has run"
        result = conversation[-1]["content"]
        assert len(result) < 25000 and "已截断" in result
        assert "完整结果共 70000 字" in result and "read_tool_result(call_index=1)" in result
        return call(TOOL_RESULT_TOOL_NAME, {"call_index": 1, "start": 24000}, "reread")

    def third(conversation, tools):
        page = json.loads(conversation[-1]["content"])
        assert page["ok"] and page["tool"] == "mcp_5_lookup"
        assert page["text"] == FULL[24000:26000] and page["next_start"] == 26000
        assert page["total_chars"] == len(FULL) and page["stored_truncated"] is False
        return {"content": "根据完整结果回答。", "tool_calls": []}

    requests = scripted_model(monkeypatch, [first, second, third])
    events = ask(client, tools=["mcp_server_5"])
    assert events[-1].get("done") is True, events[-1]
    assert len(requests) == 3 and executed == [("mcp_5_lookup", {"q": "fixture"})]
    request_id = events[-1]["request_id"]
    ends = [event["tool_end"] for event in events if "tool_end" in event]
    assert [(end["name"], end["call_index"], end["truncated"]) for end in ends] == [
        ("mcp_5_lookup", 1, True),
        (TOOL_RESULT_TOOL_NAME, 2, False),
    ]
    assert len(ends[0]["result"]) == 3000
    with Session(engine) as session:
        rows = session.exec(select(ToolResult).order_by(ToolResult.call_index)).all()
        assert [(row.call_index, row.tool_name, row.node_id) for row in rows] == [
            (1, "mcp_5_lookup", 2),
            (2, TOOL_RESULT_TOOL_NAME, 2),
        ]
        assert rows[0].content == FULL and rows[0].truncated is False
        assert rows[0].arguments == {"q": "fixture"} and rows[0].request_id == request_id
        assert json.loads(rows[1].content)["text"] == FULL[24000:26000]
        answer = session.exec(select(Message).where(Message.role == "assistant")).one()
        assert answer.steps[0] == {
            "tool": "mcp_5_lookup",
            "result": ends[0]["result"],
            "truncated": True,
            "call_index": 1,
            "request_id": request_id,
        }
        assert answer.steps[1]["call_index"] == 2 and answer.steps[1]["truncated"] is False
    thread = client.get("/nodes/2/thread").json()["nodes"]
    assert thread[-1]["steps"][0]["call_index"] == 1
    first_page = client.get(
        "/nodes/2/tool-results/1", params={"request_id": request_id, "max_chars": 50000}
    ).json()
    assert first_page["text"] == FULL[:50000] and first_page["next_start"] == 50000
    assert first_page["tool"] == "mcp_5_lookup" and first_page["truncated"] is False
    rest = client.get(
        "/nodes/2/tool-results/1", params={"request_id": request_id, "start": 50000}
    ).json()
    assert rest["text"] == FULL[50000:] and rest["next_start"] is None
    for path, params in (
        ("/nodes/2/tool-results/1", {"request_id": "another-request"}),
        ("/nodes/2/tool-results/9", {"request_id": request_id}),
        ("/nodes/4/tool-results/1", {"request_id": request_id}),
    ):
        missing = client.get(path, params=params)
        assert missing.status_code == 404 and missing.json()["detail"] == "工具结果不存在或已删除。"
    assert (
        client.get("/nodes/99/tool-results/1", params={"request_id": request_id}).status_code == 404
    )
    assert client.get("/nodes/2/tool-results/1").status_code == 422

    exported = client.get("/trees/1/export").json()
    steps = next(m["steps"] for m in exported["messages"] if m["steps"])
    assert steps[0] == {"tool": "mcp_5_lookup", "result": ends[0]["result"], "truncated": True}
    assert request_id not in json.dumps(exported)
    imported = client.post("/trees/import", json=exported)
    assert imported.status_code == 200, imported.text

    assert client.delete("/nodes/2").status_code == 200
    with Session(engine) as session:
        assert session.exec(select(ToolResult)).all() == []


def test_reader_is_not_offered_before_a_tool_runs_or_in_the_final_round(monkeypatch):
    spec = llm.LLMSpec(
        label="Offline",
        base_url="https://offline.invalid/v1",
        llm_model="synthetic",
        api_key="synthetic-key-never-sent",
    )
    executed = []
    rounds = [
        # A premature call is refused: the reader was not advertised in this round.
        lambda convo, tools: call(TOOL_RESULT_TOOL_NAME, {"call_index": 1}, "early"),
        lambda convo, tools: call("mcp_5_lookup", {"q": "x"}, "run"),
        lambda convo, tools: call(TOOL_RESULT_TOOL_NAME, {"call_index": 2}, "read"),
        lambda convo, tools: {"content": "完成。", "tool_calls": []},
    ]
    requests = scripted_model(monkeypatch, rounds)
    events = list(
        llm.run_agent(
            spec,
            "system",
            [{"role": "user", "content": "q"}],
            [LOOKUP],
            lambda name, args: executed.append(name) or f"{name} output",
            max_rounds=3,
            late_tool_defs=[TOOL_RESULT_TOOL_DEF],
        )
    )
    assert [tools for _, tools in requests] == [
        ["mcp_5_lookup"],
        ["mcp_5_lookup"],
        ["mcp_5_lookup", TOOL_RESULT_TOOL_NAME],
        [],
    ]
    assert executed == ["mcp_5_lookup", TOOL_RESULT_TOOL_NAME]
    early = next(e for e in events if e["type"] == "tool_end")
    assert json.loads(early["result"])["error"] == "tool_not_available"


def test_catalog_never_registers_reserved_reader_names():
    impostor = copy.deepcopy(TOOL_RESULT_TOOL_DEF)
    impostor["function"]["description"] = "A selected tool claiming the reader's name."
    search = {"type": "function", "function": {"name": SEARCH_TOOL_NAME, "parameters": {}}}
    catalog = ToolCatalog([impostor, search, LOOKUP], budget=4000)
    names = [item["function"]["name"] for item in catalog.definitions]
    assert TOOL_RESULT_TOOL_NAME not in names
    found = json.loads(catalog.search({"query": TOOL_RESULT_TOOL_NAME}))
    assert TOOL_RESULT_TOOL_NAME not in [tool["name"] for tool in found["tools"]]


def seed(engine, rows):
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()


def test_reader_is_bound_to_its_node_and_request(api):
    _, engine, _ = api
    seed(
        engine,
        [
            ToolResult(
                node_id=2, request_id="mine", call_index=1, tool_name="t", content="甲" * 3000
            ),
            ToolResult(
                node_id=2, request_id="retried", call_index=1, tool_name="t", content="OTHER"
            ),
            ToolResult(
                node_id=4, request_id="mine", call_index=2, tool_name="t", content="FOREIGN"
            ),
        ],
    )
    read = make_tool_result_reader(engine, 2, "mine", max_page_chars=500)
    page = json.loads(read({"call_index": 1}))
    assert page["text"] == "甲" * 500 and page["next_start"] == 500 and page["total_chars"] == 3000
    assert json.loads(read({"call_index": 1, "start": 2900, "max_chars": 2000}))["end"] == 3000
    for args in ({"call_index": 2}, {"call_index": 3}):
        assert json.loads(read(args))["error"] == "result_unavailable"
    for args in (
        {},
        {"call_index": 0},
        {"call_index": True},
        {"call_index": "1"},
        {"call_index": 1, "start": -1},
        {"call_index": 1, "max_chars": 0},
        {"call_index": 1, "path": "/etc"},
        ["call_index"],
    ):
        assert json.loads(read(args))["error"] == "invalid_arguments", args


def test_late_writers_cannot_store_results_for_deleted_or_replaced_requests(api):
    client, engine, _ = api
    with Session(engine) as session:
        node = session.get(Node, 2)
        node.request_id = "current"
        session.add(node)
        session.commit()
    replaced = nodes.Generation(request_id="replaced-by-retry")
    assert not nodes._store_tool_result(2, replaced, 1, "t", {}, "late text")
    current = nodes.Generation(request_id="current")
    assert nodes._store_tool_result(2, current, 1, "t", {"a": 1}, "x" * (MAX_STORED_CHARS + 5))
    with Session(engine) as session:
        row = session.exec(select(ToolResult)).one()
        assert row.truncated is True and len(row.content) == MAX_STORED_CHARS
    assert client.delete("/nodes/2").status_code == 200
    assert not nodes._store_tool_result(2, current, 2, "t", {}, "after deletion")
    with Session(engine) as session:
        assert session.exec(select(ToolResult)).all() == []


@pytest.mark.parametrize("delete_whole_tree", [False, True])
def test_deletion_removes_only_the_owned_tool_results(api, delete_whole_tree):
    client, engine, _ = api
    seed(
        engine,
        [
            ToolResult(node_id=1, request_id="r", call_index=1, tool_name="t", content="root"),
            ToolResult(node_id=2, request_id="r", call_index=1, tool_name="t", content="branch"),
            ToolResult(node_id=4, request_id="r", call_index=1, tool_name="t", content="other"),
        ],
    )
    endpoint = "/trees/1" if delete_whole_tree else "/nodes/2"
    assert client.delete(endpoint).status_code == 200
    with Session(engine) as session:
        kept = sorted(row.content for row in session.exec(select(ToolResult)).all())
    assert kept == (["other"] if delete_whole_tree else ["other", "root"])


def test_mock_answers_store_no_tool_results(api):
    client, engine, executed = api
    with Session(engine) as session:
        model = session.get(ModelConfig, 1)
        model.base_url, model.api_key = "mock", "mock"
        session.add(model)
        session.commit()
    assert ask(client, tools=["mcp_server_5"])[-1].get("done") is True
    with Session(engine) as session:
        assert session.exec(select(ToolResult)).all() == [] and executed == []


def test_portable_steps_drop_local_references_only():
    steps = [
        {"tool": "t", "result": "r", "truncated": True, "call_index": 3, "request_id": "q"},
        {"tool": "u", "result": None},
    ]
    assert portable_steps(steps) == [
        {"tool": "t", "result": "r", "truncated": True},
        {"tool": "u", "result": None},
    ]
    assert steps[0]["call_index"] == 3  # The stored original is untouched.
    assert portable_steps(None) is None and portable_steps([]) == []


def test_mcp_text_is_bounded_for_the_model_but_complete_for_storage():
    result = CallToolResult(
        content=[
            TextContent(type="text", text="A" * 30000),
            ImageContent(type="image", mimeType="image/png", data="B" * 100000),
            TextContent(type="text", text="TAIL"),
        ],
        isError=True,
    )
    bounded, complete = mcp_client._result_texts(result)
    assert bounded == mcp_client._result_text(result)
    assert len(bounded) <= mcp_client.MAX_OUTPUT_CHARS and "已截断" in bounded
    assert complete.startswith("MCP 工具报告错误：\n" + "A" * 30000) and complete.endswith("TAIL")
    assert "B" * 100 not in complete and "image/png" in complete
    structured = CallToolResult(
        content=[], structuredContent={"answer": "C" * 5000, "data": "D" * 5000}
    )
    bounded, complete = mcp_client._result_texts(structured)
    assert "C" * 5000 not in bounded and "C" * 5000 in complete
    assert "D" * 100 not in complete


def test_executor_carries_the_complete_mcp_text(monkeypatch):
    def call_tool(spec, name, args, full=None):
        full.append("complete " * 10)
        return "bounded"

    monkeypatch.setattr(mcp_client, "call_tool", call_tool)
    execute = service.make_executor({"mcp_5_lookup": ("mcp", {"command": "x"}, "lookup")})
    result = execute("mcp_5_lookup", {})
    assert result == "bounded" and result.full == "complete " * 10
    builtin = service.make_executor({})("missing", {})
    assert getattr(builtin, "full", builtin) == "未知工具：missing"
