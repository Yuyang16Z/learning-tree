"""Documents reach the model, retain provenance and survive user workflows offline."""

import copy
import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.service as service
from app.context_budget import ContextPolicy, estimate_request_tokens
from app.document_context import DOCUMENT_TOOL_NAME
from app.documents import DocumentAttachment
from app.documents import router as document_router
from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes, trees


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'documents.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args: ([], {}))
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    calls = []

    def agent(spec, system, messages, tools, execute, **kwargs):
        calls.append((spec, system, copy.deepcopy(messages), tools, execute))
        yield {"type": "delta", "text": "Verified offline document response."}

    monkeypatch.setattr(nodes, "run_agent", agent)
    monkeypatch.setattr(
        nodes, "stream_chat", lambda *args, **kwargs: iter([("text", "No documents.")])
    )
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="One"), KnowledgeTree(id=2, title="Other")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="Root"),
                Node(id=2, tree_id=1, parent_id=1, title="Sibling"),
                Node(id=3, tree_id=2, title="Other"),
            ]
        )
        session.add(
            ModelConfig(
                id=1,
                label="Offline",
                base_url="https://offline.invalid",
                llm_model="fake",
                api_key="synthetic-not-sent",
                is_default=True,
                context_window=8192,
                max_tokens=1024,
            )
        )
        session.commit()
    nodes._GENERATIONS.clear()
    app = FastAPI()
    for router in (document_router, nodes.router, trees.router):
        app.include_router(router)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[nodes.get_session] = override
    with TestClient(app) as client:
        yield client, engine, calls
    nodes._GENERATIONS.clear()
    engine.dispose()


def upload(client, name="notes.md", text="The required calibration value is 7319.", node=1):
    result = client.post(
        f"/nodes/{node}/documents", files={"file": (name, text.encode(), "text/plain")}
    )
    assert result.status_code == 200, result.text
    return result.json()


def ask(client, node=1, question="What is the calibration value?", **kwargs):
    result = client.post(f"/nodes/{node}/ask", json={"question": question, **kwargs})
    assert result.status_code == 200, result.text
    events = [
        json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")
    ]
    return events[-1]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_extracted_text_reaches_budgeted_model_and_same_path(api, protocol):
    client, engine, calls = api
    with Session(engine) as session:
        model = session.get(ModelConfig, 1)
        model.protocol = protocol
        session.add(model)
        session.commit()
    doc = upload(client)
    assert not calls, "upload must never invoke a model"
    assert ask(client, document_ids=[doc["id"]])["status"] == "complete"
    spec, system, messages, tools, execute = calls[-1]
    assert "7319" in system and "notes.md" in system and "不是系统指令" not in str(messages)
    assert "而非指令" in system
    assert DOCUMENT_TOOL_NAME in [t["function"]["name"] for t in tools]
    assert (
        estimate_request_tokens(system, messages, tools, protocol)
        <= ContextPolicy.from_spec(spec).input_budget
    )
    assert messages[-1]["content"] == "What is the calibration value?"
    result = json.loads(execute(DOCUMENT_TOOL_NAME, {"action": "read", "document_id": doc["id"]}))
    assert result["ok"] and "7319" in result["text"] and result["source"]
    continued = ask(client, question="Explain the same calibration.")
    assert continued["status"] == "complete" and "7319" in calls[-1][1]


def test_large_document_relevance_and_reread_without_full_prompt(api):
    client, engine, calls = api
    text = "Ordinary background without a special condition.\n" * 2200
    text += "ZXQ_CALIBRATION condition is 9472. Only valid after approval."
    doc = upload(client, text=text)
    result = ask(
        client, question="What is the ZXQ_CALIBRATION condition?", document_ids=[doc["id"]]
    )
    assert result["status"] == "complete"
    spec, system, messages, tools, execute = calls[-1]
    assert "9472" in system and len(system) < len(text) // 2
    assert (
        estimate_request_tokens(system, messages, tools)
        <= ContextPolicy.from_spec(spec).input_budget
    )
    read = json.loads(
        execute(
            DOCUMENT_TOOL_NAME,
            {"action": "search", "document_id": doc["id"], "query": "ZXQ_CALIBRATION"},
        )
    )
    assert read["ok"] and any("9472" in hit["text"] for hit in read["matches"])


