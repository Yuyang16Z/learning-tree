"""Answers outlive their connection, and other windows or devices learn what changed."""

import asyncio
import json
import os
import threading

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.main as main
import app.service as service
from app.changes import ChangeFeed
from app.models import ModelConfig, Node
from app.routers import nodes
from app.schemas import AskIn


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
    yield TestClient(main.app), engine
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    main.app.dependency_overrides.clear()
    engine.dispose()


def events(response):
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def root_of(client):
    return client.post("/trees", json={"title": "多设备"}).json()["root_node_id"]


def leave_after_first_event(engine, node_id, request_id, **body):
    """Open an answer, read one event and drop the connection, like a phone locking."""
    with Session(engine) as session:
        response = nodes.ask(
            node_id, AskIn(question="问题", request_id=request_id, **body), session
        )

    async def read_then_close():
        first = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return json.loads(first[len("data: ") :])

    return asyncio.run(read_then_close())


def paced(release: threading.Event, produced: threading.Event, chunks=("第一段", "，第二段")):
    def stream(*args, **kwargs):
        yield "text", chunks[0]
        produced.set()
        assert release.wait(3)
        for chunk in chunks[1:]:
            yield "text", chunk

    return stream


def test_leaving_keeps_the_answer_running_and_remembers_it(setup, monkeypatch):
    client, engine = setup
    with Session(engine) as session:
        real = ModelConfig(
            label="真实", base_url="https://model.invalid", llm_model="m", api_key="k"
        )
        session.add(real)
        session.commit()
        real_id = real.id
    root = root_of(client)
    release, produced, remembered = threading.Event(), threading.Event(), []
    monkeypatch.setattr(nodes, "stream_chat", paced(release, produced))
    monkeypatch.setattr(nodes, "fetch_memory_note", lambda *a, **k: "")
    monkeypatch.setattr(nodes, "extract_and_save", lambda *a: remembered.append(a[-1]))

    started = leave_after_first_event(engine, root, "phone", config_id=real_id)
    assert started["started"] and started["node_id"] == root
    generation = nodes._GENERATIONS[root]
    assert produced.wait(3) and not generation.stop.is_set()
    release.set()
    assert generation.finished.wait(3)
    with Session(engine) as session:
        assert session.get(Node, root).status == "complete"
        assert service.get_messages(session, root)[-1].content == "第一段，第二段"
    assert remembered == ["第一段，第二段"]
    assert root not in nodes._GENERATIONS


def test_reattach_replays_only_missed_events_then_finishes(setup, monkeypatch):
    client, engine = setup
    root = root_of(client)
    release, produced = threading.Event(), threading.Event()
    monkeypatch.setattr(nodes, "stream_chat", paced(release, produced, ("A", "B", "C")))
    leave_after_first_event(engine, root, "resume-me")
    assert produced.wait(3)
    with Session(engine) as session:
        response = nodes.follow(root, "resume-me", 1, session)

    async def attach_then_release():
        # Attach while the answer is still running, then let it continue.
        followed = [json.loads((await anext(response.body_iterator))[len("data: ") :])]
        release.set()
        async for chunk in response.body_iterator:
            followed.append(json.loads(chunk[len("data: ") :]))
        return followed

    followed = asyncio.run(attach_then_release())
    assert followed[0]["started"] and followed[0]["resumed"] and followed[0]["node_id"] == root
    assert [(e["seq"], e["delta"]) for e in followed if "delta" in e] == [(2, "B"), (3, "C")]
    assert followed[-1]["done"] and followed[-1]["message_id"]

    # Once finished, reattaching reports the stored result instead of replaying text.
    again = events(client.get(f"/nodes/{root}/stream", params={"request_id": "resume-me"}))
    assert [e.get("done") for e in again] == [None, True]
    assert again[-1]["message_id"] == followed[-1]["message_id"]


