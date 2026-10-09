"""Per-model estimator calibration and per-answer diagnostics: offline doubles only."""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

import app.service as service
from app import db
from app.calibration import load_calibration, record_usage
from app.context_budget import ContextPolicy
from app.models import KnowledgeTree, Message, ModelCalibration, ModelConfig, Node
from app.routers import nodes, trees
from app.service import to_spec

BASE_PROMPT = "你是帮助用户理解陌生知识的助教"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'calibration.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    nodes._GENERATIONS.clear()
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="校准"))
        session.add(Node(id=1, tree_id=1, title="根", status="idle"))
        session.add_all(
            [
                ModelConfig(
                    id=1,
                    label="Real",
                    base_url="https://offline.invalid/v1",
                    llm_model="synthetic",
                    api_key="synthetic-key-never-sent",
                    is_default=True,
                    context_window=8192,
                    max_tokens=1024,
                ),
                ModelConfig(id=2, label="Mock", base_url="mock", llm_model="mock", api_key="mock"),
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
        yield client, engine, monkeypatch
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    nodes._GENERATIONS.clear()
    engine.dispose()


def streaming_double(monkeypatch, estimated, actual, cached=0, output=40):
    def stream(spec, system, messages, **kwargs):
        kwargs["diagnostics"]["rounds"] = [
            {
                "estimated_input_tokens": estimated,
                "input_tokens": actual,
                "cached_input_tokens": cached,
                "output_tokens": output,
            }
        ]
        yield "text", "合成回答。"

    monkeypatch.setattr(nodes, "stream_chat", stream)


def ask(client, node_id=1, **body):
    response = client.post(f"/nodes/{node_id}/ask", json={"question": "解释一下", **body})
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaves(item)
    else:
        yield value


def test_record_usage_gates_small_requests_clamps_and_averages(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ratio.db'}")
    SQLModel.metadata.create_all(engine)
    assert record_usage(engine, 1, 1999, 4000) is None, "small estimates are too noisy"
    assert record_usage(engine, 1, 4000, 0) is None, "missing usage never learns"
    assert record_usage(engine, None, 4000, 4000) is None
    assert record_usage(engine, 1, 2000, 40000) == 3.0, "ratio is clamped to [0.5, 3.0]"
    assert record_usage(engine, 1, 2000, 100) == pytest.approx(3.0 * 0.7 + 0.5 * 0.3)
    with Session(engine) as session:
        row = session.get(ModelCalibration, 1)
        assert row.samples == 2 and row.estimated_total == 4000 and row.actual_total == 40100
        assert load_calibration(session, 1) == pytest.approx(row.ratio)
        assert load_calibration(session, 99) == 1.0 and load_calibration(None, 1) == 1.0
    assert record_usage(engine, 2, 10000, 5000) == 0.5


def test_real_answers_learn_calibration_and_shrink_the_next_budget(workspace):
    client, engine, monkeypatch = workspace
    streaming_double(monkeypatch, estimated=4000, actual=6000, cached=1000)
    events = ask(client)
    assert events[-1]["status"] == "complete", events
    with Session(engine) as session:
        row = session.get(ModelCalibration, 1)
        assert row.samples == 1 and row.ratio == pytest.approx(1.5)
        spec = to_spec(session.get(ModelConfig, 1), session)
    assert spec.calibration == pytest.approx(1.5)
    assert ContextPolicy.from_spec(spec).input_budget == int(
        ContextPolicy(8192, 1024).input_budget / 1.5
    )
    streaming_double(monkeypatch, estimated=4000, actual=12000)
    assert ask(client)[-1]["status"] == "complete"
    with Session(engine) as session:
        assert session.get(ModelCalibration, 1).ratio == pytest.approx(1.5 * 0.7 + 3.0 * 0.3)
        assert session.get(ModelCalibration, 1).samples == 2


def test_small_estimates_and_demo_answers_never_learn(workspace):
    client, engine, monkeypatch = workspace
    streaming_double(monkeypatch, estimated=1500, actual=9000)
    assert ask(client)[-1]["status"] == "complete"
    streaming_double(monkeypatch, estimated=5000, actual=9000)
    assert ask(client, config_id=2)[-1]["status"] == "complete"
    with Session(engine) as session:
        assert session.exec(select(ModelCalibration)).all() == []


def test_diagnostics_are_emitted_once_persisted_listed_and_never_exported(workspace):
    client, engine, monkeypatch = workspace
    streaming_double(monkeypatch, estimated=3000, actual=2600, cached=800, output=55)
    events = ask(client)
    assert events[-1]["done"] is True
    reports = [event for event in events if "diagnostics" in event]
    assert len(reports) == 1 and "seq" in reports[0]
    assert events.index(reports[0]) < events.index(events[-1])
    report = reports[0]["diagnostics"]
    assert report["estimated_input_tokens"] == 3000 and report["actual_input_tokens"] == 2600
    assert report["cached_input_tokens"] == 800 and report["output_tokens"] == 55
    assert report["compacted"] is False and report["tool_rounds"] == 0
    assert report["calibration_ratio"] == 1.0
    assert set(report["timings_ms"]) == {"retrieval", "tools", "summary", "model", "total"}
    assert all(isinstance(value, (int, float, bool, type(None))) for value in leaves(report))
    assert BASE_PROMPT not in json.dumps(events, ensure_ascii=False)
    with Session(engine) as session:
        answer = session.exec(
            select(Message).where(Message.role == "assistant").order_by(Message.id.desc())
        ).first()
        assert answer.diagnostics == report
    thread = client.get("/nodes/1/thread").json()["nodes"]
    assert thread[-1]["diagnostics"] == report
    backup = client.get("/trees/1/export").json()
    assert "diagnostics" not in json.dumps(backup)
    assert client.post("/trees/import", json=backup).status_code == 200


def test_legacy_schema_gains_diagnostics_column_and_calibration_table(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE message (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, answered_by TEXT, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO message VALUES (5, 1, 'assistant', '旧回答', NULL, '2026-01-01')")
        )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()
    columns = {column["name"] for column in inspect(engine).get_columns("message")}
    assert "diagnostics" in columns and "steps" in columns
    assert "modelcalibration" in inspect(engine).get_table_names()
    with Session(engine) as session:
        message = session.get(Message, 5)
        assert message.content == "旧回答" and message.diagnostics is None
    engine.dispose()
