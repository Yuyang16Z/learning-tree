"""Integration tests use only a temporary SQLite database and deterministic models."""

import json
import os
import threading

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.main as main
import app.service as service
from app.context import build_context
from app.llm import LLMSpec, run_agent
from app.models import KnowledgeTree, Memory, Message, ModelConfig, Node
from app.routers import nodes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)
    nodes._GENERATIONS.clear()

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add(
            ModelConfig(
                label="演示", base_url="mock", llm_model="mock", api_key="mock", is_default=True
            )
        )
        session.commit()
    client = TestClient(main.app)
    yield client, engine
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    main.app.dependency_overrides.clear()
    engine.dispose()


def tree(client):
    response = client.post("/trees", json={"title": "测试学习"})
    assert response.status_code == 200, response.text
    return response.json()


def events(response):
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def ask(client, node, question="问题", **kwargs):
    return events(client.post(f"/nodes/{node}/ask", json={"question": question, **kwargs}))


def test_revision_keeps_original_subtree(setup):
    client, engine = setup
    root = tree(client)["root_node_id"]
    ask(client, root)
    child = ask(client, root, "后续")[-1]["node_id"]
    revision = ask(client, root, "修改问题", mode="revise")[-1]["node_id"]
    with Session(engine) as session:
        assert session.get(Node, child).parent_id == root
        assert session.get(Node, revision).parent_id is None
        assert session.get(Node, revision).revision_of == root
        assert len(service.get_messages(session, root)) == 2
    assert client.delete(f"/nodes/{root}/messages/1").status_code == 409


def test_failure_partial_retry_and_request_replay(setup, monkeypatch):
    client, engine = setup
    root = tree(client)["root_node_id"]

    def broken(*args, **kwargs):
        yield "text", "已回答的一部分"
        raise RuntimeError("should not expose credentials or request dump")

    monkeypatch.setattr(nodes, "stream_chat", broken)
    failed = ask(client, root, request_id="first")
    assert failed[0]["started"] and failed[-1]["status"] == "error"
    assert not any(event.get("done") for event in failed)
    assert "credentials" not in failed[-1]["error"]
    monkeypatch.setattr(nodes, "stream_chat", lambda *a, **k: iter([("text", "完整回答")]))
    done = ask(client, root, mode="retry", request_id="retry-1")
    assert done[-1]["node_id"] == root and done[-1]["done"]
    replay = ask(client, root, mode="retry", request_id="retry-1")
    assert replay[-1]["replayed"]
    with Session(engine) as session:
        messages = service.get_messages(session, root)
        assert len([m for m in messages if m.role == "user"]) == 1
        assert [(m.content, m.status) for m in messages if m.role == "assistant"] == [
            ("已回答的一部分", "error"),
            ("完整回答", "complete"),
        ]
        assert len(session.exec(select(Node)).all()) == 1
    thread = client.get(f"/nodes/{root}/thread").json()["nodes"]
    assert thread[0]["answer"] == "完整回答"
    assert thread[0]["attempts"][0]["content"] == "已回答的一部分"


def test_stop_by_original_node_request_id_preserves_partial(setup, monkeypatch):
    client, engine = setup
    root = tree(client)["root_node_id"]
    ask(client, root)
    produced, release, closed = threading.Event(), threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        try:
            yield "text", "部分"
            produced.set()
            release.wait(3)
            yield "text", "不应追加"
        finally:
            closed.set()

    monkeypatch.setattr(nodes, "stream_chat", slow)
    result = []
    thread = threading.Thread(
        target=lambda: result.extend(ask(client, root, "第二问", request_id="slow"))
    )
    thread.start()
    assert produced.wait(3)
    stopped = client.post(f"/nodes/{root}/stop", json={"request_id": "slow"}).json()
    assert stopped["node_id"] != root
    # Stop persists immediately even if the provider's next chunk is blocked.
    with Session(engine) as session:
        assert service.get_messages(session, stopped["node_id"])[-1].content == "部分"
    thread.join(3)
    assert not thread.is_alive()
    assert result[-1]["status"] == "interrupted"
    with Session(engine) as session:
        target = session.get(Node, stopped["node_id"])
        assert target.status == "interrupted"
        assert service.get_messages(session, target.id)[-1].content == "部分"
    monkeypatch.setattr(nodes, "stream_chat", lambda *a, **k: iter([("text", "重试完整回答")]))
    assert ask(client, stopped["node_id"], "第二问", mode="retry", request_id="fresh-retry")[-1][
        "done"
    ]
    release.set()
    assert closed.wait(3)
    with Session(engine) as session:
        assert session.get(Node, stopped["node_id"]).status == "complete"
        assert service.get_messages(session, stopped["node_id"])[-1].content == "重试完整回答"


