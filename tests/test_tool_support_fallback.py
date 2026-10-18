"""Endpoints without tool support still answer when only implicit readers ride along."""

import json
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.service as service
from app import llm
from app.models import KnowledgeTree, ModelConfig, Node, RetrievalSettings
from app.routers import nodes


class RejectsTools(Exception):
    """What an OpenAI-compatible server without a tool template answers."""

    status_code = 400

    def __str__(self) -> str:
        return "Error code: 400 - registry.ollama.ai/library/synthetic does not support tools"


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=finish, delta=delta)])


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        pass


class Endpoint:
    """A provider double: any request carrying tools is rejected with HTTP 400."""

    def __init__(self):
        self.requests: list[bool] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append("tools" in kwargs)
        if "tools" in kwargs:
            raise RejectsTools()
        return _Stream([_chunk("不用工具也能回答。"), _chunk(finish="stop")])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tools.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (nodes, service, database):
        monkeypatch.setattr(module, "engine", engine)
    endpoint = Endpoint()
    monkeypatch.setattr(llm, "_client", lambda spec: endpoint)
    monkeypatch.setattr(llm, "TOOLS_UNSUPPORTED", set())
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="主题"))
        session.add(Node(id=1, tree_id=1, title="根", status="complete"))
        session.add(RetrievalSettings(include_siblings=True))
        session.add(
            ModelConfig(
                label="Local model",
                base_url="http://127.0.0.1:11434/v1",
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

    application.dependency_overrides[database.get_session] = session_override
    yield TestClient(application), endpoint
    nodes._GENERATIONS.clear()
    engine.dispose()


def ask(client, node_id, question, **body):
    response = client.post(f"/nodes/{node_id}/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def test_widened_recall_falls_back_to_a_plain_answer_once_per_endpoint(setup, monkeypatch):
    client, endpoint = setup
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args: ([], {}))
    first = ask(client, 1, "第一个问题")
    assert first[-1].get("done"), first[-1]
    assert "".join(event.get("delta", "") for event in first) == "不用工具也能回答。"
    # One rejected request with the implicit readers, then the same turn without tools.
    assert endpoint.requests == [True, False]
    second = ask(client, first[-1]["node_id"], "第二个问题")
    assert second[-1].get("done"), second[-1]
    assert endpoint.requests == [True, False, False], "the endpoint is remembered"


def test_tools_the_user_selected_are_never_dropped_silently(setup, monkeypatch):
    client, endpoint = setup
    selected = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
    monkeypatch.setattr(
        nodes, "assemble_tools", lambda *args: ([selected], {"web_search": ("builtin", "x")})
    )
    events = ask(client, 1, "查一下", tools=["web_search"])
    assert events[-1]["status"] == "error"
    assert events[-1]["error"] == nodes.TOOLS_REJECTED_ERROR
    assert endpoint.requests == [True]
