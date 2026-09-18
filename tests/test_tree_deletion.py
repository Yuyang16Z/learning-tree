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
    PreferenceExtraction,
    PreferenceProfile,
    PreferenceSupplement,
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


@pytest.mark.parametrize("delete_whole_tree", [False, True])
def test_deletion_purges_supplements_and_extraction_ledger_only_for_its_sources(
    setup, delete_whole_tree
):
    client, engine = setup
    target, other = create(client), create(client, "Keep")
    with Session(engine) as session:
        for tree in (target, other):
            source = session.get(Node, tree["root_node_id"])
            source.status = "complete"
            session.add(source)
            for scope in ("global", "topic"):
                session.add(
                    PreferenceSupplement(
                        content=f"Derived {tree['id']} {scope}",
                        evidence="Explicit durable preference",
                        scope=scope,
                        tree_id=tree["id"] if scope == "topic" else None,
                        source_node_id=source.id,
                        source_tree_id=tree["id"],
                    )
                )
            session.add(
                PreferenceExtraction(key=str(tree["id"]), node_id=source.id, tree_id=tree["id"])
            )
        session.add(PreferenceProfile(content="Keep handwritten profile."))
        session.commit()
    endpoint = f"/trees/{target['id']}" if delete_whole_tree else f"/nodes/{target['root_node_id']}"
    assert client.delete(endpoint).status_code == 200
    with Session(engine) as session:
        supplements = session.exec(select(PreferenceSupplement)).all()
        assert len(supplements) == 2
        assert {row.source_tree_id for row in supplements} == {other["id"]}
        assert {row.tree_id for row in session.exec(select(PreferenceExtraction))} == {other["id"]}
        assert session.get(PreferenceProfile, 1).content == "Keep handwritten profile."


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


