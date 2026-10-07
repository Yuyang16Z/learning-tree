# Performance measurements

`scripts/benchmark.py` measures two paths on a temporary database with no personal data:

```bash
uv run python scripts/benchmark.py
```

- **Topic load**: `GET /trees/{id}` for a topic with 2,000 nodes and 4,000 messages, where one question in ten carries a 150 KB image. Three warm-up requests, then 20 timed requests through the ASGI test client; the script also counts SELECT statements for one request.
- **Stream relay**: one viewer follows an answer while a worker thread emits 300 events at random 1–20 ms intervals. Latency is the time from `emit` to the viewer handling the event. Before the events start, the viewer waits 2 s with nothing to send, and the script counts how often it reads the log.

## Results (2026-10-07)

Apple M3, macOS 27, Python 3.11.15. Each commit was run three times on the same machine; the table shows the median of the three runs (the range is in brackets).

| Measurement | Before (`e4cdb0c`) | After | Change |
|---|---|---|---|
| Topic load, median | 243.9 ms [234.3–246.4] | 22.1 ms [21.6–22.3] | about 11× faster |
| Topic load, p95 | 277.5 ms [275.7–328.5] | 58.2 ms [55.2–60.3] | |
| SELECT statements per topic load | 2,002 | 3 | constant in topic size |
| Stream relay, mean latency | 12.98 ms [12.92–13.52] | 0.69 ms [0.19–0.72] | |
| Stream relay, p95 latency | 25.51 ms [25.03–26.17] | 1.87 ms [0.50–2.10] | |
| Idle viewer log reads | 37.5 /s [36.0–37.5] | 1.5 /s | |

## What changed

- **Topic load.** The listing only needs to know whether each node has a message, but it ran one query per node and loaded every message body, including image data URLs. It now finds answered nodes with one grouped query, and the topic list finds every root node with one grouped query. `tests/test_tree_listing_queries.py` keeps the query count constant as topics grow.
- **Stream relay.** Each viewer polled the event log every 25 ms. A viewer now waits on an `asyncio.Event` that the producer thread sets through `call_soon_threadsafe` whenever it appends an event, stops, finishes or the node is deleted. A 1 s re-check only guards against a missed wake-up. `tests/test_stream_wakeup.py` raises that re-check to 30 s, so its streams can finish quickly only through wake-ups.

## Limits

These are single-machine, in-process numbers through the ASGI test client, not load tests: no network, no concurrent users, and the mock database is synthetic. Compare commits on the same machine rather than reading the absolute values as server capacity.
