"""Summaries of older history are prepared after answers, never while one waits.

Temporary SQLite and synthetic provider responses only.
"""

import os
import threading
from dataclasses import replace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app import db
from app import learning_summaries as summaries
from app.config import Settings
from app.llm import LLMSpec
from app.models import ContextSummary, KnowledgeTree, Message, ModelConfig, Node
from app.service import summary_spec, to_spec

TOPICS = {1: "recursion base cases", 2: "pointer aliasing rules", 3: "gradient descent steps"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'background.sqlite'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="Learning"))
        for node_id, topic in TOPICS.items():
            session.add(
                Node(
                    id=node_id,
                    tree_id=1,
                    parent_id=None if node_id == 1 else node_id - 1,
                    title=f"Node {node_id}",
                    status="complete",
                )
            )
            session.add_all(
                [
                    Message(
                        id=node_id * 10 + 1,
                        node_id=node_id,
                        role="user",
                        content=f"Explain {topic}.",
                    ),
                    Message(
                        id=node_id * 10 + 2,
                        node_id=node_id,
                        role="assistant",
                        content=f"Source {node_id} explains {topic} under stated conditions. " * 40,
                    ),
                ]
            )
        session.commit()

    def sources(*ids):
        with Session(engine) as session:
            return [
                (
                    session.get(Node, node_id),
                    list(
                        session.exec(
                            select(Message).where(Message.node_id == node_id).order_by(Message.id)
                        ).all()
                    ),
                )
                for node_id in ids
            ]

    calls = []

    def provider(spec, payload, max_output_bytes):
        calls.append((spec, payload, max_output_bytes))
        return f"covered:\n- An earlier explanation. [{payload[0]['source_id']}]"

    monkeypatch.setattr(summaries, "summarize_learning_context", provider)
    # Process-local claim and cooldown state must not leak between tests.
    summaries._FAILURES.clear()
    summaries._INFLIGHT.clear()
    spec = LLMSpec("Synthetic", "https://provider.invalid/v1", "model", "synthetic-secret-key")
    yield engine, sources, spec, calls
    summaries._FAILURES.clear()
    summaries._INFLIGHT.clear()
    engine.dispose()


def stored(engine):
    with Session(engine) as session:
        return list(session.exec(select(ContextSummary)).all())


def test_cached_only_resolution_never_calls_the_provider(workspace):
    engine, sources, spec, calls = workspace
    path = sources(1, 2)
    # Nothing is cached yet: the context builder falls back to its local excerpts.
    assert summaries.summarize_history(engine, spec, path, "pointer", 4000, generate=False) is None
    assert calls == []
    assert summaries.prepare_summaries(engine, spec, path, "pointer", 4000) == 1
    assert len(calls) == 1 and len(stored(engine)) == 1
    rendered = summaries.summarize_history(engine, spec, path, "pointer", 4000, generate=False)
    assert "模型摘要（node=2" in rendered
    assert len(calls) == 1, "reading cached summaries makes no provider call"


def test_preparation_spends_its_limit_on_the_most_relevant_missing_blocks(workspace):
    engine, sources, spec, calls = workspace
    path = sources(1, 2, 3)
    assert summaries.prepare_summaries(engine, spec, path, "gradient descent", 4000) == 1
    assert all(item["source_id"].startswith("node=3,") for item in calls[0][1])
    assert summaries.prepare_summaries(engine, spec, path, "gradient descent", 4000, limit=5) == 2
    assert len(calls) == 3 and len(stored(engine)) == 3
    assert summaries.prepare_summaries(engine, spec, path, "gradient descent", 4000, limit=5) == 0
    assert len(calls) == 3, "a cached block is never summarized again"


def test_failed_attempts_count_against_the_limit_and_cool_down(workspace, monkeypatch):
    engine, sources, spec, _ = workspace
    attempts = []

    def failing(spec, payload, max_output_bytes):
        attempts.append(payload[0]["source_id"])
        return None

    monkeypatch.setattr(summaries, "summarize_learning_context", failing)
    path = sources(1, 2, 3)
    assert summaries.prepare_summaries(engine, spec, path, "recursion", 4000, limit=2) == 0
    assert len(attempts) == 2, "the limit bounds provider calls, not only stored summaries"
    assert summaries.prepare_summaries(engine, spec, path, "recursion", 4000, limit=2) == 0
    assert len(attempts) == 3, "blocks that just failed cool down; only the untried one is asked"
    assert stored(engine) == []


def test_preparation_needs_a_real_model_room_and_unchanged_sources(workspace):
    engine, sources, spec, calls = workspace
    path = sources(1, 2)
    assert summaries.prepare_summaries(engine, replace(spec, api_key="mock"), path, "q", 4000) == 0
    assert summaries.prepare_summaries(engine, spec, path, "q", 511) == 0
    assert summaries.prepare_summaries(engine, spec, path, "q", 4000, limit=0) == 0
    stop = threading.Event()
    stop.set()
    assert summaries.prepare_summaries(engine, spec, path, "q", 4000, stop=stop) == 0
    with Session(engine) as session:
        node = session.get(Node, 1)
        node.learning_note = "Edited after the answer was assembled"
        session.add(node)
        session.commit()
    assert summaries.prepare_summaries(engine, spec, path, "q", 4000) == 0
    assert calls == [] and stored(engine) == []


