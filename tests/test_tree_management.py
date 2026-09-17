"""Topic organization preserves learning content and upgrades old SQLite databases safely."""

import hashlib
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
from app.documents import DocumentAttachment
from app.models import KnowledgeTree, Memory, MemoryEmbedding, Message, Node
from app.routers.trees import router


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'topics.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    application = FastAPI()
    application.include_router(router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[database.get_session] = session_override
    yield TestClient(application), engine
    engine.dispose()


def content_snapshot(engine):
    with Session(engine) as session:
        return {
            model.__name__: [row.model_dump() for row in session.exec(select(model)).all()]
            for model in (Node, Message, Memory, MemoryEmbedding, DocumentAttachment)
        }


def test_archive_column_migrates_old_trees_without_rewriting_content(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledgetree (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO knowledgetree VALUES (51, '原有学习话题', '2026-01-01')")
        )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Node(id=79, tree_id=51, title="最初的问题", kind="root", title_state="manual"))
        session.add(Message(id=97, node_id=79, role="user", content="保留原有问题"))
        session.commit()
    before = content_snapshot(engine)
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    columns = inspect(engine).get_columns("knowledgetree")
    archived = next(column for column in columns if column["name"] == "archived")
    assert not archived["nullable"]
    with Session(engine) as session:
        tree = session.get(KnowledgeTree, 51)
        assert tree.title == "原有学习话题" and not tree.archived
        assert tree.created_at.isoformat() == "2026-01-01T00:00:00"
        tree.archived = True
        session.add(tree)
        session.commit()
    database.init_db()
    with Session(engine) as session:
        assert session.get(KnowledgeTree, 51).archived, "repeat migration must not unarchive topics"
    assert content_snapshot(engine) == before
    engine.dispose()


def test_rename_archive_restore_preserve_learning_content_and_exports(setup):
    client, engine = setup
    tree = client.post("/trees", json={"title": "原话题", "root_question": "原来的问题"}).json()
    other = client.post("/trees", json={"title": "另一个话题"}).json()
    assert tree["archived"] is False
    identifier, root = tree["id"], tree["root_node_id"]
    content = b"Learning document"
    with Session(engine) as session:
        session.add(
            DocumentAttachment(
                id="document-1",
                tree_id=identifier,
                name="notes.txt",
                media_type="text/plain",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content=content,
                sections=[{"label": "notes", "text": content.decode()}],
            )
        )
        session.add(
            Message(node_id=root, role="user", content="原来的问题", document_ids=["document-1"])
        )
        session.add(Message(node_id=root, role="assistant", content="回答 **原文**"))
        session.add(
            Node(
                tree_id=identifier,
                parent_id=root,
                title="分支问题",
                kind="branch",
                summary="已有摘要",
                learning_note="学习笔记",
            )
        )
        memory = Memory(kind="fact", tree_id=identifier, source_node_id=root, content="学习事实")
        session.add(memory)
        session.flush()
        session.add(
            MemoryEmbedding(
                memory_id=memory.id, model_key="test", content_hash="hash", vector=[1.0]
            )
        )
        session.commit()
    before = content_snapshot(engine)
    renamed = client.patch(f"/trees/{identifier}", json={"title": "  ReAct 入门  "})
    assert renamed.status_code == 200
    assert renamed.json() == {**tree, "title": "ReAct 入门"}
    archived = client.patch(f"/trees/{identifier}", json={"archived": True})
    assert archived.status_code == 200 and archived.json()["archived"]
    assert client.get("/trees").json() == [other]
    assert client.get("/trees?include_archived=false").json() == [other]
    assert client.get("/trees?include_archived=true").json() == [archived.json(), other]
    assert client.get(f"/trees/{identifier}").status_code == 200
    backup = client.get(f"/trees/{identifier}/export")
    assert backup.status_code == 200
    assert backup.json()["tree"] == {"title": "ReAct 入门"}
    assert backup.json()["nodes"][0]["title"] == "原来的问题"
    assert len(backup.json()["documents"]) == 1
    assert content_snapshot(engine) == before
    # Local organization does not make an imported learning topic invisible.
    imported = client.post("/trees/import", json=backup.json())
    assert imported.status_code == 200 and imported.json()["archived"] is False
    assert imported.json()["id"] != identifier
    restored = client.patch(f"/trees/{identifier}", json={"archived": False})
    assert restored.json() == renamed.json()
    assert [item["id"] for item in client.get("/trees").json()] == [
        identifier,
        other["id"],
        imported.json()["id"],
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": " \n\t "},
        {"title": "长" * 301},
        {"title": None},
        {"archived": None},
        {"archived": "true"},
        {"archived": True, "title": None},
        {"unsupported": "new name"},
    ],
)
def test_invalid_topic_updates_do_not_mutate_data(setup, payload):
    client, engine = setup
    tree = client.post("/trees", json={"title": "保持原样"}).json()
    before = content_snapshot(engine)
    assert client.patch(f"/trees/{tree['id']}", json=payload).status_code == 422
    assert client.get("/trees?include_archived=true").json() == [tree]
    assert content_snapshot(engine) == before


def test_topic_patch_supports_combined_fields_and_returns_missing_topic(setup):
    client, _engine = setup
    assert client.patch("/trees/999", json={"title": "新名称"}).status_code == 404
    tree = client.post("/trees", json={"title": "原话题"}).json()
    payload = {"title": "  " + "字" * 300 + "  ", "archived": True}
    updated = client.patch(f"/trees/{tree['id']}", json=payload)
    assert updated.status_code == 200
    assert updated.json() == {**tree, "title": "字" * 300, "archived": True}
    assert client.get("/trees").json() == []
