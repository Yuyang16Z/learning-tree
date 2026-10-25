"""Per-model estimator calibration and per-answer diagnostics: offline doubles only."""

import json
import os
import threading

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

import app.llm as llm
import app.service as service
from app import changes, db
from app.calibration import (
    MIN_ESTIMATED_UNITS,
    calibration_sample,
    load_calibration,
    record_usage,
)
from app.context_budget import ContextPolicy, estimate_request_tokens, has_images
from app.models import KnowledgeTree, Message, ModelCalibration, ModelConfig, Node
from app.routers import models_router, nodes, trees
from app.service import MemoryNote, to_spec

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
    app.include_router(models_router.router)

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
    with Session(engine) as session:  # Ratios are written only for existing models.
        for config_id in (1, 2):
            session.add(
                ModelConfig(
                    id=config_id,
                    label="M",
                    base_url="https://m.invalid",
                    llm_model="m",
                    api_key="k",
                )
            )
        session.commit()
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


def test_calibration_sample_pairs_each_qualifying_request():
    rounds = [
        {"estimated_input_tokens": 3000, "input_tokens": 3600, "has_images": False},
        {"estimated_input_tokens": 1999, "input_tokens": 9000},  # Below the gate on its own.
        {"estimated_input_tokens": 5000, "input_tokens": None},  # Usage not reported.
        {"estimated_input_tokens": 5000, "input_tokens": 1000, "has_images": True},
        {"estimated_input_tokens": 4000, "input_tokens": 0},
        {"estimated_input_tokens": 2000, "input_tokens": 2400},
    ]
    assert calibration_sample(rounds) == (5000, 6000)
    assert calibration_sample(rounds[1:5]) is None and calibration_sample([]) is None
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    assert has_images([{"role": "user", "content": [{"type": "text", "text": "q"}, image]}])
    native = [{"type": "image", "source": {"type": "base64", "data": "AA=="}}]
    assert has_images([{"role": "assistant", "content": None, "_anthropic_content": native}])
    assert not has_images([{"role": "user", "content": "q"}, {"role": "tool", "content": "x"}])


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
    assert set(report["timings_ms"]) == {
        "retrieval",
        "tools",
        "summary",
        "approval",
        "model",
        "total",
    }
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


LOOKUP = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look a term up.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def counts(input_tokens):
    return {"input_tokens": input_tokens, "cached_input_tokens": 0, "output_tokens": 20}


def tool_loop(monkeypatch, tool_rounds, usage_for):
    """The real run_agent and request fitting; the double answers each fitted request
    with `tool_rounds` lookup calls, then a final answer, reporting usage_for(index, estimate)."""
    estimates = []

    def chat_once(spec, conversation, tools):
        system, messages = conversation[0]["content"], conversation[1:]
        estimate = estimate_request_tokens(system, messages, tools, spec.protocol)
        estimates.append(estimate)
        index = len(estimates) - 1
        reply = (
            {"content": None, "tool_calls": [{"id": f"call_{index}", "name": "lookup", "args": {}}]}
            if index < tool_rounds
            else {"content": "合成回答。", "tool_calls": []}
        )
        return {**reply, "usage": usage_for(index, estimate)}

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    monkeypatch.setattr(nodes, "run_agent", llm.run_agent)
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args, **kwargs: ([LOOKUP], {}))
    return estimates


def calibration_rows(engine):
    with Session(engine) as session:
        return [(row.samples, row.ratio) for row in session.exec(select(ModelCalibration))]


def test_tool_rounds_each_below_the_gate_teach_nothing(workspace):
    """The 2,000-unit gate applies to each provider request, not to an answer's sum."""
    client, engine, monkeypatch = workspace
    estimates = tool_loop(monkeypatch, 2, lambda index, estimate: counts(estimate + 350))
    events = ask(client, tools=["lookup"])
    assert events[-1]["status"] == "complete", events
    assert len(estimates) == 3 and max(estimates) < MIN_ESTIMATED_UNITS <= sum(estimates)
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    assert report["tool_rounds"] == 2 and report["estimated_input_tokens"] == sum(estimates)
    assert calibration_rows(engine) == []


