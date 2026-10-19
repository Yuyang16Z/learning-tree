"""Understanding marks are learner state: listed everywhere, never source content."""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.main as main
import app.service as service
from app import learning_summaries
from app.models import ContextSummary, KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'understanding.db'}", connect_args={"check_same_thread": False}
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
    yield TestClient(main.app), engine
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    main.app.dependency_overrides.clear()
    engine.dispose()


def answered_branch(client):
    """A topic whose root and one branch both have a completed answer."""
    created = client.post("/trees", json={"title": "理解标记"}).json()
    root = created["root_node_id"]
    answer_id = _ask(client, root, "什么是递归？")[-1]["message_id"]
    branch = client.post(
        f"/nodes/{root}/branch",
        json={"seed_text": "递归", "source_message_id": answer_id},
    ).json()
    _ask(client, branch["id"], "基例是什么？")
    return created, root, branch["id"]


def _ask(client, node_id, question, **extra):
    response = client.post(f"/nodes/{node_id}/ask", json={"question": question, **extra})
    assert response.status_code == 200, response.text
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[-1].get("done"), events
    return events


def marks(client, tree_id):
    return {node["id"]: node["understanding"] for node in client.get(f"/trees/{tree_id}").json()}


def test_marks_are_set_cleared_and_listed_on_every_node_payload(setup):
    client, _ = setup
    created, root, branch = answered_branch(client)
    assert marks(client, created["id"]) == {root: None, branch: None}

    response = client.patch(f"/nodes/{branch}", json={"understanding": "partial"})
    assert response.status_code == 200, response.text
    assert response.json()["understanding"] == "partial"
    assert marks(client, created["id"]) == {root: None, branch: "partial"}
    assert client.get(f"/nodes/{branch}").json()["understanding"] == "partial"
    rows = client.get(f"/nodes/{branch}/thread").json()["nodes"]
    assert [(row["node_id"], row["understanding"]) for row in rows] == [
        (root, None),
        (branch, "partial"),
    ]

    cleared = client.patch(f"/nodes/{branch}", json={"understanding": None})
    assert cleared.status_code == 200 and cleared.json()["understanding"] is None
    assert marks(client, created["id"]) == {root: None, branch: None}


def test_invalid_or_missing_targets_change_nothing(setup):
    client, engine = setup
    created, root, branch = answered_branch(client)
    client.patch(f"/nodes/{branch}", json={"understanding": "clear"})
    for value in ("maybe", "", 1, True):
        assert client.patch(f"/nodes/{branch}", json={"understanding": value}).status_code == 422
    missing = client.patch("/nodes/999999", json={"understanding": "open"})
    assert missing.status_code == 404 and missing.json()["detail"] == "节点不存在"
    assert marks(client, created["id"]) == {root: None, branch: "clear"}
    with Session(engine) as session:
        assert session.get(Node, branch).understanding == "clear"


def test_mark_and_note_change_independently(setup):
    client, engine = setup
    _, _, branch = answered_branch(client)
    client.patch(f"/nodes/{branch}", json={"learning_note": "  基例让递归停下  "})
    client.patch(f"/nodes/{branch}", json={"understanding": "open"})
    detail = client.get(f"/nodes/{branch}").json()
    assert (detail["learning_note"], detail["understanding"]) == ("基例让递归停下", "open")

    client.patch(f"/nodes/{branch}", json={"learning_note": "新的理解"})
    assert client.get(f"/nodes/{branch}").json()["understanding"] == "open"

    both = client.patch(f"/nodes/{branch}", json={"learning_note": None, "understanding": "clear"})
    assert (both.json()["learning_note"], both.json()["understanding"]) == (None, "clear")

    assert client.patch(f"/nodes/{branch}", json={}).status_code == 200
    with Session(engine) as session:
        node = session.get(Node, branch)
        assert (node.learning_note, node.understanding) == (None, "clear")


def test_mark_keeps_summaries_titles_and_request_state(setup, monkeypatch):
    client, engine = setup
    _, root, branch = answered_branch(client)
    with Session(engine) as session:
        node = session.get(Node, branch)
        session.add(
            ContextSummary(
                key="kept",
                tree_id=node.tree_id,
                source_node_ids=[branch],
                source_message_ids=[],
                fingerprint="synthetic",
                model_fingerprint="synthetic",
                prompt_version="synthetic",
                content="Disposable synthetic summary",
            )
        )
        node.summary = "Cached path summary"
        session.add(node)
        session.commit()
        before = session.get(Node, branch).model_dump(exclude={"understanding"})
        messages = service.get_messages(session, branch)
        sources = [(Node(**session.get(Node, branch).model_dump()), messages)]
        keys = [block.key for block in learning_summaries._blocks(sources, "model", 24000)]

    def forbidden(*args, **kwargs):
        pytest.fail("an understanding mark must not touch summaries, titles or answers")

    monkeypatch.setattr(nodes, "invalidate_summaries", forbidden)
    monkeypatch.setattr(nodes, "_queue_branch_title", forbidden)
    monkeypatch.setattr(nodes, "stream_chat", forbidden)
    for value in ("open", "clear", None, "partial"):
        assert client.patch(f"/nodes/{branch}", json={"understanding": value}).status_code == 200

    with Session(engine) as session:
        assert [row.key for row in session.exec(select(ContextSummary)).all()] == ["kept"]
        current = session.get(Node, branch)
        assert current.model_dump(exclude={"understanding"}) == before
        assert current.understanding == "partial"
        marked = [(Node(**current.model_dump()), service.get_messages(session, branch))]
        # Summary keys and in-flight validation ignore the mark.
        assert [b.key for b in learning_summaries._blocks(marked, "model", 24000)] == keys
        assert learning_summaries._valid_sources(session, sources)
        assert session.get(Node, root).understanding is None


