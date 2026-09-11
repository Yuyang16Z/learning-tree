"""Question-based branch titles, isolated from live data and all model providers."""

import json
import os
import threading
import time
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.main as main
import app.service as service
from app.models import Message, ModelConfig, Node
from app.routers import nodes
from app.title_generation import fallback_title

SOURCE = "AI 领域的 harness 工程，核心是：模型本身只是会预测下一个词的引擎。"
QUESTION = "能详细给我从最基础讲一下 Eval harness：评估框架吗？"
AI_TITLE = "Eval harness 评估框架入门"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'branch-titles.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)
    workers, timers, releases = [], [], []

    def tracked_thread(*args, **kwargs):
        worker = threading.Thread(*args, **kwargs)
        workers.append(worker)
        return worker

    def tracked_timer(*args, **kwargs):
        timer = threading.Timer(*args, **kwargs)
        timers.append(timer)
        return timer

    monkeypatch.setattr(
        nodes, "threading", SimpleNamespace(Thread=tracked_thread, Timer=tracked_timer)
    )
    monkeypatch.setattr(nodes, "stream_chat", lambda *a, **k: iter([("text", SOURCE)]))
    monkeypatch.setattr(nodes, "extract_and_save", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "assemble_tools", lambda *a, **k: ([], {}))
    monkeypatch.setattr(
        nodes, "summarize_question", lambda *a, **k: pytest.fail("unexpected title request")
    )
    nodes._GENERATIONS.clear()
    nodes._TITLE_JOBS.clear()
    nodes._CANCELLED_REQUESTS.clear()

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add(
            ModelConfig(
                label="Deterministic fixture",
                base_url="https://provider.invalid/v1",
                llm_model="fixture-model",
                api_key="unit-test-only",
                is_default=True,
            )
        )
        session.commit()
    client = TestClient(main.app)
    env = SimpleNamespace(
        client=client, engine=engine, workers=workers, timers=timers, releases=releases
    )
    try:
        yield env
    finally:
        for release in releases:
            release.set()
        for generation in list(nodes._GENERATIONS.values()):
            generation.stop.set()
        for timer in timers:
            timer.cancel()
        # Join before restoring engine globals: late callbacks must never touch
        # another test's database or the application's real database.
        for worker in workers + timers:
            if worker.ident is not None:
                worker.join(3)
                assert not worker.is_alive(), "title test left a running worker"
        client.close()
        nodes._GENERATIONS.clear()
        nodes._TITLE_JOBS.clear()
        nodes._CANCELLED_REQUESTS.clear()
        main.app.dependency_overrides.clear()
        engine.dispose()