def test_legacy_full_thread_and_startup_do_not_move_branches(setup):
    client, engine = setup
    root = tree(client)["root_node_id"]
    with Session(engine) as session:
        child = Node(tree_id=session.get(Node, root).tree_id, parent_id=root, title="旧分支")
        session.add(child)
        for role, content in [
            ("user", "第一问"),
            ("assistant", "第一答"),
            ("user", "第二问"),
            ("assistant", "第二答"),
        ]:
            session.add(Message(node_id=root, role=role, content=content))
        session.commit()
        child_id = child.id
    database.init_db()
    rows = client.get(f"/nodes/{root}/thread").json()["nodes"]
    assert [row["answer"] for row in rows] == ["第一答", "第二答"]
    assert len({row["answer_message_id"] for row in rows}) == 2
    with Session(engine) as session:
        assert session.get(Node, child_id).parent_id == root
        assert len(session.exec(select(Node)).all()) == 2


def test_restart_marks_pending_interrupted(setup):
    client, engine = setup
    root = tree(client)["root_node_id"]
    with Session(engine) as session:
        node = session.get(Node, root)
        node.status = "pending"
        session.add(node)
        session.commit()
    database.init_db()
    assert client.get(f"/nodes/{root}").json()["status"] == "interrupted"


def test_memory_does_not_cross_sibling_paths(setup):
    client, engine = setup
    t = tree(client)
    with Session(engine) as session:
        root = t["root_node_id"]
        session.add(Memory(kind="fact", content="本路径", tree_id=t["id"], source_node_id=root))
        session.add(
            Memory(kind="fact", content="兄弟机密", tree_id=t["id"], source_node_id=root + 99)
        )
        session.add(Memory(kind="fact", content="来源不明", tree_id=t["id"]))
        session.add(Memory(kind="preference", content="喜欢例子"))
        session.commit()
        note = service.fetch_memory_note(session, t["id"], [root])
        assert "本路径" in note and "喜欢例子" in note
        assert "兄弟机密" not in note and "来源不明" not in note


def test_recent_ancestor_keeps_full_tail_and_images():
    answer = "细节" * 1000 + "只有权限明确允许时才执行"
    root = Node(id=1, tree_id=1, title="背景", summary="不完整旧摘要")
    child = Node(id=2, tree_id=1, parent_id=1, title="追问")
    messages = [
        Message(node_id=1, role="user", content="图中是什么", images=["data:image/png;base64,abc"]),
        Message(node_id=1, role="assistant", content=answer),
    ]
    system, context = build_context([(root, messages)], child, [], "然后呢")
    assert context[1]["content"] == answer
    assert context[0]["content"][1]["image_url"]["url"].startswith("data:image")
    assert "不完整旧摘要" not in system


def test_branch_explanation_no_model_config_call_or_graph_growth(setup, monkeypatch):
    client, engine = setup
    root = tree(client)["root_node_id"]
    answer_id = ask(client, root)[-1]["message_id"]
    monkeypatch.setattr(nodes, "complete", lambda *args: "一个简单例子")
    explanation = client.post(
        f"/nodes/{root}/explain", json={"text": "上下文", "source_message_id": answer_id}
    )
    assert explanation.json() == {"explanation": "一个简单例子"}
    with Session(engine) as session:
        assert len(session.exec(select(Node)).all()) == 1
    monkeypatch.setattr(
        nodes, "complete", lambda *args: pytest.fail("branch must not invoke a model")
    )
    branch = client.post(
        f"/nodes/{root}/branch",
        json={
            "seed_text": "上下文",
            "source_message_id": answer_id,
            "source_start": 0,
            "source_end": 3,
        },
    )
    assert branch.status_code == 200
    assert branch.json()["source_message_id"] == answer_id


def test_mock_does_not_assemble_or_execute_tools(setup, monkeypatch):
    client, _ = setup
    root = tree(client)["root_node_id"]

    def assemble(_session, requested):
        assert requested == []
        return [], {}

    monkeypatch.setattr(nodes, "assemble_tools", assemble)
    assert ask(client, root, tools=["mcp_server_999", "fetch"])[-1]["done"]
    result = list(
        run_agent(
            LLMSpec("mock", "mock", "mock", "mock"),
            "",
            [],
            [{"function": {"name": "danger"}}],
            lambda *args: pytest.fail("real tool ran"),
        )
    )
    assert all(event["type"] == "delta" for event in result)