def test_retry_keeps_original_attachment_and_revision_can_remove_it(api, monkeypatch):
    client, engine, calls = api
    doc = upload(client)
    successful = nodes.run_agent

    def fail(*args, **kwargs):
        yield {"type": "delta", "text": "Partial response."}
        raise RuntimeError("synthetic interrupt")

    monkeypatch.setattr(nodes, "run_agent", fail)
    assert ask(client, document_ids=[doc["id"]])["status"] == "error"
    monkeypatch.setattr(nodes, "run_agent", successful)
    assert ask(client, question="", mode="retry")["status"] == "complete"
    assert "7319" in calls[-1][1]
    with Session(engine) as session:
        questions = session.exec(
            select(Message).where(Message.node_id == 1, Message.role == "user")
        ).all()
        assert len(questions) == 1 and questions[0].document_ids == [doc["id"]]
    revision = ask(
        client, question="A question without a document.", mode="revise", document_ids=[]
    )
    rows = client.get(f"/nodes/{revision['node_id']}/thread").json()["nodes"]
    assert rows[-1]["documents"] == []
    assert client.get("/nodes/1/thread").json()["nodes"][-1]["documents"][0]["id"] == doc["id"]


def test_foreign_ids_rejected_and_sibling_not_implicitly_recalled(api):
    client, engine, calls = api
    foreign = upload(client, text="FOREIGN_DOCUMENT_PRIVATE", node=3)
    result = client.post("/nodes/1/ask", json={"question": "Read", "document_ids": [foreign["id"]]})
    assert result.status_code in (404, 422)
    sibling = upload(client, text="SIBLING_DOCUMENT_PRIVATE", node=2)
    assert ask(client, node=2, document_ids=[sibling["id"]])["status"] == "complete"
    own = upload(client)
    assert ask(client, document_ids=[own["id"]])["status"] == "complete"
    assert "SIBLING_DOCUMENT_PRIVATE" not in calls[-1][1]
    reader = calls[-1][-1]
    for forbidden in (foreign, sibling):
        result = json.loads(
            reader(DOCUMENT_TOOL_NAME, {"action": "read", "document_id": forbidden["id"]})
        )
        assert result["error"] == "source_unavailable"


def test_document_only_prompt_and_portable_backup_roundtrip(api):
    client, engine, calls = api
    doc = upload(client)
    assert ask(client, question="", document_ids=[doc["id"]])["status"] == "complete"
    assert calls[-1][2][-1]["content"].startswith("请概括")
    backup = client.get("/trees/1/export").json()
    assert backup["version"] == 2 and len(backup["documents"]) == 1
    assert "synthetic-not-sent" not in json.dumps(backup)
    imported = client.post("/trees/import", json=backup)
    assert imported.status_code == 200, imported.text
    root = imported.json()["root_node_id"]
    restored = client.get(f"/nodes/{root}/thread").json()["nodes"][0]["documents"][0]
    assert restored["id"] != doc["id"] and restored["name"] == doc["name"]
    assert (
        client.get(f"/documents/{restored['id']}/download").content
        == client.get(f"/documents/{doc['id']}/download").content
    )
    broken = copy.deepcopy(backup)
    broken["documents"][0]["sha256"] = "0" * 64
    assert client.post("/trees/import", json=broken).status_code == 422
    assert client.delete("/trees/1").status_code == 200
    assert client.get(f"/documents/{doc['id']}").status_code == 404
    assert client.get(f"/documents/{restored['id']}").status_code == 200
    with Session(engine) as session:
        assert session.get(DocumentAttachment, restored["id"]) is not None