def ask(env, node_id, question, **kwargs):
    response = env.client.post(f"/nodes/{node_id}/ask", json={"question": question, **kwargs})
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def create_branch(env, **kwargs):
    tree = env.client.post("/trees", json={"title": "Harness learning"}).json()
    root_id = tree["root_node_id"]
    answer_id = ask(env, root_id, "什么是 harness 工程？")[-1]["message_id"]
    response = env.client.post(
        f"/nodes/{root_id}/branch",
        json={
            "seed_text": SOURCE,
            "source_message_id": answer_id,
            "source_start": 0,
            "source_end": 10,
            "learning_note": "Keep the source separate from my new question.",
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return tree, response.json()


def wait_title(env, node_id, state):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = env.client.get(f"/nodes/{node_id}").json()
        if result["title_state"] == state:
            return result
        threading.Event().wait(0.01)
    pytest.fail(f"branch title did not reach {state}: {result['title_state']}")


def held_title(env, monkeypatch, title=AI_TITLE):
    started, release = threading.Event(), threading.Event()
    calls = []
    env.releases.append(release)

    def summarize(spec, question, source):
        calls.append((spec, question, source))
        started.set()
        assert release.wait(3), "test did not release title provider"
        return title

    monkeypatch.setattr(nodes, "summarize_question", summarize)
    return started, release, calls


def test_first_question_gets_ai_title_without_blocking_answer(workspace, monkeypatch):
    env = workspace
    tree, branch = create_branch(env)
    assert branch["title"] == "" and branch["title_state"] == "empty"
    started, release, calls = held_title(env, monkeypatch)
    response = ask(env, branch["id"], QUESTION, request_id="first-question")
    assert started.wait(1)
    assert response[-1]["done"] is True
    assert not release.is_set(), "answer must finish while its title is still blocked"
    pending = wait_title(env, branch["id"], "pending")
    assert pending["title"] == fallback_title(QUESTION)
    assert pending["title"] != SOURCE[:24]
    assert pending["status"] == "complete"
    assert calls[0][1:] == (QUESTION, SOURCE)
    release.set()
    result = wait_title(env, branch["id"], "ai")
    assert result["title"] == AI_TITLE
    assert result["source_node_id"] == tree["root_node_id"]
    for field in ("seed_text", "source_message_id", "source_start", "source_end", "learning_note"):
        assert result[field] == branch[field]
    assert [message["content"] for message in result["messages"]] == [QUESTION, SOURCE]
    graph = env.client.get(f"/trees/{tree['id']}").json()
    assert next(node for node in graph if node["id"] == branch["id"])["title"] == AI_TITLE
    rows = env.client.get(f"/nodes/{branch['id']}/thread").json()["nodes"]
    assert rows[-1]["title"] == AI_TITLE


@pytest.mark.parametrize("failure", [None, RuntimeError("private provider request details")])
def test_title_failure_keeps_question_and_successful_answer(workspace, monkeypatch, failure):
    env = workspace
    _, branch = create_branch(env)

    def failed_title(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(nodes, "summarize_question", failed_title)
    assert ask(env, branch["id"], QUESTION)[-1]["done"]
    result = wait_title(env, branch["id"], "fallback")
    assert result["title"] == fallback_title(QUESTION)
    assert result["status"] == "complete" and result["error"] is None
    assert "private provider" not in json.dumps(result)


def test_title_deadline_ignores_late_provider_result(workspace, monkeypatch):
    env = workspace
    _, branch = create_branch(env)
    monkeypatch.setattr(nodes, "TITLE_DEADLINE", 0.05)
    started, release, _ = held_title(env, monkeypatch)
    assert ask(env, branch["id"], QUESTION)[-1]["done"]
    assert started.wait(1)
    assert wait_title(env, branch["id"], "fallback")["title"] == fallback_title(QUESTION)
    release.set()
    for worker in env.workers:
        worker.join(3)
    result = env.client.get(f"/nodes/{branch['id']}").json()
    assert result["title_state"] == "fallback"
    assert result["title"] != AI_TITLE
    assert branch["id"] not in nodes._TITLE_JOBS


def test_title_commit_failure_can_settle_at_deadline_without_repeating_model(
    workspace, monkeypatch
):
    env = workspace
    _, branch = create_branch(env)
    failed_commit = threading.Event()
    provider_calls = []

    class TransientTitleFailureSession(Session):
        def commit(self):
            # Only the title worker changing pending -> ai fails. Request and
            # answer persistence use their normal transactions throughout.
            if not failed_commit.is_set() and any(
                isinstance(node, Node)
                and node.id == branch["id"]
                and node.title_state == "ai"
                and inspect(node).attrs.title_state.history.has_changes()
                for node in self.dirty
            ):
                failed_commit.set()
                raise RuntimeError("simulated transient title persistence failure")
            return super().commit()

    def summarize(*args):
        provider_calls.append(args)
        return AI_TITLE

    monkeypatch.setattr(nodes, "Session", TransientTitleFailureSession)
    monkeypatch.setattr(nodes, "summarize_question", summarize)
    monkeypatch.setattr(nodes, "TITLE_DEADLINE", 0.5)
    assert ask(env, branch["id"], QUESTION)[-1]["done"]
    assert failed_commit.wait(1)
    result = wait_title(env, branch["id"], "fallback")
    assert result["title"] == fallback_title(QUESTION)
    assert result["status"] == "complete" and result["error"] is None
    assert len(provider_calls) == 1
    assert branch["id"] not in nodes._TITLE_JOBS


def test_request_replay_and_title_endpoint_do_not_duplicate_title_calls(workspace, monkeypatch):
    env = workspace
    _, branch = create_branch(env)
    started, release, calls = held_title(env, monkeypatch)
    assert ask(env, branch["id"], QUESTION, request_id="replay-question")[-1]["done"]
    assert started.wait(1)
    assert ask(env, branch["id"], QUESTION, request_id="replay-question")[-1]["replayed"]
    response = env.client.post(f"/nodes/{branch['id']}/title", json={})
    assert response.status_code == 200
    assert len(calls) == 1
    release.set()
    wait_title(env, branch["id"], "ai")
    assert env.client.post(f"/nodes/{branch['id']}/title", json={}).status_code == 200
    assert len(calls) == 1
    with Session(env.engine) as session:
        assert len(service.get_messages(session, branch["id"])) == 2
        assert len(session.exec(select(Node)).all()) == 2


def test_title_repair_uses_existing_first_question_without_new_messages(workspace, monkeypatch):
    env = workspace
    _, branch = create_branch(env)
    with Session(env.engine) as session:
        node = session.get(Node, branch["id"])
        node.title, node.title_state = SOURCE[:24], "legacy"
        session.add(node)
        session.add(Message(node_id=node.id, role="user", content=QUESTION))
        session.add(Message(node_id=node.id, role="assistant", content="Saved answer"))
        session.add(Message(node_id=node.id, role="user", content="A later follow-up question"))
        session.commit()
        before = [message.model_dump() for message in service.get_messages(session, node.id)]
    _, release, calls = held_title(env, monkeypatch)
    response = env.client.post(f"/nodes/{branch['id']}/title", json={})
    assert response.status_code == 200
    assert response.json()["title"] == fallback_title(QUESTION)
    assert env.client.post(f"/nodes/{branch['id']}/title", json={}).status_code == 200
    release.set()
    assert wait_title(env, branch["id"], "ai")["title"] == AI_TITLE
    assert len(calls) == 1 and calls[0][1] == QUESTION
    with Session(env.engine) as session:
        assert [
            message.model_dump() for message in service.get_messages(session, node.id)
        ] == before


def test_custom_title_and_unasked_branch_never_invoke_title_provider(workspace):
    env = workspace
    tree, branch = create_branch(env, title="My chosen title")
    assert ask(env, branch["id"], QUESTION)[-1]["done"]
    result = env.client.get(f"/nodes/{branch['id']}").json()
    assert result["title"] == "My chosen title" and result["title_state"] == "manual"
    assert env.client.post(f"/nodes/{branch['id']}/title", json={}).status_code == 409
    empty = env.client.post(
        f"/nodes/{tree['root_node_id']}/branch", json={"seed_text": SOURCE}
    ).json()
    assert env.client.post(f"/nodes/{empty['id']}/title", json={}).status_code == 409
    assert env.client.post(f"/nodes/{tree['root_node_id']}/title", json={}).status_code == 409


def test_mock_model_retains_question_without_ai_title_call(workspace):
    env = workspace
    _, branch = create_branch(env)
    with Session(env.engine) as session:
        model = session.exec(select(ModelConfig)).first()
        model.base_url = model.api_key = "mock"
        session.add(model)
        session.commit()
    assert ask(env, branch["id"], QUESTION)[-1]["done"]
    result = wait_title(env, branch["id"], "fallback")
    assert result["title"] == fallback_title(QUESTION)
    assert nodes._TITLE_JOBS == {} and env.timers == []


@pytest.mark.parametrize("delete_tree", [False, True])
def test_deleted_branch_and_reused_id_ignore_old_title_result(workspace, monkeypatch, delete_tree):
    env = workspace
    tree, branch = create_branch(env)
    _, release, _ = held_title(env, monkeypatch)
    assert ask(env, branch["id"], QUESTION)[-1]["done"]
    path = f"/trees/{tree['id']}" if delete_tree else f"/nodes/{branch['id']}"
    assert env.client.delete(path).status_code == 200
    assert branch["id"] not in nodes._TITLE_JOBS
    if delete_tree:
        _, replacement = create_branch(env)
    else:
        replacement = env.client.post(
            f"/nodes/{tree['root_node_id']}/branch", json={"seed_text": SOURCE}
        ).json()
    # SQLite reuses the greatest deleted ID. A new job must be allowed to run,
    # and its successful title must survive the old provider eventually returning.
    assert replacement["id"] == branch["id"]
    monkeypatch.setattr(nodes, "summarize_question", lambda *a: "Replacement summary")
    assert ask(env, replacement["id"], "Replacement question")[-1]["done"]
    assert wait_title(env, replacement["id"], "ai")["title"] == "Replacement summary"
    release.set()
    for worker in env.workers:
        worker.join(3)
    with Session(env.engine) as session:
        replacement = session.get(Node, branch["id"])
        assert replacement.title == "Replacement summary"
        assert replacement.title_state == "ai"


def test_imported_titles_stay_explicit_and_export_omits_internal_state(workspace, monkeypatch):
    env = workspace
    tree, branch = create_branch(env)
    monkeypatch.setattr(nodes, "summarize_question", lambda *a: AI_TITLE)
    ask(env, branch["id"], QUESTION)
    wait_title(env, branch["id"], "ai")
    backup = env.client.get(f"/trees/{tree['id']}/export").json()
    assert all("title_state" not in node for node in backup["nodes"])
    imported = env.client.post("/trees/import", json=backup)
    assert imported.status_code == 200, imported.text
    imported_nodes = env.client.get(f"/trees/{imported.json()['id']}").json()
    imported_branch = next(node for node in imported_nodes if node["kind"] == "branch")
    assert imported_branch["title"] == AI_TITLE and imported_branch["title_state"] == "manual"
    assert imported_branch["source_message_id"] != branch["source_message_id"]
    assert env.client.post(f"/nodes/{imported_branch['id']}/title", json={}).status_code == 409


@pytest.mark.parametrize("custom_title", ["A deliberate custom title", "评估框架"])
def test_restart_falls_back_pending_titles_and_preserves_custom_titles(workspace, custom_title):
    env = workspace
    _, branch = create_branch(env)
    with Session(env.engine) as session:
        node = session.get(Node, branch["id"])
        node.title, node.title_state, node.status = "Old pending label", "pending", "complete"
        session.add(node)
        session.add(Message(node_id=node.id, role="user", content=QUESTION))
        custom = Node(
            tree_id=node.tree_id,
            parent_id=node.parent_id,
            kind="branch",
            seed_text=custom_title if len(custom_title) <= 24 else SOURCE,
            title=custom_title,
            title_state="legacy",
        )
        session.add(custom)
        session.commit()
        custom_id = custom.id
    database.init_db()
    database.init_db()
    result = env.client.get(f"/nodes/{branch['id']}").json()
    assert result["title"] == fallback_title(QUESTION) and result["title_state"] == "fallback"
    assert result["status"] == "complete"
    custom = env.client.get(f"/nodes/{custom_id}").json()
    assert custom["title"] == custom_title
    assert custom["title_state"] == "manual"
    assert env.client.post(f"/nodes/{custom_id}/title", json={}).status_code == 409


def test_additive_title_migration_repairs_old_auto_title_without_moving_messages(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-branch.db'}",
        connect_args={"check_same_thread": False},
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
                "CREATE TABLE node (id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL, parent_id INTEGER, "
                "title TEXT NOT NULL, seed_text TEXT, summary TEXT, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE message (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL, role TEXT NOT NULL, "
                "content TEXT NOT NULL, answered_by TEXT, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO knowledgetree VALUES (1, 'Legacy tree', '2026-01-01')")
        )
        connection.execute(
            text(
                "INSERT INTO node VALUES (1,1,NULL,'Root',NULL,NULL,'2026-01-01'), "
                "(2,1,1,:title,:source,'Keep summary','2026-01-01')"
            ),
            {"title": SOURCE[:24], "source": SOURCE},
        )
        connection.execute(
            text(
                "INSERT INTO message VALUES (1,1,'assistant',:source,NULL,'2026-01-01'), "
                "(2,2,'user',:question,NULL,'2026-01-01'), "
                "(3,2,'assistant','Saved branch answer',NULL,'2026-01-01')"
            ),
            {"source": SOURCE, "question": QUESTION},
        )
    monkeypatch.setattr(database, "engine", engine)
    try:
        database.init_db()
        database.init_db()
        with Session(engine) as session:
            branch = session.get(Node, 2)
            assert branch.title == fallback_title(QUESTION) and branch.title_state == "fallback"
            assert branch.parent_id == 1 and branch.seed_text == SOURCE
            assert branch.summary == "Keep summary"
            messages = session.exec(select(Message).order_by(Message.id)).all()
            assert [(m.id, m.node_id, m.content) for m in messages] == [
                (1, 1, SOURCE),
                (2, 2, QUESTION),
                (3, 2, "Saved branch answer"),
            ]
    finally:
        engine.dispose()