def test_export_import_roundtrip_remaps_anchors_and_excludes_keys(setup):
    client, engine = setup
    t = tree(client)
    root = t["root_node_id"]
    aid = ask(client, root)[-1]["message_id"]
    branch = client.post(
        f"/nodes/{root}/branch",
        json={"seed_text": "概念", "source_message_id": aid, "source_start": 0, "source_end": 2},
    ).json()
    client.patch(f"/nodes/{branch['id']}", json={"learning_note": "我的理解"})
    backup = client.get(f"/trees/{t['id']}/export").json()
    assert "api_key" not in json.dumps(backup) and "request_id" not in json.dumps(backup)
    imported = client.post("/trees/import", json=backup)
    assert imported.status_code == 200, imported.text
    new_tree = imported.json()
    assert new_tree["id"] != t["id"] and new_tree["root_node_id"] != root
    new_nodes = client.get(f"/trees/{new_tree['id']}").json()
    new_branch = next(node for node in new_nodes if node["kind"] == "branch")
    assert new_branch["learning_note"] == "我的理解"
    assert new_branch["source_node_id"] == new_tree["root_node_id"]
    assert new_branch["source_message_id"] != aid
    with Session(engine) as session:
        assert (
            session.get(Message, new_branch["source_message_id"]).node_id
            == new_tree["root_node_id"]
        )
        assert len(session.exec(select(ModelConfig)).all()) == 1


@pytest.mark.parametrize("fault", ["cycle", "unknown_source", "secret", "duplicate"])
def test_import_rejects_invalid_without_partial_tree(setup, fault):
    client, engine = setup
    t = tree(client)
    backup = client.get(f"/trees/{t['id']}/export").json()
    if fault == "cycle":
        backup["nodes"][0]["parent_id"] = backup["nodes"][0]["id"]
    elif fault == "unknown_source":
        backup["nodes"][0]["source_node_id"] = 999
    elif fault == "secret":
        backup["api_key"] = "do-not-import"
    else:
        backup["nodes"].append(dict(backup["nodes"][0]))
    assert client.post("/trees/import", json=backup).status_code == 422
    with Session(engine) as session:
        assert len(session.exec(select(KnowledgeTree)).all()) == 1


def test_api_alias_health_and_cors(setup):
    client, _ = setup
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/trees").status_code == 200
    assert (
        "access-control-allow-origin"
        not in client.get("/trees", headers={"Origin": "https://unknown.example"}).headers
    )
    assert (
        client.get("/trees", headers={"Origin": "http://127.0.0.1:5174"}).headers[
            "access-control-allow-origin"
        ]
        == "http://127.0.0.1:5174"
    )


def test_early_cancel_cannot_stop_different_request_or_create_node(setup):
    client, engine = setup
    root = tree(client)["root_node_id"]
    response = client.post(f"/nodes/{root}/stop", json={"request_id": "cancel-before-start"})
    assert response.status_code == 200
    rejected = client.post(
        f"/nodes/{root}/ask", json={"question": "不会提交", "request_id": "cancel-before-start"}
    )
    assert rejected.status_code == 409
    with Session(engine) as session:
        assert session.get(Node, root).status == "idle"
        assert service.get_messages(session, root) == []


def test_revise_legacy_second_question_keeps_prior_context(setup, monkeypatch):
    client, engine = setup
    t = tree(client)
    root = t["root_node_id"]
    with Session(engine) as session:
        for role, content in [
            ("user", "前提问题"),
            ("assistant", "前提答案"),
            ("user", "原来的第二问"),
            ("assistant", "原来的第二答"),
        ]:
            session.add(Message(node_id=root, role=role, content=content))
        session.commit()
        question_id = [m.id for m in service.get_messages(session, root) if m.role == "user"][-1]
    captured = []

    def answer(_spec, _system, messages, **kwargs):
        captured.extend(messages)
        yield "text", "新第二答"

    monkeypatch.setattr(nodes, "stream_chat", answer)
    revision = ask(client, root, "新第二问", mode="revise", question_message_id=question_id)[-1][
        "node_id"
    ]
    assert [message["content"] for message in captured] == ["前提问题", "前提答案", "新第二问"]
    revised_rows = client.get(f"/nodes/{revision}/thread").json()["nodes"]
    assert [row["question"] for row in revised_rows] == ["前提问题", "新第二问"]
    assert client.get(f"/nodes/{root}/thread").json()["nodes"][-1]["question"] == "原来的第二问"


def test_additive_migration_on_legacy_schema_does_not_rewrite_messages(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledgetree (id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE node (id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL, parent_id INTEGER, title TEXT NOT NULL, seed_text TEXT, summary TEXT, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE message (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, answered_by TEXT, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO knowledgetree VALUES (1, '旧主题', '2026-01-01')"))
        connection.execute(
            text(
                "INSERT INTO node VALUES (1,1,NULL,'旧根',NULL,NULL,'2026-01-01'), (2,1,1,'旧分支','概念',NULL,'2026-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO message VALUES (1,1,'user','问1',NULL,'2026-01-01'), (2,1,'assistant','答1',NULL,'2026-01-01'), (3,1,'user','问2',NULL,'2026-01-01'), (4,1,'assistant','答2',NULL,'2026-01-01')"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with Session(engine) as session:
        assert len(session.exec(select(Node)).all()) == 2
        assert session.get(Node, 2).parent_id == 1
        assert [m.id for m in service.get_messages(session, 1)] == [1, 2, 3, 4]
        assert session.get(Node, 1).source_message_id is None
    engine.dispose()
