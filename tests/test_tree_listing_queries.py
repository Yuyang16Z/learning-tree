"""Topic listings use a constant number of queries, independent of topic size."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.main as main
from app.models import KnowledgeTree, Message, Node


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'listing.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main):
        monkeypatch.setattr(module, "engine", engine)

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    yield TestClient(main.app), engine
    main.app.dependency_overrides.clear()
    engine.dispose()


def _seed(engine, tree_id: int, nodes: int) -> None:
    with Session(engine) as session:
        session.add(KnowledgeTree(id=tree_id, title=f"topic {tree_id}"))
        base = tree_id * 10_000
        for offset in range(nodes):
            node_id = base + offset
            session.add(
                Node(
                    id=node_id,
                    tree_id=tree_id,
                    parent_id=None if offset == 0 else base + (offset - 1) // 2,
                    title=f"node {offset}",
                )
            )
            if offset % 2 == 0:  # Every other node has a question with an image.
                session.add(
                    Message(
                        node_id=node_id,
                        role="user",
                        content="question",
                        images=["data:image/png;base64," + "A" * 4096],
                    )
                )
        session.commit()


def _count_queries(engine, call):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return response, statements


@pytest.mark.parametrize("size", [5, 200])
def test_tree_listing_query_count_does_not_grow_with_nodes(client, size):
    http, engine = client
    _seed(engine, 1, size)
    response, statements = _count_queries(engine, lambda: http.get("/trees/1"))
    assert response.status_code == 200
    body = response.json()
    assert len(body) == size
    # Nodes with a message report complete; nodes without one stay idle.
    assert [node["status"] for node in body[:2]] == ["complete", "idle"]
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) <= 3
    assert not any("message.images" in s for s in statements)  # Bodies are never loaded.


def test_topic_list_finds_roots_with_one_query(client):
    http, engine = client
    for tree_id in range(1, 21):
        _seed(engine, tree_id, 3)
    response, statements = _count_queries(engine, lambda: http.get("/trees"))
    assert response.status_code == 200
    roots = {tree["id"]: tree["root_node_id"] for tree in response.json()}
    assert roots == {tree_id: tree_id * 10_000 for tree_id in range(1, 21)}
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) <= 2
