"""Smoke-test a running LearningTree container with a mock model (standard library only).

Usage: python3 scripts/docker_smoke.py http://127.0.0.1:8099
The container must be started with DEFAULT_API_KEY=mock so answers come from the
deterministic mock model; no request leaves the machine.
"""

import json
import sys
import urllib.error
import urllib.request


def request(base: str, path: str, *, method: str = "GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def main(base: str) -> None:
    status, text = request(base, "/health")
    assert status == 200 and json.loads(text)["status"] == "ok", (status, text)

    status, text = request(base, "/")
    assert status == 200 and "<div id=" in text, "built web app is not served"

    status, _ = request(base, "/trees", headers={"Host": "attacker.example"})
    assert status == 400, f"foreign Host header accepted ({status})"

    status, text = request(base, "/trees", method="POST", body={"title": "container smoke"})
    assert status == 200, (status, text)
    tree = json.loads(text)

    status, text = request(
        base,
        f"/nodes/{tree['root_node_id']}/ask",
        method="POST",
        body={"question": "What is a container?", "request_id": "docker-smoke"},
    )
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert status == 200 and events and events[-1].get("done") is True, (status, events[-1:])
    assert any("delta" in event for event in events), "no streamed answer text"

    status, text = request(base, f"/trees/{tree['id']}")
    assert status == 200 and json.loads(text)[0]["status"] == "complete", (status, text)
    print("container smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099")