def test_requests_without_reported_usage_stay_out_of_sample_and_totals(workspace):
    """Only requests that report their input pair an estimate with an actual count."""
    client, engine, monkeypatch = workspace
    estimates = tool_loop(
        monkeypatch,
        1,
        lambda index, estimate: counts(round(estimate * 1.1)) if index == 0 else None,
    )
    events = ask(client, question="请逐条解释这些条件。" + "条件与例外" * 500, tools=["lookup"])
    assert events[-1]["status"] == "complete", events
    assert len(estimates) == 2 and min(estimates) >= MIN_ESTIMATED_UNITS
    ((samples, ratio),) = calibration_rows(engine)
    assert samples == 1 and ratio == pytest.approx(1.1, abs=0.001)
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    # A total that one request did not report is unknown, never a partial sum.
    assert report["estimated_input_tokens"] == sum(estimates)
    assert report["actual_input_tokens"] is None and report["cached_input_tokens"] is None


def openai_stream(monkeypatch, prompt_tokens_for):
    """The real stream_chat over the OpenAI SDK; the endpoint reports prompt_tokens_for(body)."""
    bodies = []

    def frame(payload):
        return "data: " + json.dumps(payload) + "\n\n"

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "synthetic"}
        frames = [
            frame({**chunk, "choices": [{"index": 0, "delta": {"content": "合成回答。"}}]}),
            frame({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            frame(
                {
                    **chunk,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt_tokens_for(body),
                        "completion_tokens": 5,
                        "total_tokens": prompt_tokens_for(body) + 5,
                    },
                }
            ),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(frames) + "data: [DONE]\n\n",
        )

    monkeypatch.setattr(llm, "_STREAM_OPTIONS_UNSUPPORTED", set())
    monkeypatch.setattr(
        llm,
        "_client",
        lambda spec: OpenAI(
            base_url=spec.base_url,
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )
    monkeypatch.setattr(nodes, "stream_chat", llm.stream_chat)
    return bodies


def test_image_questions_never_move_the_learned_ratio(workspace):
    """An image's fixed reservation measures provider image pricing, not the text estimator."""
    client, engine, monkeypatch = workspace
    with Session(engine) as session:
        config = session.get(ModelConfig, 1)
        # Large enough that the image history never compacts: every question streams.
        config.context_window, config.max_tokens = 65536, 4096
        session.add(config)
        session.add(ModelCalibration(config_id=1, samples=3, ratio=1.5))
        session.commit()

    def reported(body):
        system, *messages = body["messages"]
        if isinstance(messages[-1]["content"], list):
            return 1418  # Text plus the provider's own (small) image cost.
        return round(estimate_request_tokens(system["content"], messages) * 1.5)

    bodies = openai_stream(monkeypatch, reported)
    # A text question teaches the ratio over this streaming path.
    assert ask(client, question="解释" + "条件与例外" * 600)[-1]["status"] == "complete"
    ((samples, ratio),) = calibration_rows(engine)
    assert samples == 4 and ratio == pytest.approx(1.5, abs=0.01)
    # Image questions, and later requests resending their images as history, do not.
    image = "data:image/png;base64,iVBORw0KGgo="
    for _ in range(3):
        assert ask(client, question="图里是什么？", images=[image])[-1]["status"] == "complete"
    assert len(bodies) == 4 and calibration_rows(engine) == [(samples, ratio)]


def test_calibration_is_written_only_for_an_existing_model(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orphan.db'}")
    SQLModel.metadata.create_all(engine)
    assert record_usage(engine, 7, 4000, 6000) is None
    with Session(engine) as session:
        assert session.exec(select(ModelCalibration)).all() == []
    engine.dispose()


LATE_MODEL = {
    "label": "Late",
    "base_url": "https://late.invalid/v1",
    "llm_model": "late",
    "api_key": "late-key-never-sent",
    "context_window": 16384,
}


@pytest.mark.parametrize("change", ["delete", "switch"])
def test_an_answer_finishing_after_its_model_changed_leaves_no_ratio_to_inherit(workspace, change):
    """A late answer writes nothing once its model was deleted (SQLite then reuses the id
    for the next model) or switched to another model."""
    client, engine, monkeypatch = workspace
    with Session(engine) as session:
        session.add(ModelConfig(id=3, **LATE_MODEL))
        session.commit()
    started, release = threading.Event(), threading.Event()

    def stream(spec, system, messages, **kwargs):
        yield "text", "开始，"
        started.set()
        assert release.wait(5)
        kwargs["diagnostics"]["rounds"] = [
            {
                "estimated_input_tokens": 5000,
                "input_tokens": 12000,
                "cached_input_tokens": 0,
                "output_tokens": 40,
            }
        ]
        yield "text", "结束。"

    monkeypatch.setattr(nodes, "stream_chat", stream)
    events = []
    worker = threading.Thread(target=lambda: events.extend(ask(client, config_id=3)))
    worker.start()
    try:
        assert started.wait(5)
        if change == "delete":
            assert client.delete("/models/3").json() == {"deleted": 3}
        else:
            other = {**LATE_MODEL, "llm_model": "another-model", "api_key": ""}
            assert client.put("/models/3", json=other).status_code == 200
    finally:
        release.set()
        worker.join(10)
    assert events and events[-1]["status"] == "complete", events
    if change == "delete":
        added = client.post("/models", json={**LATE_MODEL, "label": "Next"}).json()
        assert added["id"] == 3, "SQLite reuses the deleted maximum id"
    with Session(engine) as session:
        assert session.exec(select(ModelCalibration)).all() == []
        assert to_spec(session.get(ModelConfig, 3), session).calibration == 1.0


def test_startup_drops_ratios_whose_model_is_gone(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'orphans.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ModelConfig(
                id=1, label="Kept", base_url="https://k.invalid", llm_model="k", api_key="k"
            )
        )
        session.add(ModelCalibration(config_id=1, samples=2, ratio=1.4))
        session.add(ModelCalibration(config_id=9, samples=1, ratio=3.0))
        session.commit()
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()
    with Session(engine) as session:
        assert [row.config_id for row in session.exec(select(ModelCalibration))] == [1]
    engine.dispose()


@pytest.mark.parametrize(
    "change,kept",
    [
        ({"label": "Renamed", "context_window": 16384, "max_tokens": 2048}, True),
        ({"api_key": "rotated-key-never-sent"}, True),
        ({"base_url": "https://offline.invalid/v1/"}, True),
        ({"llm_model": "another-model"}, False),
        ({"base_url": "https://elsewhere.invalid/v1"}, False),
        ({"protocol": "anthropic"}, False),
    ],
)
def test_editing_a_model_resets_its_ratio_only_when_it_becomes_another_model(
    workspace, change, kept
):
    client, engine, _ = workspace
    assert record_usage(engine, 1, 4000, 8000) == 2.0
    body = {
        "label": "Real",
        "base_url": "https://offline.invalid/v1",
        "llm_model": "synthetic",
        "api_key": "",
        "context_window": 8192,
        "max_tokens": 1024,
        "is_default": True,
        **change,
    }
    assert client.put("/models/1", json=body).status_code == 200
    with Session(engine) as session:
        assert load_calibration(session, 1) == (2.0 if kept else 1.0)


@pytest.mark.parametrize("seeded", [True, False])
def test_concurrent_answers_never_lose_a_calibration_sample(tmp_path, monkeypatch, seeded):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ModelConfig(
                id=1, label="Real", base_url="https://r.invalid", llm_model="m", api_key="k"
            )
        )
        if seeded:
            session.add(
                ModelCalibration(
                    config_id=1, samples=1, ratio=1.0, estimated_total=4000, actual_total=4000
                )
            )
        session.commit()
    # Hold every row read until both writers have read: a read-modify-write in Python
    # then loses one sample (or, on the first insert, fails on the primary key).
    barrier = threading.Barrier(2, timeout=2)
    original_get = Session.get

    def get(self, *args, **kwargs):
        row = original_get(self, *args, **kwargs)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return row

    monkeypatch.setattr(Session, "get", get)
    threads = [
        threading.Thread(target=record_usage, args=(engine, 1, 4000, 4000 * factor))
        for factor in (2, 3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    monkeypatch.setattr(Session, "get", original_get)
    with Session(engine) as session:
        row = session.get(ModelCalibration, 1)
        before = 1 if seeded else 0
        assert row.samples == before + 2
        assert row.estimated_total == 4000 * (before + 2)
        assert row.actual_total == 4000 * before + 8000 + 12000
    engine.dispose()


def test_diagnostics_report_the_largest_request_apart_from_the_sum_over_requests(workspace):
    """Each tool round is a provider request of its own, so an answer's summed input can
    exceed the model window; the report also names the largest single request."""
    client, engine, monkeypatch = workspace
    with Session(engine) as session:
        config = session.get(ModelConfig, 1)
        config.context_window, config.max_tokens = 65536, 4096
        session.add(config)
        session.commit()

    def agent(spec, system, messages, tools, execute, **kwargs):
        kwargs["diagnostics"]["rounds"] = [
            {
                "estimated_input_tokens": estimated,
                "input_tokens": actual,
                "cached_input_tokens": cached,
                "output_tokens": 30,
            }
            for estimated, actual, cached in [
                (29000, 28700, 0),
                (31000, 30900, 28000),
                (30000, 29800, 30000),
            ]
        ]
        kwargs["diagnostics"]["tool_rounds"] = 2
        yield {"type": "delta", "text": "合成回答。"}

    monkeypatch.setattr(nodes, "run_agent", agent)
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args, **kwargs: ([LOOKUP], {}))
    events = ask(client, tools=["lookup"])
    assert events[-1]["status"] == "complete", events
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    assert report["estimated_input_tokens"] == 90000 and report["actual_input_tokens"] == 89400
    assert report["requests"] == 3 and report["tool_rounds"] == 2
    assert report["largest_request"] == {
        "estimated_input_tokens": 31000,
        "actual_input_tokens": 30900,
        "cached_input_tokens": 28000,
    }
    assert report["largest_request"]["actual_input_tokens"] < 65536 < report["actual_input_tokens"]
    assert all(isinstance(value, (int, float, bool, type(None))) for value in leaves(report))


def test_diagnostics_count_the_memories_the_answer_was_given(workspace):
    """Decision F: the stored numbers include how many memory records reached the answer."""
    client, _, monkeypatch = workspace

    def source(memory_id):
        return {
            "memory_id": memory_id,
            "source_node_id": 1,
            "tree_id": 1,
            "tree_title": "校准",
            "scope": "path",
        }

    note = MemoryNote(
        "以下是历史学习材料：\n【当前学习路径的相关记忆】\n"
        "- [记忆 7；来源节点 1] 需要授权。\n- [记忆 8；来源节点 1] 先备份。",
        # Record 9 was retrieved but is not in the note, so the answer never saw it.
        [source(7), source(8), source(9)],
    )
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: note)
    streaming_double(monkeypatch, estimated=3000, actual=2600)
    events = ask(client)
    report = next(event["diagnostics"] for event in events if "diagnostics" in event)
    sources = next(event["sources"] for event in events if "sources" in event)
    assert report["memory_count"] == len(sources) == 2
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    events = ask(client)
    assert next(e["diagnostics"] for e in events if "diagnostics" in e)["memory_count"] == 0


def test_a_stopped_attempt_keeps_its_diagnostics_when_a_retry_starts_first(workspace):
    """Stop ends the viewers at once and persists the partial answer; the producer reports
    its diagnostics only when its stream returns. A retry may start in between, and the
    stopped attempt still keeps its numbers."""
    client, engine, monkeypatch = workspace
    produced, release = threading.Event(), threading.Event()

    def slow(spec, system, messages, **kwargs):
        yield "text", "部分"
        produced.set()
        release.wait(5)
        yield "text", "不应追加"

    monkeypatch.setattr(nodes, "stream_chat", slow)
    result = []
    worker = threading.Thread(target=lambda: result.extend(ask(client, request_id="slow")))
    worker.start()
    try:
        assert produced.wait(5)
        generation = next(g for g in nodes._GENERATIONS.values() if g.request_id == "slow")
        stopped = client.post("/nodes/1/stop", json={"request_id": "slow"}).json()
        worker.join(5)
        assert result[-1]["status"] == "interrupted"
        assert not any("diagnostics" in event for event in result)
        streaming_double(monkeypatch, estimated=3000, actual=2600)
        retried = ask(client, stopped["node_id"], mode="retry", request_id="fresh-retry")
        assert retried[-1]["status"] == "complete", retried
        cursor = changes.feed.since("")["cursor"]
    finally:
        release.set()
    assert generation.finished.wait(5)
    # Open pages, the stopping one included, are told to reload the node's stored answers.
    assert stopped["node_id"] in changes.feed.since(cursor)["trees"]["1"]
    with Session(engine) as session:
        first, second = session.exec(
            select(Message)
            .where(Message.node_id == stopped["node_id"], Message.role == "assistant")
            .order_by(Message.id)
        ).all()
    assert first.status == "interrupted" and first.content == "部分"
    assert first.diagnostics is not None and first.diagnostics["actual_input_tokens"] is None
    assert set(first.diagnostics["timings_ms"]) == {
        "retrieval",
        "tools",
        "summary",
        "approval",
        "model",
        "total",
    }
    assert second.status == "complete" and second.diagnostics["actual_input_tokens"] == 2600
