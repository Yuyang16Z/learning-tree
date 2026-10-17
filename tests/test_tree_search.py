"""Topic search for the map: titles first, then indexed text, with a constant query count."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.main as main
from app.models import KnowledgeTree, Memory, Message, Node


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'search.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main):
        monkeypatch.setattr(module, "engine", engine)

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add_all(
            [
                KnowledgeTree(id=1, title="排序算法"),
                KnowledgeTree(id=2, title="归档", archived=True),
            ]
        )
        session.add_all(
            [
                Node(id=1, tree_id=1, title="快速排序是什么", status="complete"),
                Node(
                    id=2,
                    tree_id=1,
                    parent_id=1,
                    title="分支",
                    seed_text="pivot 的选择",
                    learning_note="笔记：pivot 要随机选",
                    status="complete",
                ),
                Node(id=3, tree_id=1, parent_id=1, title="归并排序", status="complete"),
                Node(id=4, tree_id=2, title="归档树里的 pivot", status="complete"),
            ]
        )
        session.add_all(
            [
                Message(id=11, node_id=1, role="user", content="快速排序是什么？"),
                Message(
                    id=12,
                    node_id=1,
                    role="assistant",
                    content="快速排序选一个 pivot，把数组分成两边再递归。",
                ),
                Message(id=31, node_id=3, role="assistant", content="归并排序是稳定的。"),
                Message(
                    id=32, node_id=3, role="assistant", content="pivot 未完成", status="pending"
                ),
                Message(id=41, node_id=4, role="assistant", content="归档树里也讲 pivot。"),
                Memory(
                    id=101,
                    kind="fact",
                    content="pivot 决定快速排序的划分。",
                    tree_id=1,
                    source_node_id=1,
                ),
            ]
        )
        session.commit()
    yield TestClient(main.app), engine
    main.app.dependency_overrides.clear()
    engine.dispose()


def _count_selects(engine, call):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return response, len([s for s in statements if s.lstrip().upper().startswith("SELECT")])


def test_titles_come_first_then_indexed_text_with_anchors(client):
    http, _ = client
    response = http.get("/trees/1/search", params={"q": "pivot"})
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["rank"] for item in results] == list(range(1, len(results) + 1))
    assert results[0] == {
        "node_id": 2,
        "message_id": None,
        "kind": "title",
        "snippet": "pivot 的选择",
        "rank": 1,
    }
    kinds = [(item["kind"], item["node_id"], item["message_id"]) for item in results[1:]]
    assert set(kinds) == {("message", 1, 12), ("note", 2, None), ("memory", 1, None)}
    for item in results[1:]:
        assert "pivot" in item["snippet"]
    assert all(item["message_id"] != 32 for item in results), "pending answers are not searchable"
    title = http.get("/api/trees/1/search", params={"q": "快速排序"}).json()["results"]
    assert title[0]["kind"] == "title" and title[0]["node_id"] == 1
    assert title[0]["snippet"] == "快速排序是什么"


def test_limits_short_queries_archived_topics_and_errors(client):
    http, _ = client
    assert (
        len(http.get("/trees/1/search", params={"q": "pivot", "limit": 2}).json()["results"]) == 2
    )
    short = http.get("/trees/1/search", params={"q": "稳定"}).json()["results"]
    assert [(item["kind"], item["message_id"]) for item in short] == [("message", 31)]
    archived = http.get("/trees/2/search", params={"q": "pivot"}).json()["results"]
    assert {item["kind"] for item in archived} == {"title", "message"}
    assert http.get("/trees/3/search", params={"q": "pivot"}).status_code == 404
    assert http.get("/trees/1/search", params={"q": "   "}).json() == {"results": []}
    for params in (
        {"q": ""},
        {"q": "x" * 301},
        {"q": "pivot", "limit": 0},
        {"q": "pivot", "limit": 51},
        {},
    ):
        assert http.get("/trees/1/search", params=params).status_code == 422, params


def test_query_count_does_not_grow_with_topic_size(client):
    http, engine = client
    _, small = _count_selects(engine, lambda: http.get("/trees/1/search", params={"q": "pivot"}))
    with Session(engine) as session:
        for offset in range(200):
            node_id = 1000 + offset
            session.add(Node(id=node_id, tree_id=1, parent_id=1, title=f"追问 {offset}"))
            session.add(
                Message(node_id=node_id, role="assistant", content=f"第 {offset} 个 pivot 讨论。")
            )
        session.commit()
    response, large = _count_selects(
        engine, lambda: http.get("/trees/1/search", params={"q": "pivot", "limit": 50})
    )
    assert len(response.json()["results"]) == 50
    assert small == large