def test_reattach_finds_a_new_follow_up_by_request_and_rejects_unknown_requests(setup, monkeypatch):
    client, engine = setup
    root = root_of(client)
    monkeypatch.setattr(nodes, "stream_chat", lambda *a, **k: iter([("text", "第一问的回答")]))
    events(client.post(f"/nodes/{root}/ask", json={"question": "第一问"}))
    release, produced = threading.Event(), threading.Event()
    monkeypatch.setattr(nodes, "stream_chat", paced(release, produced))
    started = leave_after_first_event(engine, root, "follow-up")
    assert started["node_id"] != root
    release.set()
    followed = events(client.get(f"/nodes/{root}/stream", params={"request_id": "follow-up"}))
    assert followed[0]["node_id"] == started["node_id"] and followed[-1]["done"]

    missing = client.get(f"/nodes/{root}/stream", params={"request_id": "never-sent"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["status"] == "missing"


def test_stop_still_ends_the_answer_for_every_viewer(setup, monkeypatch):
    client, engine = setup
    root = root_of(client)
    release, produced = threading.Event(), threading.Event()
    monkeypatch.setattr(nodes, "stream_chat", paced(release, produced))
    leave_after_first_event(engine, root, "to-stop")
    generation = nodes._GENERATIONS[root]
    assert produced.wait(3)
    followed = []
    viewer = threading.Thread(
        target=lambda: followed.extend(
            events(client.get(f"/nodes/{root}/stream", params={"request_id": "to-stop"}))
        )
    )
    viewer.start()
    assert client.post(f"/nodes/{root}/stop", json={"request_id": "to-stop"}).status_code == 200
    viewer.join(3)
    release.set()
    assert not viewer.is_alive() and generation.finished.wait(3)
    assert followed[-1]["status"] == "interrupted"
    with Session(engine) as session:
        node = session.get(Node, root)
        assert node.status == "interrupted"
        assert service.get_messages(session, root)[-1].content == "第一段"


def test_change_feed_reports_ids_by_scope(setup, monkeypatch):
    client, engine = setup
    first = client.get("/api/changes").json()
    assert first["reload"]
    cursor = first["cursor"]

    def since():
        nonlocal cursor
        notice = client.get("/changes", params={"since": cursor}).json()
        cursor = notice["cursor"]
        return notice

    assert not since()["reload"]
    created = client.post("/trees", json={"title": "新主题"}).json()
    notice = since()
    assert notice["topics"] and not notice["reload"] and not notice["models"]
    assert notice["trees"] == {str(created["id"]): [created["root_node_id"]]}

    monkeypatch.setattr(nodes, "stream_chat", lambda *a, **k: iter([("text", "回答")]))
    events(client.post(f"/nodes/{created['root_node_id']}/ask", json={"question": "问"}))
    notice = since()
    assert notice["trees"] == {str(created["id"]): [created["root_node_id"]]}
    assert not notice["topics"]

    client.post("/mcp", json={"label": "工具", "command": "echo", "args": [], "enabled": False})
    with Session(engine) as session:
        session.add(ModelConfig(label="另一个", base_url="mock", llm_model="m", api_key="mock"))
        session.commit()
    notice = since()
    assert notice["mcp"] and notice["models"] and not notice["trees"]

    client.delete("/memories")  # Bulk statements report their table.
    assert since()["memory"]

    with Session(engine) as session:
        session.add(ModelConfig(label="未提交", base_url="mock", llm_model="m", api_key="mock"))
        session.flush()
        session.rollback()
    notice = since()
    assert not notice["models"] and not notice["reload"]


def test_unknown_or_expired_cursors_ask_for_a_reload():
    feed = ChangeFeed(history=2)
    start = feed.since("")["cursor"]
    for tree_id in (1, 2, 3):
        feed.publish({("node", tree_id, 10 + tree_id)})
    assert feed.since(start)["reload"]
    assert feed.since("other-instance:3")["reload"]
    assert feed.since(f"{feed.instance}:99")["reload"]
    recent = feed.since(f"{feed.instance}:1")
    assert not recent["reload"] and recent["trees"] == {"2": [12], "3": [13]}
    current = feed.since(recent["cursor"])
    assert (
        not current["reload"] and current["trees"] == {} and current["cursor"] == recent["cursor"]
    )
