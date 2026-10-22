"""Answers asked with a learning style: stored per question, sent last in the system prompt."""

import copy
import json
import os
import time

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.service as service
from app import learning_presets as presets
from app import llm
from app.context import BASE_SYSTEM, _full_system, build_context
from app.context_budget import (
    SYSTEM_OMISSION,
    ContextBudgetExceeded,
    ContextPolicy,
    estimate_request_tokens,
    fit_request,
)
from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes, trees

TOOL = {
    "type": "function",
    "function": {
        "name": "synthetic_tool",
        "description": "A synthetic tool that is never executed.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def section(identifier):
    return presets.preset_section(presets.preset_catalog()[identifier])


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'learning-styles.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    calls = {name: [] for name in ("stream", "agent", "explain", "extract")}

    def stream(spec, system, messages, **kwargs):
        calls["stream"].append({"system": system, "messages": copy.deepcopy(messages), **kwargs})
        if "SYNTHETIC-FAILURE" in json.dumps(messages, ensure_ascii=False):
            raise RuntimeError("synthetic provider failure")
        yield "text", "合成回答。"

    def agent(spec, system, messages, tools, execute, **kwargs):
        calls["agent"].append({"system": system, "tools": copy.deepcopy(tools), **kwargs})
        yield {"type": "delta", "text": "合成工具回答。"}

    def complete(spec, system, messages):
        calls["explain"].append(system)
        return "合成释义。"

    monkeypatch.setattr(nodes, "stream_chat", stream)
    monkeypatch.setattr(nodes, "run_agent", agent)
    monkeypatch.setattr(nodes, "complete", complete)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        nodes, "assemble_tools", lambda session, requested: ([TOOL] if requested else [], {})
    )
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args: calls["extract"].append(args))
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    nodes._GENERATIONS.clear()
    with Session(engine) as session:
        session.add_all(
            [KnowledgeTree(id=1, title="学习方式主题"), KnowledgeTree(id=2, title="另一个")]
        )
        session.add_all(
            [Node(id=1, tree_id=1, title="根"), Node(id=2, tree_id=2, title="另一个根")]
        )
        session.add_all(
            [
                ModelConfig(
                    id=1,
                    label="Offline test",
                    base_url="https://offline.invalid/v1",
                    llm_model="synthetic",
                    api_key="synthetic-key-never-sent",
                    is_default=True,
                ),
                ModelConfig(id=2, label="演示", base_url="mock", llm_model="mock", api_key="mock"),
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
    engine.dispose()


def ask(client, node_id, question, **body):
    response = client.post(f"/nodes/{node_id}/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    return events[-1]


def stored(engine, node_id):
    with Session(engine) as session:
        return session.get(Node, node_id).learning_preset


def set_topic_default(engine, value, tree_id=1):
    with Session(engine) as session:
        topic = session.get(KnowledgeTree, tree_id)
        topic.learning_preset = value
        session.add(topic)
        session.commit()


def test_a_chosen_style_is_stored_listed_and_closes_the_system_prompt(api):
    client, engine, calls = api
    outcome = ask(client, 1, "什么是递归？", learning_preset="socratic")
    assert outcome["done"] and outcome["node_id"] == 1
    assert stored(engine, 1) == "socratic"
    system = calls["stream"][-1]["system"]
    assert system.startswith(BASE_SYSTEM) and system.endswith(section("socratic"))
    # The cached stable head is unchanged by the style, so prompt caching still matches.
    prefix = calls["stream"][-1]["cache_prefix"]
    assert system.startswith(prefix) and "学习方式：" not in prefix
    assert client.get("/nodes/1").json()["learning_preset"] == "socratic"
    assert [row["learning_preset"] for row in client.get("/nodes/1/thread").json()["nodes"]] == [
        "socratic"
    ]
    assert {node["id"]: node["learning_preset"] for node in client.get("/trees/1").json()} == {
        1: "socratic"
    }

    follow_up = ask(client, 1, "那尾递归呢？", learning_preset=None)["node_id"]
    assert stored(engine, follow_up) is None
    assert "学习方式：" not in calls["stream"][-1]["system"]
    rows = client.get(f"/nodes/{follow_up}/thread").json()["nodes"]
    assert [row["learning_preset"] for row in rows] == ["socratic", None]


def test_the_style_belongs_to_the_protected_core_of_tool_rounds(api):
    client, engine, calls = api
    ask(client, 1, "对比两种排序", learning_preset="compare", tools=["synthetic_tool"])
    request = calls["agent"][-1]
    assert request["system"].endswith(section("compare"))
    # Fitting may replace optional context with this core; the style survives it.
    assert request["protected_system"].endswith(section("compare"))
    assert "学习方式：" not in request["cache_prefix"]


def test_an_absent_field_takes_the_topic_default_and_null_means_default(api):
    client, engine, calls = api
    set_topic_default(engine, "worked-examples")
    first = ask(client, 1, "怎么求导？")["node_id"]
    assert stored(engine, first) == "worked-examples"
    assert calls["stream"][-1]["system"].endswith(section("worked-examples"))
    explicit_default = ask(client, first, "直接讲讲链式法则", learning_preset=None)["node_id"]
    assert stored(engine, explicit_default) is None
    assert "学习方式：" not in calls["stream"][-1]["system"]
    chosen = ask(client, explicit_default, "考考我", learning_preset="self-test")["node_id"]
    assert stored(engine, chosen) == "self-test"
    again = ask(client, chosen, "再来一题")["node_id"]
    assert stored(engine, again) == "worked-examples", "absent means the topic default again"
    # Another topic keeps its own default.
    assert stored(engine, ask(client, 2, "另一个主题的问题")["node_id"]) is None


def test_an_unknown_style_is_rejected_before_anything_is_stored(api):
    client, engine, calls = api
    with Session(engine) as session:
        before = (len(session.exec(select(Node)).all()), len(session.exec(select(Message)).all()))
    for value in ("no-such-style", "custom:missing", "Socratic", "custom:socratic"):
        response = client.post("/nodes/1/ask", json={"question": "问题", "learning_preset": value})
        assert response.status_code == 422
        assert response.json()["detail"] == presets.UNKNOWN_PRESET
    too_long = client.post("/nodes/1/ask", json={"question": "问题", "learning_preset": "x" * 81})
    assert too_long.status_code == 422
    with Session(engine) as session:
        after = (len(session.exec(select(Node)).all()), len(session.exec(select(Message)).all()))
        assert session.get(Node, 1).status == "idle"
    assert after == before
    assert calls["stream"] == []


def test_retry_reuses_the_stored_style_and_ignores_the_body(api):
    client, engine, calls = api
    failed = ask(client, 1, "SYNTHETIC-FAILURE 费曼一下", learning_preset="feynman")
    assert failed["status"] == "error"
    for body in ({"learning_preset": "socratic"}, {"learning_preset": "no-such-style"}, {}):
        retried = ask(client, 1, "", mode="retry", **body)
        assert retried["status"] == "error"
        assert calls["stream"][-1]["system"].endswith(section("feynman"))
        assert stored(engine, 1) == "feynman"


def test_revise_copies_the_original_style_or_takes_the_chosen_one(api):
    client, engine, calls = api
    assert ask(client, 1, "解释闭包", learning_preset="compare")["done"]
    copied = ask(client, 1, "解释 JavaScript 闭包", mode="revise")["node_id"]
    assert copied != 1 and stored(engine, copied) == "compare"
    assert calls["stream"][-1]["system"].endswith(section("compare"))
    chosen = ask(client, 1, "用例题解释闭包", mode="revise", learning_preset="worked-examples")
    assert stored(engine, chosen["node_id"]) == "worked-examples"
    cleared = ask(client, 1, "简单解释闭包", mode="revise", learning_preset=None)
    assert stored(engine, cleared["node_id"]) is None
    assert "学习方式：" not in calls["stream"][-1]["system"]
    assert stored(engine, 1) == "compare", "the original question keeps its style"


def test_only_the_last_question_of_a_node_carries_its_style(api):
    """A legacy node holds several questions; revising its second one copies the first
    into the revision, which was never asked with the revision's style."""
    client, engine, _ = api
    with Session(engine) as session:
        for role, content in (
            ("user", "旧问一"),
            ("assistant", "旧答一"),
            ("user", "旧问二"),
            ("assistant", "旧答二"),
        ):
            session.add(Message(node_id=1, role=role, content=content, status="complete"))
        root = session.get(Node, 1)
        root.status = "complete"
        session.add(root)
        session.commit()
        second = session.exec(
            select(Message).where(Message.node_id == 1, Message.content == "旧问二")
        ).one()
    rows = client.get("/nodes/1/thread").json()["nodes"]
    assert [(row["question"], row["learning_preset"]) for row in rows] == [
        ("旧问一", None),
        ("旧问二", None),
    ]
    revision = ask(
        client,
        1,
        "新问二",
        mode="revise",
        question_message_id=second.id,
        learning_preset="socratic",
    )["node_id"]
    assert stored(engine, revision) == "socratic"
    rows = client.get(f"/nodes/{revision}/thread").json()["nodes"]
    assert [(row["question"], row["learning_preset"]) for row in rows] == [
        ("旧问一", None),
        ("新问二", "socratic"),
    ]
    assert client.get(f"/nodes/{revision}").json()["learning_preset"] == "socratic"


def test_inline_explanations_and_memory_extraction_never_use_styles(api):
    client, engine, calls = api
    set_topic_default(engine, "socratic")
    assert ask(client, 1, "什么是熵？", learning_preset="socratic")["done"]
    with Session(engine) as session:
        answer = session.exec(
            select(Message).where(Message.node_id == 1, Message.role == "assistant")
        ).one()
    response = client.post(
        "/nodes/1/explain", json={"text": "合成回答", "source_message_id": answer.id}
    )
    assert response.status_code == 200
    assert calls["explain"] and all("学习方式：" not in system for system in calls["explain"])
    for _ in range(100):
        if calls["extract"]:
            break
        time.sleep(0.02)
    # Extraction keeps its five positional arguments and sees the question itself.
    (_, tree_id, node_id, question, answer_text) = calls["extract"][-1]
    assert (tree_id, node_id, question, answer_text) == (1, 1, "什么是熵？", "合成回答。")


def test_demo_answers_store_the_style_like_any_other(api, monkeypatch):
    client, engine, _ = api
    monkeypatch.setattr(nodes, "stream_chat", llm.stream_chat)
    outcome = ask(client, 1, "演示问题", config_id=2, learning_preset="self-test")
    assert outcome["done"]
    assert stored(engine, 1) == "self-test"
    with Session(engine) as session:
        answer = session.exec(select(Message).where(Message.role == "assistant")).one()
    assert answer.content == llm.MOCK_ANSWER
    unknown = client.post(
        "/nodes/1/ask",
        json={"question": "演示问题", "config_id": 2, "learning_preset": "no-such-style"},
    )
    assert unknown.status_code == 422 and unknown.json()["detail"] == presets.UNKNOWN_PRESET


def test_a_deleted_custom_style_is_kept_as_history_and_answers_as_default(api):
    client, engine, calls = api
    directory = presets.CUSTOM_DIRECTORY
    directory.mkdir()
    custom = directory / "analogy.md"
    custom.write_text(
        "---\ntitle_zh: 类比讲解\ntitle_en: Analogies\n---\nSYNTHETIC-ANALOGY-INSTRUCTION\n",
        encoding="utf-8",
    )
    set_topic_default(engine, "custom:analogy")
    assert ask(client, 1, "SYNTHETIC-FAILURE 类比一下")["status"] == "error"
    assert stored(engine, 1) == "custom:analogy"
    assert calls["stream"][-1]["system"].endswith(section("custom:analogy"))

    custom.unlink()
    retried = ask(client, 1, "", mode="retry")
    assert retried["status"] == "error"
    assert "SYNTHETIC-ANALOGY-INSTRUCTION" not in calls["stream"][-1]["system"]
    assert stored(engine, 1) == "custom:analogy", "the stored id stays as history"
    assert client.get("/nodes/1/thread").json()["nodes"][0]["learning_preset"] == "custom:analogy"

    follow_up = ask(client, 1, "换个问题")["node_id"]
    assert stored(engine, follow_up) is None, "the missing topic default answers as default"
    assert "学习方式：" not in calls["stream"][-1]["system"]
    explicit = client.post(
        f"/nodes/{follow_up}/ask", json={"question": "再问", "learning_preset": "custom:analogy"}
    )
    assert explicit.status_code == 422 and explicit.json()["detail"] == presets.UNKNOWN_PRESET


def test_the_style_is_counted_in_budgets_and_kept_through_compaction():
    preset = presets.preset_catalog()["mistake-review"]
    style = presets.preset_section(preset)
    current = Node(id=2, tree_id=1, parent_id=1, title="当前", seed_text="引用")
    root = Node(id=1, tree_id=1, title="根")
    history = [
        Message(node_id=1, role="user", content="早期问题"),
        Message(node_id=1, role="assistant", content="早期背景。" * 6000),
    ]
    stats = {}
    system, messages = build_context(
        [(root, history)],
        current,
        [],
        "追问",
        policy=ContextPolicy(8192, 1024),
        diagnostics=stats,
        preset=preset,
    )
    assert stats["compacted"] is True
    assert system.endswith(style), "the style stays the last section after compaction"
    assert stats["estimated_input_tokens"] == estimate_request_tokens(system, messages)
    assert stats["protected_system"] == _full_system([], current, "", preset=preset)
    assert stats["protected_system"].endswith(style)
    assert stats["stable_system"] == BASE_SYSTEM

    plain = {}
    build_context([], current, [], "追问", diagnostics=plain)
    styled = {}
    build_context([], current, [], "追问", diagnostics=styled, preset=preset)
    assert styled["estimated_input_tokens"] > plain["estimated_input_tokens"]

    # Request fitting may drop optional context for the protected core; the style is in it.
    optional = stats["protected_system"] + "\n" + "可选背景。" * 4000
    fitted = fit_request(
        optional,
        [{"role": "user", "content": "追问"}],
        policy=ContextPolicy(8192, 1024),
        protected_system=stats["protected_system"],
    )
    assert fitted.system == stats["protected_system"] + SYSTEM_OMISSION


def test_a_style_that_cannot_fit_makes_the_mandatory_input_too_large():
    long_style = presets.LearningPreset(
        id="custom:long",
        source="custom",
        title_zh="长",
        title_en="Long",
        description_zh="",
        description_en="",
        instruction="教" * 4000,
    )
    current = Node(id=1, tree_id=1, title="当前")
    policy = ContextPolicy(8192, 4096)
    build_context([], current, [], "问" * 200, policy=policy)  # Fits without the style.
    with pytest.raises(ContextBudgetExceeded) as raised:
        build_context([], current, [], "问" * 200, policy=policy, preset=long_style)
    # The style is the cause, so the message names it: a retry keeps the style and would
    # fail the same way, so shortening the question would not help.
    assert str(raised.value) == presets.STYLE_TOO_LONG
    # Selected tools reserve room too; the style is still what does not fit.
    with pytest.raises(ContextBudgetExceeded) as raised:
        build_context(
            [], current, [], "问" * 200, policy=policy, tool_defs=[TOOL], preset=long_style
        )
    assert str(raised.value) == presets.STYLE_TOO_LONG
    # A question too large on its own keeps the general message, style or not.
    general = ContextBudgetExceeded().args[0]
    for preset in (None, long_style):
        with pytest.raises(ContextBudgetExceeded) as raised:
            build_context([], current, [], "问" * 6000, policy=policy, preset=preset)
        assert str(raised.value) == general != presets.STYLE_TOO_LONG


def test_an_answer_that_fails_on_its_long_style_says_so_and_retry_keeps_it(api):
    client, engine, calls = api
    directory = presets.CUSTOM_DIRECTORY
    directory.mkdir()
    (directory / "long.md").write_text(
        "---\ntitle_zh: 很长的方式\ntitle_en: Long style\n---\n" + "请用类比讲解。" * 571 + "\n",
        encoding="utf-8",
    )
    assert presets.list_presets()[1] == [], "the style is within the file limits"
    with Session(engine) as session:
        session.add(
            ModelConfig(
                id=3,
                label="Small window",
                base_url="https://offline.invalid/v1",
                llm_model="synthetic-small",
                api_key="synthetic-key-never-sent",
                context_window=8192,
                max_tokens=4096,
            )
        )
        session.commit()
    failed = ask(client, 1, "什么是类比？", config_id=3, learning_preset="custom:long")
    assert failed["status"] == "error"
    assert client.get("/nodes/1").json()["error"] == presets.STYLE_TOO_LONG
    assert calls["stream"] == [], "it fails before any model call"
    retried = ask(client, 1, "", mode="retry", config_id=3, learning_preset=None)
    assert retried["status"] == "error"
    assert client.get("/nodes/1").json()["error"] == presets.STYLE_TOO_LONG
    assert stored(engine, 1) == "custom:long"
    # The default model's larger window has room for it.
    assert ask(client, 1, "", mode="retry", config_id=1)["done"]


def test_legacy_schema_gains_nullable_style_columns(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledgetree (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE node (id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL, "
                "parent_id INTEGER, title TEXT NOT NULL, seed_text TEXT, summary TEXT, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO knowledgetree VALUES (1, '旧主题', '2026-01-01')"))
        connection.execute(
            text(
                "INSERT INTO node (id, tree_id, title, created_at) "
                "VALUES (1, 1, '旧根', '2026-01-01')"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    for table in ("knowledgetree", "node"):
        column = next(
            c for c in inspect(engine).get_columns(table) if c["name"] == "learning_preset"
        )
        assert column["nullable"] and column["default"] is None
    with Session(engine) as session:
        assert session.get(KnowledgeTree, 1).learning_preset is None
        assert session.get(Node, 1).learning_preset is None
    database.init_db()
    engine.dispose()


def test_a_short_style_beside_a_near_limit_question_keeps_the_general_message():
    """A small style that only tips a near-limit question over the budget is not the
    cause: shortening the question is the way out, so the general message stays."""
    short_style = presets.LearningPreset(
        id="custom:short",
        source="custom",
        title_zh="短",
        title_en="Short",
        description_zh="",
        description_en="",
        instruction="先直接回答，再举一个例子。",
    )
    current = Node(id=1, tree_id=1, title="当前")
    policy = ContextPolicy(8192, 4096)

    def fits(length, preset=None):
        try:
            build_context([], current, [], "问" * length, policy=policy, preset=preset)
        except ContextBudgetExceeded:
            return False
        return True

    low, high = 1, 20000
    while low < high:  # The longest question that fits without a style.
        middle = (low + high + 1) // 2
        low, high = (middle, high) if fits(middle) else (low, middle - 1)
    assert not fits(low, short_style), "the short style tips this question over the budget"
    with pytest.raises(ContextBudgetExceeded) as raised:
        build_context([], current, [], "问" * low, policy=policy, preset=short_style)
    assert str(raised.value) == ContextBudgetExceeded().args[0]
