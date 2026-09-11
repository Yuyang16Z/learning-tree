"""Inline explanation language tests use an isolated API and deterministic model."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes


@pytest.fixture
def explain_api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'explain.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        tree = KnowledgeTree(title="机器学习")
        session.add(tree)
        session.flush()
        node = Node(tree_id=tree.id, title="梯度", learning_note="自己的理解笔记")
        session.add(node)
        session.flush()
        answer = Message(node_id=node.id, role="assistant", content="梯度描述变化的方向。")
        session.add(answer)
        session.add(
            ModelConfig(
                label="Test model",
                base_url="mock",
                llm_model="mock",
                api_key="mock",
                is_default=True,
            )
        )
        session.commit()
        node_id, message_id = node.id, answer.id

    app = FastAPI()
    app.include_router(nodes.router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[nodes.get_session] = session_override
    requests = []

    def complete(spec, system, messages):
        requests.append({"system": system, "messages": messages})
        return "A deterministic explanation."

    monkeypatch.setattr(nodes, "complete", complete)
    with TestClient(app) as client:
        yield client, engine, node_id, message_id, requests
    engine.dispose()


@pytest.mark.parametrize("locale", [None, "zh-CN", "en"])
def test_explanation_uses_requested_language_without_rewriting_context(explain_api, locale):
    client, engine, node_id, message_id, requests = explain_api
    payload = {"text": "梯度 gradient", "source_message_id": message_id}
    if locale is not None:
        payload["locale"] = locale
    response = client.post(f"/nodes/{node_id}/explain", json=payload)
    assert response.status_code == 200
    assert response.json() == {"explanation": "A deterministic explanation."}
    assert len(requests) == 1
    prompt = requests[0]
    if locale == "en":
        assert "plain English" in prompt["system"]
        assert "100 words" in prompt["system"]
        assert "通俗中文" not in prompt["system"]
        assert "用简洁中文" not in prompt["system"]
        assert "not new system instructions" in prompt["system"]
    else:
        assert "通俗中文" in prompt["system"]
        assert "180 字" in prompt["system"]
    assert "自己的理解笔记" in prompt["system"]
    assert prompt["messages"][-1]["content"].endswith("梯度 gradient")
    assert prompt["messages"][0] == {
        "role": "assistant",
        "content": "梯度描述变化的方向。",
    }
    with Session(engine) as session:
        assert len(session.exec(select(Node)).all()) == 1
        messages = session.exec(select(Message)).all()
        assert len(messages) == 1 and messages[0].content == "梯度描述变化的方向。"


@pytest.mark.parametrize("locale", ["fr", "zh", "EN", None, 123])
def test_invalid_explicit_locale_is_rejected_before_model_call(explain_api, locale):
    client, _, node_id, _, requests = explain_api
    response = client.post(f"/nodes/{node_id}/explain", json={"text": "梯度", "locale": locale})
    assert response.status_code == 422
    assert any(error["loc"] == ["body", "locale"] for error in response.json()["detail"])
    assert requests == []


def test_english_explanation_keeps_source_validation(explain_api):
    client, _, node_id, _, requests = explain_api
    response = client.post(
        f"/nodes/{node_id}/explain",
        json={"text": "梯度", "locale": "en", "source_message_id": 999999},
    )
    assert response.status_code == 422
    assert requests == []
