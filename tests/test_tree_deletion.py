"""Permanent topic deletion removes owned data and rejects late background writes."""

import hashlib
import json
import os
import threading

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.context_compaction as compaction
import app.db as database
import app.documents as documents
import app.service as service
from app.llm import LLMSpec
from app.models import (
    KnowledgeTree,
    McpServer,
    Memory,
    MemoryEmbedding,
    Message,
    ModelConfig,
    Node,
    PreferenceProfile,
)
from app.routers import nodes, trees


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'delete.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    application = FastAPI()
    for router in (trees.router, nodes.router, documents.router):
        application.include_router(router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[database.get_session] = session_override
    yield TestClient(application), engine
    for generation in nodes._GENERATIONS.values():
        generation.stop.set()
    nodes._GENERATIONS.clear()
    nodes._TITLE_JOBS.clear()
    nodes._CANCELLED_REQUESTS.clear()
    engine.dispose()


def create(client, title="Topic"):
    response = client.post("/trees", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()


def document(identifier, tree_id, content=b"Private source document"):
    return documents.DocumentAttachment(
        id=identifier,
        tree_id=tree_id,
        name=identifier + ".txt",
        media_type="text/plain",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        sections=[{"label": "Page 1", "text": content.decode()}],
    )


def test_delete_removes_all_owned_records_and_caches_preserving_other_topics(setup):
    client, engine = setup
    target, other = create(client, "Delete me"), create(client, "Keep me")
    target_id, target_root = target["id"], target["root_node_id"]
    other_id, other_root = other["id"], other["root_node_id"]
    with Session(engine) as session:
        session.add_all(
            [
                document("target-attached", target_id),
                document("target-unsent", target_id),
                document("other-doc", other_id),
                Message(
                    node_id=target_root,
                    role="user",
                    content="Private question",
                    images=["data:image/png;base64,cHJpdmF0ZQ=="],
                    document_ids=["target-attached"],
                ),
                Message(node_id=target_root, role="assistant", content="Private answer"),
                Message(node_id=other_root, role="user", content="Keep question"),
                Node(tree_id=target_id, parent_id=target_root, title="Private branch"),
                ModelConfig(label="Keep model", base_url="mock", llm_model="mock", api_key="mock"),
                McpServer(label="Shared library", command="mock", args=["shared-library"]),
                PreferenceProfile(content="Manually managed preferences"),
            ]
        )
        memories = [
            Memory(
                kind="fact", tree_id=target_id, source_node_id=target_root, content="Private fact"
            ),
            Memory(kind="fact", tree_id=target_id, content="Private legacy fact"),
            Memory(kind="preference", source_node_id=target_root, content="Derived preference"),
            Memory(kind="fact", tree_id=other_id, source_node_id=other_root, content="Keep fact"),
            Memory(kind="preference", content="Unattributed shared preference"),
        ]
        session.add_all(memories)
        session.flush()
        for memory in memories:
            session.add(
                MemoryEmbedding(
                    memory_id=memory.id, model_key="test", content_hash="hash", vector=[1.0]
                )
            )
        session.commit()
        kept_memory_ids = [memories[3].id, memories[4].id]
        preserved = {
            model.__name__: [row.model_dump() for row in session.exec(select(model)).all()]
            for model in (ModelConfig, McpServer, PreferenceProfile)
        }
        for tree_id, root in ((target_id, target_root), (other_id, other_root)):
            compaction._DELETED_TREES.discard(tree_id)
            compaction.extract(session.get(Node, root), service.get_messages(session, root))
    deleted_generation = nodes.Generation(request_id="target", text=["Private pending text"])
    kept_generation = nodes.Generation(request_id="other", text=["Other pending text"])
    nodes._GENERATIONS.update({target_root: deleted_generation, other_root: kept_generation})
    nodes._TITLE_JOBS.update({target_root: "target-job", other_root: "other-job"})
    nodes._CANCELLED_REQUESTS.update({(target_id, "cancel"): 1.0, (other_id, "cancel"): 1.0})

    assert client.delete(f"/trees/{target_id}").json() == {"deleted": target_id}
    assert client.delete(f"/trees/{target_id}").status_code == 404
    for endpoint in (
        f"/trees/{target_id}",
        f"/trees/{target_id}/export",
        f"/nodes/{target_root}",
        f"/nodes/{target_root}/thread",
        "/documents/target-attached",
        "/documents/target-unsent/download",
    ):
        assert client.get(endpoint).status_code == 404, endpoint
    assert client.get("/documents/other-doc/download").status_code == 200
    assert deleted_generation.stop.is_set() and not deleted_generation.text
    assert target_root not in nodes._GENERATIONS and target_root not in nodes._TITLE_JOBS
    assert (target_id, "cancel") not in nodes._CANCELLED_REQUESTS
    assert not kept_generation.stop.is_set() and nodes._TITLE_JOBS[other_root] == "other-job"
    assert (other_id, "cancel") in nodes._CANCELLED_REQUESTS
    assert not any(key[0] == target_id for key in compaction._CACHE)
    assert any(key[0] == other_id for key in compaction._CACHE)
    # Checkpoint cleanup can race a provider's final return; it must remain a no-op.
    nodes._persist(target_root, deleted_generation, "Mock")
    with Session(engine) as session:
        assert [row.id for row in session.exec(select(KnowledgeTree)).all()] == [other_id]
        assert [row.id for row in session.exec(select(Node)).all()] == [other_root]
        assert [row.content for row in session.exec(select(Message)).all()] == ["Keep question"]
        assert [row.id for row in session.exec(select(documents.DocumentAttachment)).all()] == [
            "other-doc"
        ]
        assert [row.id for row in session.exec(select(Memory)).all()] == kept_memory_ids
        assert [
            row.memory_id for row in session.exec(select(MemoryEmbedding)).all()
        ] == kept_memory_ids
        for model in (ModelConfig, McpServer, PreferenceProfile):
            assert [row.model_dump() for row in session.exec(select(model)).all()] == preserved[
                model.__name__
            ]


def test_document_finishing_after_delete_cannot_reappear_on_replacement_node(setup, monkeypatch):
    client, engine = setup
    target = create(client)
    entered, release = threading.Event(), threading.Event()
    result = []

    def delayed_parse(content, name):
        entered.set()
        assert release.wait(5)
        return [{"label": name, "text": content.decode()}], []

    monkeypatch.setattr(documents, "parse_document_limited", delayed_parse)
    worker = threading.Thread(
        target=lambda: result.append(
            client.post(
                f"/nodes/{target['root_node_id']}/documents",
                files={"file": ("late.txt", b"Deleted source material", "text/plain")},
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(3)
        assert client.delete(f"/trees/{target['id']}").status_code == 200
        replacement = create(client, "Replacement")
        assert replacement["root_node_id"] > target["root_node_id"]
        assert replacement["id"] > target["id"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and result[0].status_code == 404
    with Session(engine) as session:
        assert session.exec(select(documents.DocumentAttachment)).all() == []


def test_late_memory_extraction_does_not_write_to_replacement_topic(setup, monkeypatch):
    client, engine = setup
    target = create(client)
    with Session(engine) as session:
        root = session.get(Node, target["root_node_id"])
        root.status = "complete"
        session.add(root)
        session.commit()

    def extract_then_delete(*args):
        assert client.delete(f"/trees/{target['id']}").status_code == 200
        replacement = create(client)
        assert replacement["root_node_id"] > target["root_node_id"]
        with Session(engine) as session:
            root = session.get(Node, replacement["root_node_id"])
            root.status = "complete"
            session.add(root)
            session.commit()
        return {"facts": ["Deleted fact"], "preferences": ["Deleted preference"]}

    monkeypatch.setattr(service, "extract_memories", extract_then_delete)
    spec = LLMSpec(
        label="test", base_url="https://example.invalid", llm_model="test", api_key="test"
    )
    service.extract_and_save(spec, target["id"], target["root_node_id"], "Question", "Answer")
    with Session(engine) as session:
        assert session.exec(select(Memory)).all() == []


def test_topic_ids_are_not_reused_after_deletion_restart_or_import(setup):
    client, engine = setup
    target = create(client)
    backup = client.get(f"/trees/{target['id']}/export").json()
    assert client.delete(f"/trees/{target['id']}").status_code == 200
    # Clear connections and rerun startup, retaining only the persisted SQLite file.
    engine.dispose()
    database.init_db()
    restored = client.post("/trees/import", json=backup)
    assert restored.status_code == 200
    assert restored.json()["id"] > target["id"]
    assert client.delete(f"/trees/{restored.json()['id']}").status_code == 200
    assert create(client)["id"] > restored.json()["id"]


def test_deleted_node_ids_are_not_reused_by_branch_followup_revision_or_import(setup):
    client, engine = setup
    tree = create(client)
    root = tree["root_node_id"]
    with Session(engine) as session:
        session.add(ModelConfig(label="Mock", base_url="mock", llm_model="mock", api_key="mock"))
        # Legacy nodes predate the counter, so deletion must preserve their high-water mark.
        session.add(Node(id=900, tree_id=tree["id"], parent_id=root, title="Legacy branch"))
        session.commit()
    assert client.delete("/nodes/900").status_code == 200
    branch = client.post(f"/nodes/{root}/branch", json={"seed_text": "Source quote"}).json()
    assert branch["id"] > 900

    def ask(node_id, question, mode="continue"):
        response = client.post(f"/nodes/{node_id}/ask", json={"question": question, "mode": mode})
        assert response.status_code == 200, response.text
        result = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert result[-1]["done"]
        return result[-1]["node_id"]

    assert ask(branch["id"], "First question") == branch["id"]
    followup = ask(branch["id"], "Second question")
    assert followup > branch["id"]
    assert client.delete(f"/nodes/{followup}").status_code == 200
    revision = ask(branch["id"], "Revised question", "revise")
    assert revision > followup
    backup = client.get(f"/trees/{tree['id']}/export").json()
    assert client.delete(f"/trees/{tree['id']}").status_code == 200
    engine.dispose()
    database.init_db()
    imported = client.post("/trees/import", json=backup)
    assert imported.status_code == 200
    imported_nodes = client.get(f"/trees/{imported.json()['id']}").json()
    assert min(node["id"] for node in imported_nodes) > revision
    new_root = create(client)["root_node_id"]
    assert new_root > max(node["id"] for node in imported_nodes)


def test_late_compaction_snapshot_cannot_recache_deleted_content():
    source = Node(id=943, tree_id=943, title="Deleted title")
    messages = [Message(id=943, node_id=943, role="user", content="Deleted question")]
    compaction.extract(source, messages)
    compaction.forget_tree(943)
    compaction.extract(source, messages)
    assert not any(key[0] == 943 for key in compaction._CACHE)
