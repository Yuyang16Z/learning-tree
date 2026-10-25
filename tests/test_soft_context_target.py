"""A per-model soft request target keeps large windows from becoming large bills.

The hard budget (window minus answer reserve and headroom) still decides what a request
may carry; the target only decides when older history starts to be compacted.
"""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app import db
from app.context import build_context
from app.context_budget import AUTO_TARGET_TOKENS, ContextPolicy
from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.service import to_spec

WINDOW = 1_048_576


def test_automatic_target_caps_compaction_for_large_windows_only():
    large = ContextPolicy(WINDOW, 4096)
    assert large.target_budget == AUTO_TARGET_TOKENS == 131_072
    assert large.compaction_budget == AUTO_TARGET_TOKENS
    # The hard budget is untouched: a request may still carry up to it.
    assert large.input_budget == WINDOW - 4096 - WINDOW // 20
    small = ContextPolicy(32_768, 4096)
    assert small.target_budget == small.input_budget
    assert small.compaction_budget == int(small.input_budget * 0.6)


def test_explicit_target_wins_and_a_target_at_the_window_removes_the_cap():
    assert ContextPolicy(WINDOW, 4096, target_tokens=65_536).compaction_budget == 65_536
    uncapped = ContextPolicy(WINDOW, 4096, target_tokens=WINDOW)
    assert uncapped.target_budget == uncapped.input_budget
    assert uncapped.compaction_budget == int(uncapped.input_budget * 0.6)


def test_target_follows_calibration_like_the_hard_budget():
    calibrated = ContextPolicy(WINDOW, 4096, calibration=2.0)
    assert calibrated.target_budget == AUTO_TARGET_TOKENS // 2
    # A ratio below 1 never enlarges either budget.
    assert ContextPolicy(WINDOW, 4096, calibration=0.5).target_budget == AUTO_TARGET_TOKENS


def test_effective_target_reports_tokens_capped_at_the_window():
    assert ContextPolicy(WINDOW, 4096).effective_target_tokens == AUTO_TARGET_TOKENS
    assert ContextPolicy(32_768, 4096).effective_target_tokens == 32_768
    assert ContextPolicy(WINDOW, 4096, target_tokens=65_536).effective_target_tokens == 65_536


def test_tool_schema_allowance_scales_with_the_target_not_the_window():
    assert ContextPolicy(WINDOW, 4096).schema_allowance == AUTO_TARGET_TOKENS // 3
    small = ContextPolicy(32_768, 4096)
    assert small.schema_allowance == small.input_budget // 3