def test_summary_spec_is_the_marked_model_or_the_answer_model(workspace):
    engine, _, _, _ = workspace
    with Session(engine) as session:
        answer = ModelConfig(
            id=1, label="Answer", base_url="https://a.invalid/v1", llm_model="big", api_key="k1"
        )
        cheap = ModelConfig(
            id=2, label="Cheap", base_url="https://b.invalid/v1", llm_model="small", api_key="k2"
        )
        session.add_all([answer, cheap])
        session.commit()
        answer_spec = to_spec(answer, session)
        assert summary_spec(session, answer, answer_spec) is answer_spec
        cheap.use_for_summaries = True
        session.add(cheap)
        session.commit()
        chosen = summary_spec(session, answer, answer_spec)
        assert (chosen.llm_model, chosen.api_key) == ("small", "k2")
        # Answering with the marked model itself needs no second spec.
        cheap_spec = to_spec(cheap, session)
        assert summary_spec(session, cheap, cheap_spec) is cheap_spec


@pytest.fixture
def legacy_models(tmp_path, monkeypatch):
    """A database from before the summary flag existed: no model writes summaries for others."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-summary-flag.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE modelconfig (id INTEGER PRIMARY KEY, label TEXT NOT NULL, "
                "base_url TEXT NOT NULL, llm_model TEXT NOT NULL, api_key TEXT NOT NULL, "
                "protocol TEXT NOT NULL, max_tokens INTEGER NOT NULL, "
                "context_window INTEGER NOT NULL, "
                "is_default BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO modelconfig VALUES (91, 'Synthetic model', 'https://mock.invalid', "
                "'mock', 'synthetic-key-918', 'openai', 4096, 32768, 1, '2026-01-01')"
            )
        )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()
    yield engine
    engine.dispose()


def _client(engine):
    from app.main import app

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db.get_session] = session_override
    return app, TestClient(app)


def test_migration_and_api_keep_at_most_one_summary_model(legacy_models):
    with Session(legacy_models) as session:
        assert session.get(ModelConfig, 91).use_for_summaries is False
    app, api = _client(legacy_models)
    try:
        assert api.get("/models").json()[0]["use_for_summaries"] is False
        payload = {
            "label": "Cheap summaries",
            "base_url": "https://mock.invalid/v1",
            "llm_model": "cheap",
            "api_key": "synthetic-cheap-key",
            "use_for_summaries": True,
        }
        first = api.post("/models", json=payload)
        assert first.status_code == 200 and first.json()["use_for_summaries"] is True
        second = api.post("/models", json={**payload, "label": "Other summaries"})
        assert second.status_code == 200 and second.json()["use_for_summaries"] is True
        flags = {model["id"]: model["use_for_summaries"] for model in api.get("/models").json()}
        assert flags == {91: False, first.json()["id"]: False, second.json()["id"]: True}
        # Marking another model moves the flag; clearing it leaves summaries to the answer model.
        moved = api.put(f"/models/{first.json()['id']}", json={**payload, "api_key": ""})
        assert moved.status_code == 200 and moved.json()["use_for_summaries"] is True
        assert [m["id"] for m in api.get("/models").json() if m["use_for_summaries"]] == [
            first.json()["id"]
        ]
        cleared = api.put(
            f"/models/{first.json()['id']}",
            json={**payload, "api_key": "", "use_for_summaries": False},
        )
        assert cleared.status_code == 200
        assert not any(m["use_for_summaries"] for m in api.get("/models").json())
        with Session(legacy_models) as session:
            assert session.get(ModelConfig, first.json()["id"]).api_key == "synthetic-cheap-key"
    finally:
        app.dependency_overrides.clear()


def test_summary_settings_have_documented_defaults_and_read_the_environment(monkeypatch):
    for name in ("CONTEXT_SUMMARY_MODE", "CONTEXT_SUMMARY_BLOCKS"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert (defaults.context_summary_mode, defaults.context_summary_blocks) == ("background", 1)
    monkeypatch.setenv("CONTEXT_SUMMARY_MODE", "inline")
    monkeypatch.setenv("CONTEXT_SUMMARY_BLOCKS", "3")
    configured = Settings(_env_file=None)
    assert (configured.context_summary_mode, configured.context_summary_blocks) == ("inline", 3)
    for name, value in (("CONTEXT_SUMMARY_BLOCKS", "9"), ("CONTEXT_SUMMARY_MODE", "later")):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError):
            Settings(_env_file=None)
        monkeypatch.delenv(name)