def test_delete_failed_retry_subtree_preserves_successful_sibling_revision(setup, monkeypatch):
    client, engine = setup
    tree, other = create(client), create(client, "Unrelated topic")
    root = tree["root_node_id"]
    attempts = []

    def answer(*args, **kwargs):
        attempts.append(True)
        if len(attempts) <= 2:
            yield "text", "Partial failed answer"
            raise RuntimeError("Synthetic provider failure")
        yield "text", "Successful revised answer"

    monkeypatch.setattr(nodes, "stream_chat", answer)
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *args, **kwargs: "")
    monkeypatch.setattr(nodes, "assemble_tools", lambda *args: ([], {}))
    monkeypatch.setattr(nodes, "_queue_branch_title", lambda *args, **kwargs: None)
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)
    with Session(engine) as session:
        session.add(
            ModelConfig(
                label="Offline test",
                base_url="https://offline.invalid/v1",
                llm_model="synthetic",
                api_key="synthetic-key-never-sent",
            )
        )
        session.commit()
    failed = client.post(f"/nodes/{root}/branch", json={"seed_text": "Shared source quote"}).json()
    failed_id = failed["id"]

    def ask(question, mode="continue"):
        response = client.post(f"/nodes/{failed_id}/ask", json={"question": question, "mode": mode})
        assert response.status_code == 200, response.text
        return [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ][-1]

    assert ask("Question that fails")["status"] == "error"
    assert ask("", "retry")["status"] == "error"
    success = ask("Revised successful question", "revise")
    assert success["done"]
    revision_id = success["node_id"]
    assert revision_id != failed_id and len(attempts) == 3

    with Session(engine) as session:
        revision = session.get(Node, revision_id)
        assert revision.parent_id == root and revision.revision_of == failed_id
        assert session.get(Node, failed_id).status == "error"
        failed_messages = service.get_messages(session, failed_id)
        assert [m.status for m in failed_messages if m.role == "assistant"] == ["error", "error"]
        failed_messages[0].images = ["data:image/png;base64,c3ludGhldGlj"]
        failed_messages[0].document_ids = ["shared-upload"]
        session.add(failed_messages[0])
        child = Node(
            tree_id=tree["id"],
            parent_id=failed_id,
            title="Pending descendant",
            status="pending",
            request_id="pending-descendant",
        )
        session.add(child)
        session.flush()
        leaf = Node(
            tree_id=tree["id"], parent_id=child.id, title="Completed descendant", status="complete"
        )
        # A surviving detached reference can occur in imported/legacy trees.
        reference = Node(
            tree_id=tree["id"],
            parent_id=root,
            title="Keep independent branch",
            kind="branch",
            source_node_id=failed_id,
            source_message_id=failed_messages[-1].id,
            source_start=0,
            source_end=7,
            seed_text="Copied quotation remains useful",
            learning_note="Keep my own reflection",
        )
        session.add_all(
            [
                leaf,
                reference,
                document("shared-upload", tree["id"]),
                document("other-upload", other["id"]),
            ]
        )
        session.flush()
        child_id, leaf_id, reference_id = child.id, leaf.id, reference.id
        session.add_all(
            [
                Message(
                    node_id=child_id,
                    role="assistant",
                    content="Pending partial answer",
                    status="pending",
                    reasoning="Private reasoning",
                    steps=[{"tool": "fixture", "result": "Private result"}],
                ),
                Message(node_id=leaf_id, role="assistant", content="Descendant answer"),
                Message(node_id=reference_id, role="user", content="Keep independent question"),
                Message(
                    node_id=other["root_node_id"], role="user", content="Keep unrelated question"
                ),
            ]
        )
        memories = [
            Memory(
                kind="fact",
                tree_id=tree["id"],
                source_node_id=failed_id,
                content="Failed-node fact",
            ),
            Memory(
                kind="preference", source_node_id=child_id, content="Descendant-derived preference"
            ),
            Memory(kind="fact", tree_id=tree["id"], source_node_id=leaf_id, content="Leaf fact"),
            Memory(
                kind="fact",
                tree_id=tree["id"],
                source_node_id=revision_id,
                content="Keep revised fact",
            ),
            Memory(kind="fact", tree_id=tree["id"], content="Keep unattributed topic fact"),
            Memory(
                kind="fact",
                tree_id=other["id"],
                source_node_id=other["root_node_id"],
                content="Keep unrelated fact",
            ),
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
        removed_ids = {failed_id, child_id, leaf_id}
        kept_memories = {m.id: m.model_dump() for m in memories[3:]}
        kept_messages = {
            m.id: m.model_dump()
            for m in session.exec(select(Message)).all()
            if m.node_id not in removed_ids
        }
        kept_documents = {
            d.id: d.model_dump() for d in session.exec(select(documents.DocumentAttachment)).all()
        }
        session.refresh(revision)
        session.refresh(reference)
        kept_revision = revision.model_dump() | {"revision_of": None}
        kept_reference = reference.model_dump() | dict.fromkeys(
            ("source_node_id", "source_message_id", "source_start", "source_end")
        )
    pending = nodes.Generation(
        request_id="pending-descendant",
        text=["Late answer"],
        reasoning=["Late reasoning"],
        steps=[{"tool": "fixture"}],
    )
    pending.events.put({"type": "delta", "text": "Buffered answer"})
    unrelated = nodes.Generation(request_id="unrelated", text=["Keep buffer"])
    nodes._GENERATIONS.update({child_id: pending, other["root_node_id"]: unrelated})
    nodes._TITLE_JOBS.update(
        {failed_id: "failed-title", child_id: "child-title", revision_id: "keep-title"}
    )
    deletion = []

    def finish_extraction_after_delete(*args):
        deletion.append(client.delete(f"/nodes/{failed_id}"))
        return {"facts": ["Must not be resurrected"], "preferences": ["Must not be resurrected"]}

    monkeypatch.setattr(service, "extract_memories", finish_extraction_after_delete)
    service.extract_and_save(
        LLMSpec(
            label="Offline",
            base_url="https://offline.invalid",
            llm_model="synthetic",
            api_key="synthetic",
        ),
        tree["id"],
        leaf_id,
        "Question",
        "Answer",
    )
    assert deletion[0].status_code == 200 and set(deletion[0].json()["deleted"]) == removed_ids
    assert (
        pending.stop.is_set() and not pending.text and not pending.reasoning and not pending.steps
    )
    assert pending.events.empty() and child_id not in nodes._GENERATIONS
    assert failed_id not in nodes._TITLE_JOBS and child_id not in nodes._TITLE_JOBS
    assert nodes._TITLE_JOBS[revision_id] == "keep-title"
    assert not unrelated.stop.is_set() and unrelated.text == ["Keep buffer"]
    # A provider that finishes after stop cannot recreate its deleted assistant row.
    pending.text.append("Provider returned after deletion")
    nodes._persist(child_id, pending, "Offline")
    assert client.delete(f"/nodes/{failed_id}").status_code == 404
    for identifier in removed_ids:
        assert client.get(f"/nodes/{identifier}").status_code == 404
        assert client.get(f"/nodes/{identifier}/thread").status_code == 404
    assert client.get(f"/nodes/{revision_id}/thread").status_code == 200
    assert client.get("/documents/shared-upload/download").status_code == 200
    with Session(engine) as session:
        assert all(session.get(Node, identifier) is None for identifier in removed_ids)
        assert session.get(Node, revision_id).model_dump() == kept_revision
        assert session.get(Node, reference_id).model_dump() == kept_reference
        assert {m.id: m.model_dump() for m in session.exec(select(Message)).all()} == kept_messages
        assert {m.id: m.model_dump() for m in session.exec(select(Memory)).all()} == kept_memories
        assert {m.memory_id for m in session.exec(select(MemoryEmbedding)).all()} == set(
            kept_memories
        )
        assert {
            d.id: d.model_dump() for d in session.exec(select(documents.DocumentAttachment)).all()
        } == kept_documents
        assert {t.id for t in session.exec(select(KnowledgeTree)).all()} == {
            tree["id"],
            other["id"],
        }


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