def test_mark_reaches_the_change_feed(setup):
    client, _ = setup
    created, _, branch = answered_branch(client)
    cursor = client.get("/changes").json()["cursor"]
    client.patch(f"/nodes/{branch}", json={"understanding": "open"})
    notice = client.get("/changes", params={"since": cursor}).json()
    assert not notice["reload"]
    assert notice["trees"] == {str(created["id"]): [branch]}
    cursor = notice["cursor"]
    client.patch(f"/nodes/{branch}", json={"understanding": "open"})  # Unchanged value.
    assert client.get("/changes", params={"since": cursor}).json()["trees"] == {}


def test_revisions_and_follow_ups_start_unmarked(setup):
    client, engine = setup
    created, root, _ = answered_branch(client)
    client.patch(f"/nodes/{root}", json={"understanding": "clear"})
    revision = _ask(client, root, "重新问递归", mode="revise")[-1]["node_id"]
    follow_up = _ask(client, root, "再追问一句")[-1]["node_id"]
    assert revision != root and follow_up != root
    current = marks(client, created["id"])
    assert current[root] == "clear"
    assert current[revision] is None and current[follow_up] is None


def test_unknown_stored_value_reads_as_unmarked(setup):
    client, engine = setup
    created, root, branch = answered_branch(client)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE node SET understanding = 'later-level' WHERE id = :id"), {"id": branch}
        )
    assert marks(client, created["id"]) == {root: None, branch: None}
    assert client.get(f"/nodes/{branch}").json()["understanding"] is None
    exported = client.get(f"/trees/{created['id']}/export").json()
    assert all("understanding" not in node for node in exported["nodes"])


def test_export_writes_marks_only_when_set_and_import_restores_them(setup):
    client, engine = setup
    created, root, branch = answered_branch(client)
    plain = client.get(f"/trees/{created['id']}/export").json()
    assert all("understanding" not in node for node in plain["nodes"]), (
        "unmarked topics export exactly as before"
    )
    client.patch(f"/nodes/{branch}", json={"understanding": "open"})
    backup = client.get(f"/trees/{created['id']}/export").json()
    exported = {node["id"]: node for node in backup["nodes"]}
    assert exported[branch]["understanding"] == "open"
    assert "understanding" not in exported[root]

    imported = client.post("/trees/import", json=backup)
    assert imported.status_code == 200, imported.text
    restored = client.get(f"/trees/{imported.json()['id']}").json()
    assert sorted((node["kind"], node["understanding"]) for node in restored) == [
        ("branch", "open"),
        ("root", None),
    ]
    legacy = client.post("/trees/import", json=plain)
    assert legacy.status_code == 200
    assert {
        node["understanding"] for node in client.get(f"/trees/{legacy.json()['id']}").json()
    } == {None}

    with Session(engine) as session:
        topics = len(session.exec(select(KnowledgeTree)).all())
    backup["nodes"][1]["understanding"] = "half"
    assert client.post("/trees/import", json=backup).status_code == 422
    with Session(engine) as session:
        assert len(session.exec(select(KnowledgeTree)).all()) == topics


def test_legacy_schema_gains_a_nullable_unmarked_column(tmp_path, monkeypatch):
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
                "learning_note TEXT, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO knowledgetree VALUES (1, '旧主题', '2026-01-01')"))
        connection.execute(
            text(
                "INSERT INTO node (id, tree_id, parent_id, title, seed_text, learning_note, "
                "created_at) VALUES (1, 1, NULL, '旧根', NULL, NULL, '2026-01-01'), "
                "(2, 1, 1, '旧分支', '概念', '旧笔记', '2026-01-01')"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    column = next(c for c in inspect(engine).get_columns("node") if c["name"] == "understanding")
    assert column["nullable"] and column["default"] is None
    with Session(engine) as session:
        assert [node.understanding for node in session.exec(select(Node)).all()] == [None, None]
        node = session.get(Node, 2)
        assert node.learning_note == "旧笔记"
        node.understanding = "partial"
        session.add(node)
        session.commit()
    database.init_db()
    with Session(engine) as session:
        assert session.get(Node, 2).understanding == "partial", "repeat upgrades keep marks"
        assert session.get(Message, 1) is None
    engine.dispose()
