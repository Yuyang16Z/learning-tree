"""Stream viewers are woken by the producer instead of polling the event log."""

import asyncio
import json
import os
import threading
import time

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.main as main
import app.service as service
from app.models import ModelConfig
from app.routers import nodes
from app.routers.nodes import Generation, _follow


@pytest.fixture(autouse=True)
def slow_recheck(monkeypatch):
    # With a 30 s re-check, delivery inside these tests can only come from a wake-up.
    monkeypatch.setattr(nodes, "FOLLOW_RECHECK_SECONDS", 30.0)


def _payload(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame[6:])


def test_event_emitted_by_a_worker_thread_reaches_a_waiting_viewer():
    generation = Generation(request_id="r1")

    async def run() -> float:
        viewer = _follow(generation, 1, {"opening": True})
        assert _payload(await viewer.__anext__()) == {"opening": True}
        pending = asyncio.ensure_future(viewer.__anext__())
        await asyncio.sleep(0.05)  # The viewer is now asleep on its asyncio.Event.
        sent = time.monotonic()
        threading.Thread(target=generation.emit, args=({"delta": "hi"},)).start()
        frame = await asyncio.wait_for(pending, 2)
        latency = time.monotonic() - sent
        assert _payload(frame)["delta"] == "hi" and _payload(frame)["seq"] == 1
        await viewer.aclose()
        assert not generation.waiters  # Leaving unregisters the viewer.
        return latency

    assert asyncio.run(run()) < 0.5


def test_finishing_wakes_viewer_and_sends_the_outcome():
    generation = Generation(request_id="r2")

    def finish() -> None:
        with generation.lock:
            generation.status = "complete"
        generation.finished.set()
        generation.wake()

    async def run() -> None:
        viewer = _follow(generation, 7, {"opening": True})
        await viewer.__anext__()
        pending = asyncio.ensure_future(viewer.__anext__())
        await asyncio.sleep(0.05)
        threading.Thread(target=finish).start()
        outcome = _payload(await asyncio.wait_for(pending, 2))
        assert outcome["done"] is True and outcome["node_id"] == 7
        assert not generation.waiters

    asyncio.run(run())


def test_deleting_the_node_wakes_viewer_with_an_interruption():
    generation = Generation(request_id="r3")
    nodes._GENERATIONS[11] = generation

    async def run() -> None:
        viewer = _follow(generation, 11, {"opening": True})
        await viewer.__anext__()
        pending = asyncio.ensure_future(viewer.__anext__())
        await asyncio.sleep(0.05)

        def delete() -> None:
            with nodes._REQUEST_LOCK:
                nodes.discard_node_work([11])

        threading.Thread(target=delete).start()
        outcome = _payload(await asyncio.wait_for(pending, 2))
        assert outcome["status"] == "interrupted"

    try:
        asyncio.run(run())
    finally:
        nodes._GENERATIONS.pop(11, None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wakeup.db'}", connect_args={"check_same_thread": False}
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
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    engine.dispose()


def test_a_real_answer_streams_without_waiting_for_the_recheck(client):
    tree = client.post("/trees", json={"title": "wake-up"}).json()
    started = time.monotonic()
    response = client.post(
        f"/nodes/{tree['root_node_id']}/ask",
        json={"question": "why?", "request_id": "wakeup-1"},
    )
    elapsed = time.monotonic() - started
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert response.status_code == 200 and events[-1].get("done") is True
    assert any("delta" in event for event in events)
    # Every delta and the finish woke the viewer; a missed wake-up would stall for 30 s.
    assert elapsed < 10
