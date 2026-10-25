"""A numbers-only trace of each provider round of an answer. Offline doubles only."""

import json
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.llm as llm
import app.service as service
from app.answer_trace import answer_report
from app.models import KnowledgeTree, ModelConfig, Node
from app.routers import nodes

ROUNDS = [
    {
        "estimated_input_tokens": 12000,
        "input_tokens": 11000,
        "cached_input_tokens": 9000,
        "output_tokens": 300,
        "has_images": False,
        "compressed": False,
        "tool_calls": 2,
        "tool_ms": 1250,
    },
    {
        "estimated_input_tokens": 15000,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": 420,
        "has_images": False,
        "compressed": True,
    },
]


def report(**overrides):
    values = dict(
        usage={"rounds": ROUNDS, "tool_rounds": 1},
        context_stats={"compacted": True, "summary_blocks": 2, "estimated_input_tokens": 12000},
        memory_count=3,
        calibration=1.25,
        target_tokens=131072,
        timings={"retrieval": 0.1, "tools": 1.25, "summary": 0.02, "approval": 4.0},
        model_started=100.0,
        started_at=99.0,
        now=110.0,
    )
    values.update(overrides)
    return answer_report(**values)


def test_the_report_traces_every_provider_round():
    result = report()
    assert result["rounds"] == [
        {
            "estimated_input_tokens": 12000,
            "actual_input_tokens": 11000,
            "cached_input_tokens": 9000,
            "output_tokens": 300,
            "trimmed": False,
            "tool_calls": 2,
            "tool_ms": 1250,
        },
        {
            "estimated_input_tokens": 15000,
            "actual_input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": 420,
            "trimmed": True,
            "tool_calls": 0,
            "tool_ms": 0,
        },
    ]
    # The totals keep their meaning: unknown when any request left a count out.
    assert result["estimated_input_tokens"] == 27000 and result["actual_input_tokens"] is None
    assert result["output_tokens"] == 720 and result["requests"] == 2
    assert result["largest_request"] == {
        "estimated_input_tokens": 15000,
        "actual_input_tokens": None,
        "cached_input_tokens": None,
    }
    assert (result["compacted"], result["summary_blocks"], result["memory_count"]) == (True, 2, 3)
    assert result["calibration_ratio"] == 1.25 and result["target_input_tokens"] == 131072
    timings = result["timings_ms"]
    assert timings["approval"] == 4000 and timings["tools"] == 1250
    # Model time is what is left after tools and approval waits.
    assert timings["model"] == 10000 - 1250 - 4000 and timings["total"] == 11000


def test_a_report_without_provider_rounds_falls_back_to_the_assembly_estimate():
    result = report(usage={})
    assert result["rounds"] == [] and result["requests"] == 0
    assert result["estimated_input_tokens"] == 12000 and result["largest_request"] is None
    assert result["tool_rounds"] == 0 and result["tool_budget_stop"] is None


def test_the_agent_records_trimming_tool_calls_and_tool_time_per_round(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(llm, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    responses = [
        {
            "content": None,
            "tool_calls": [
                {"id": "call_1", "name": "lookup", "args": {}},
                {"id": "call_2", "name": "lookup", "args": {}},
            ],
            "usage": {"input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 40},
        },
        {"content": "完成", "tool_calls": [], "usage": None},
    ]
    calls = []

    def chat_once(spec, conversation, tools):
        calls.append(tools)
        return responses[len(calls) - 1]

    monkeypatch.setattr(llm, "_chat_once", chat_once)
    waited = {"seconds": 0.0}

    def execute(name, args):
        clock["now"] += 2.0  # Each call: 1.5 s waiting for approval, 0.5 s running.
        waited["seconds"] += 1.5
        return "result"

    spec = llm.LLMSpec("Offline", "https://offline.invalid/v1", "synthetic", "synthetic-key")
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    diagnostics: dict = {}
    list(
        llm.run_agent(
            spec,
            "system",
            [{"role": "user", "content": "q"}],
            [tool],
            execute,
            diagnostics=diagnostics,
            waited=lambda: waited["seconds"],
        )
    )
    first, final = diagnostics["rounds"]
    assert first["tool_calls"] == 2 and first["tool_ms"] == 1000, "approval waits excluded"
    assert first["compressed"] is False and first["input_tokens"] == 900
    assert "tool_calls" not in final and final["compressed"] is False


@pytest.fixture
def ask_api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'trace.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)

    def stream(spec, system, messages, diagnostics=None, **kwargs):
        diagnostics.setdefault("rounds", []).extend(json.loads(json.dumps(ROUNDS[:1])))
        yield "text", "合成回答。"

    monkeypatch.setattr(nodes, "stream_chat", stream)
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
        yield client
    nodes._GENERATIONS.clear()
    engine.dispose()


def test_answer_diagnostics_carry_the_round_trace(ask_api):
    response = ask_api.post("/nodes/1/ask", json={"question": "问题"})
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[-1]["status"] == "complete", events
    diagnostics = next(event["diagnostics"] for event in events if "diagnostics" in event)
    assert diagnostics["rounds"] == [
        {
            "estimated_input_tokens": 12000,
            "actual_input_tokens": 11000,
            "cached_input_tokens": 9000,
            "output_tokens": 300,
            "trimmed": False,
            "tool_calls": 2,
            "tool_ms": 1250,
        }
    ]