def _path(count: int, chars: int):
    """A synthetic learning path; each answer holds `chars` Chinese characters."""
    sources = []
    for index in range(1, count + 1):
        node = Node(
            id=index,
            tree_id=1,
            parent_id=None if index == 1 else index - 1,
            title=f"节点{index}",
            status="complete",
        )
        messages = [
            Message(
                id=2 * index - 1,
                node_id=index,
                role="user",
                content=f"第{index}个问题",
                status="complete",
            ),
            Message(
                id=2 * index,
                node_id=index,
                role="assistant",
                content="历史背景" * (chars // 4),
                status="complete",
            ),
        ]
        sources.append((node, messages))
    return sources[:-1], sources[-1][0], sources[-1][1]


def test_long_history_is_compacted_at_the_target_on_a_large_window():
    ancestors, current, messages = _path(10, 25_000)
    stats = {}
    build_context(
        ancestors,
        current,
        messages,
        "现在的问题",
        policy=ContextPolicy(WINDOW, 4096),
        diagnostics=stats,
    )
    assert stats["raw_estimate"] > AUTO_TARGET_TOKENS, "the fixture must exceed the target"
    assert stats["raw_estimate"] < int(ContextPolicy(WINDOW, 4096).input_budget * 0.6)
    assert stats["compacted"] is True
    assert stats["estimated_input_tokens"] <= AUTO_TARGET_TOKENS


def test_the_same_history_stays_whole_when_the_target_is_the_window():
    ancestors, current, messages = _path(10, 25_000)
    stats = {}
    build_context(
        ancestors,
        current,
        messages,
        "现在的问题",
        policy=ContextPolicy(WINDOW, 4096, target_tokens=WINDOW),
        diagnostics=stats,
    )
    assert stats["compacted"] is False


def test_tool_reserves_are_taken_from_the_target_room():
    ancestors, current, messages = _path(10, 25_000)
    policy = ContextPolicy(WINDOW, 4096)
    tool = {
        "type": "function",
        "function": {"name": "lookup", "description": "Look up.", "parameters": {"type": "object"}},
    }
    stats = {}
    build_context(
        ancestors,
        current,
        messages,
        "现在的问题",
        policy=policy,
        tool_defs=[tool],
        tool_schema_budget=policy.schema_allowance,
        diagnostics=stats,
    )
    assert stats["compacted"] is True
    # The initial request leaves the tool-result and schema reserves free inside the target.
    assert stats["estimated_input_tokens"] <= policy.target_budget - policy.tool_reserve


@pytest.fixture
def legacy_models(tmp_path, monkeypatch):
    """A database from before the target existed: its models keep the automatic target."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-target.db'}", connect_args={"check_same_thread": False}
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
                "INSERT INTO modelconfig VALUES (81, 'Synthetic model', 'https://mock.invalid', "
                "'mock', 'synthetic-key-417', 'openai', 4096, 1048576, 1, '2026-01-01')"
            )
        )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()
    yield engine
    engine.dispose()


def test_migration_adds_an_empty_target_and_specs_carry_it(legacy_models):
    with Session(legacy_models) as session:
        config = session.get(ModelConfig, 81)
        assert config.target_input_tokens is None
        assert to_spec(config).target_input_tokens is None
        assert ContextPolicy.from_spec(to_spec(config)).compaction_budget == AUTO_TARGET_TOKENS
        config.target_input_tokens = 65_536
        session.add(config)
        session.commit()
        spec = to_spec(session.get(ModelConfig, 81))
        assert spec.target_input_tokens == 65_536
        assert ContextPolicy.from_spec(spec).compaction_budget == 65_536


def _client(engine):
    from app.main import app

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db.get_session] = session_override
    return app, TestClient(app)


def test_model_api_stores_validates_and_returns_the_target(legacy_models):
    app, api = _client(legacy_models)
    try:
        listed = api.get("/models").json()
        assert listed[0]["target_input_tokens"] is None
        payload = {
            "label": "Synthetic target",
            "base_url": "https://mock.invalid/v1",
            "llm_model": "mock",
            "api_key": "synthetic-test-value",
            "protocol": "openai",
            "max_tokens": 4096,
            "context_window": WINDOW,
            "target_input_tokens": 65_536,
        }
        created = api.post("/models", json=payload)
        assert created.status_code == 200, created.text
        identifier = created.json()["id"]
        assert created.json()["target_input_tokens"] == 65_536
        payload.update(target_input_tokens=None, api_key="")
        cleared = api.put(f"/models/{identifier}", json=payload)
        assert cleared.status_code == 200 and cleared.json()["target_input_tokens"] is None
        for invalid in (1_000, 16_383, WINDOW + 1):
            payload["target_input_tokens"] = invalid
            assert api.put(f"/models/{identifier}", json=payload).status_code == 422, invalid
        payload["target_input_tokens"] = 16_384
        assert api.put(f"/models/{identifier}", json=payload).status_code == 200
        with Session(legacy_models) as session:
            stored = session.exec(select(ModelConfig).where(ModelConfig.id == identifier)).one()
            assert stored.target_input_tokens == 16_384
            assert stored.api_key == "synthetic-test-value"
    finally:
        app.dependency_overrides.clear()


def test_answer_diagnostics_report_the_effective_target(tmp_path, monkeypatch):
    from app import main, service
    from app.routers import nodes

    engine = create_engine(
        f"sqlite:///{tmp_path / 'target-diagnostics.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (db, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="主题"))
        session.add(Node(id=1, tree_id=1, title="根", status="idle"))
        session.add(
            ModelConfig(
                label="Mock",
                base_url="mock",
                llm_model="mock",
                api_key="mock",
                context_window=WINDOW,
                target_input_tokens=65_536,
                is_default=True,
            )
        )
        session.commit()
    app, api = _client(engine)
    try:
        response = api.post("/nodes/1/ask", json={"question": "问题"})
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1].get("done"), events[-1]
        report = next(event["diagnostics"] for event in events if "diagnostics" in event)
        assert report["target_input_tokens"] == 65_536
    finally:
        app.dependency_overrides.clear()
        nodes._GENERATIONS.clear()
        engine.dispose()
