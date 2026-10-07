"""Reproducible local benchmarks for topic loading and stream relaying.

Run from the repository root:  uv run python scripts/benchmark.py
It uses a temporary database and touches no personal data. Results depend on the machine;
compare commits on the same machine, not absolute numbers across machines.
"""

import asyncio
import os
import platform
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="learning-tree-bench-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'bench.db'}"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlmodel import Session  # noqa: E402

import app.db as database  # noqa: E402
from app.main import app  # noqa: E402
from app.models import KnowledgeTree, Message, Node  # noqa: E402
from app.routers import nodes  # noqa: E402

TREE_NODES = 2000
IMAGE_EVERY = 10  # One question in ten carries an image.
IMAGE_BYTES = 150_000
LOAD_RUNS = 20
STREAM_EVENTS = 300
IDLE_SECONDS = 2.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def seed_topic() -> int:
    rng = random.Random(7)
    image = "data:image/png;base64," + "A" * IMAGE_BYTES
    answer = "答案" * 600
    with Session(database.engine) as session:
        session.add(KnowledgeTree(id=1, title="benchmark"))
        for offset in range(TREE_NODES):
            node_id = offset + 1
            parent = None if offset == 0 else rng.randint(1, offset)
            session.add(Node(id=node_id, tree_id=1, parent_id=parent, title=f"node {offset}"))
            session.add(
                Message(
                    node_id=node_id,
                    role="user",
                    content=f"question {offset}",
                    images=[image] if offset % IMAGE_EVERY == 0 else None,
                )
            )
            session.add(Message(node_id=node_id, role="assistant", content=answer))
        session.commit()
    return 1


def bench_topic_load(client: TestClient, tree_id: int) -> dict:
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    for _ in range(3):  # Warm caches and connections.
        assert client.get(f"/trees/{tree_id}").status_code == 200
    event.listen(database.engine, "before_cursor_execute", record)
    try:
        client.get(f"/trees/{tree_id}")
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    timings = []
    for _ in range(LOAD_RUNS):
        started = time.perf_counter()
        response = client.get(f"/trees/{tree_id}")
        timings.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200 and len(response.json()) == TREE_NODES
    return {
        "median_ms": statistics.median(timings),
        "p95_ms": _percentile(timings, 0.95),
        "queries": sum(1 for s in statements if s.lstrip().upper().startswith("SELECT")),
    }


class _CountingLock:
    """Counts how often the viewer reads the log; works with any Generation version."""

    def __init__(self, inner):
        self.inner = inner
        self.count = 0

    def __enter__(self):
        self.count += 1
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)

    def acquire(self, *args, **kwargs):
        return self.inner.acquire(*args, **kwargs)

    def release(self):
        return self.inner.release()


async def _relay(generation, latencies: list[float], idle_reads: list[int]) -> None:
    viewer = nodes._follow(generation, 1, {"opening": True})
    await viewer.__anext__()
    lock = generation.lock
    # Idle phase: nothing is emitted; count how often the viewer wakes to read the log.
    counting = _CountingLock(lock)
    generation.lock = counting
    pending = asyncio.ensure_future(viewer.__anext__())
    await asyncio.sleep(IDLE_SECONDS)
    idle_reads.append(counting.count)
    generation.lock = lock

    def produce() -> None:
        rng = random.Random(11)
        for _ in range(STREAM_EVENTS):
            time.sleep(rng.uniform(0.001, 0.02))
            generation.emit({"delta": "x", "sent": time.perf_counter()})
        generation.finished.set()
        if hasattr(generation, "wake"):
            generation.wake()

    threading.Thread(target=produce, daemon=True).start()
    import json

    frame = await pending
    while True:
        payload = json.loads(frame[6:])
        if "sent" in payload:
            latencies.append((time.perf_counter() - payload["sent"]) * 1000)
        if "delta" not in payload:
            break
        frame = await viewer.__anext__()
    await viewer.aclose()


def bench_stream_relay() -> dict:
    generation = nodes.Generation(request_id="bench")
    latencies: list[float] = []
    idle_reads: list[int] = []
    asyncio.run(_relay(generation, latencies, idle_reads))
    return {
        "events": len(latencies),
        "mean_ms": statistics.mean(latencies),
        "p95_ms": _percentile(latencies, 0.95),
        "idle_reads_per_s": idle_reads[0] / IDLE_SECONDS,
    }


def main() -> None:
    with TestClient(app) as client:
        tree_id = seed_topic()
        load = bench_topic_load(client, tree_id)
    relay = bench_stream_relay()
    print(f"machine: {platform.platform()} | python {platform.python_version()}")
    print(
        f"topic load ({TREE_NODES} nodes, {2 * TREE_NODES} messages, "
        f"1 in {IMAGE_EVERY} questions with a {IMAGE_BYTES // 1000} KB image):"
    )
    print(
        f"  median {load['median_ms']:.1f} ms | p95 {load['p95_ms']:.1f} ms | "
        f"{load['queries']} SELECT statements per request"
    )
    print(f"stream relay ({relay['events']} events from a worker thread):")
    print(
        f"  mean latency {relay['mean_ms']:.2f} ms | p95 {relay['p95_ms']:.2f} ms | "
        f"idle viewer reads {relay['idle_reads_per_s']:.1f}/s"
    )


if __name__ == "__main__":
    main()
