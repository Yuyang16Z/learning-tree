#!/usr/bin/env python3
"""Record a public, offline UI walkthrough using only synthetic learning content.

Run: uv run python scripts/record_demo.py
Requires the normal project dependencies, Playwright Chromium and ffmpeg.
No personal database, .env file, live API, retrieval model or MCP server is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets"
QUESTION = "Why do models overfit?"
BRANCH_QUESTION = "How does a validation set help?"
MAIN_ANSWER = """A model can memorize the examples it practiced instead of learning a pattern that works elsewhere. This is called **overfitting**.

Think of memorizing an answer sheet: a perfect practice score does not guarantee you can solve a new problem.

Use a **validation set** to check performance on examples the model did not train on. A widening gap between training and validation results is a useful warning sign.

Start with a simpler model, compare both scores, and adjust from there."""
BRANCH_ANSWER = """A **validation set** is a separate set of examples used to compare model choices during development.

For example, train on 800 practice examples and check each candidate on 200 validation examples. If training improves while validation gets worse, you may be fitting details that do not generalize.

Keep a separate **test set** untouched until the final evaluation. Repeatedly tuning against the same validation set can also lead to overfitting."""
EXPLANATION = (
    "A validation set is a practice check using examples kept out of training. "
    "Like a new quiz after studying, it helps you compare whether a model learned "
    "a reusable pattern. Keep the final test separate so it remains an honest last check."
)


def serve(port: int) -> None:
    """The child runs from a temporary cwd, where there is no personal .env."""
    sys.path.insert(0, str(ROOT))
    database = Path(os.environ["LEARNING_TREE_DEMO_DATABASE"]).resolve()
    if database.parent != Path.cwd() or database.exists():
        raise RuntimeError("Recording requires a new database in its temporary directory.")
    os.environ.update(
        DATABASE_URL=f"sqlite:///{database.as_posix()}",
        DEFAULT_API_KEY="mock",
        DEFAULT_LABEL="Offline demo · scripted answers",
        DEFAULT_BASE_URL="mock://offline",
        DEFAULT_LLM_MODEL="mock",
        MEMORY_RETRIEVAL_MODE="lexical",
        TAVILY_API_KEY="",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )

    def deny_connection(*_args, **_kwargs):
        raise RuntimeError("The recording server cannot make outbound network connections.")

    socket.socket.connect = deny_connection
    socket.socket.connect_ex = deny_connection

    from app import llm

    def scripted_stream(spec, system, messages, deep=False):
        if spec.api_key != "mock":
            raise RuntimeError("Recording may use only the offline demo provider.")
        question = llm._last_user_text(messages)
        answer = BRANCH_ANSWER if question == BRANCH_QUESTION else MAIN_ANSWER
        for start in range(0, len(answer), 24):
            time.sleep(0.035)
            yield "text", answer[start : start + 24]

    def scripted_complete(spec, system, messages):
        if spec.api_key != "mock":
            raise RuntimeError("Recording may use only the offline demo provider.")
        return EXPLANATION

    # Replace only the provider boundary before routers import these functions.
    # Tree creation, streaming persistence, selected-text source anchors,
    # branching, reflection saving and source navigation use real application code.
    llm.stream_chat = scripted_stream
    llm.complete = scripted_complete

    import uvicorn

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return
    for command in ("node", "npm", "ffmpeg"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is required to record the public demo.")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Keep inherited provider tokens out of both recording child processes.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    subprocess.run(
        ["npm", "--prefix", "web", "run", "build"],
        cwd=ROOT,
        env={**env, "VITE_API_BASE": "/api"},
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="learning-tree-public-demo-") as temporary:
        directory = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env.update(
            LEARNING_TREE_DEMO_DATABASE=str(directory / "demo.db"),
            LEARNING_TREE_DEMO_BASE=f"http://127.0.0.1:{port}",
            LEARNING_TREE_DEMO_OUTPUT=str(directory),
            LEARNING_TREE_DEMO="1",
            LEARNING_TREE_DEMO_QUESTION=QUESTION,
            LEARNING_TREE_DEMO_BRANCH_QUESTION=BRANCH_QUESTION,
        )
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)],
            cwd=directory,
            env=env,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError("The isolated recording server exited early.")
                try:
                    with urllib.request.urlopen(env["LEARNING_TREE_DEMO_BASE"] + "/health"):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The isolated recording server did not start.") from None
                    time.sleep(0.15)
            subprocess.run(["node", str(ROOT / "scripts/record_demo.cjs")], env=env, check=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        recording = json.loads((directory / "recording.json").read_text())
        video = directory / recording["video"]
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                str(recording["start"]),
                "-i",
                str(video),
                "-t",
                str(recording["duration"]),
                "-filter_complex",
                "fps=10,scale=1120:-1:flags=lanczos,split[a][b];"
                "[a]palettegen=max_colors=128:stats_mode=diff[p];"
                "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                "-loop",
                "0",
                str(OUTPUT / "learning-tree-demo.gif"),
            ],
            check=True,
        )
        shutil.copyfile(directory / "frame-05-source.png", OUTPUT / "learning-tree-overview.png")
        # Key frames are retained locally for visual review, never copied into Git.
        review = ROOT / "artifacts/public-demo-review"
        review.mkdir(parents=True, exist_ok=True)
        for source in directory.glob("frame-*.png"):
            shutil.copyfile(source, review / source.name)
        manifest = {
            "provider": "offline scripted answers; no live AI or external tools",
            "database": "new temporary SQLite; removed after recording",
            "duration_seconds": recording["duration"],
            "checks": recording["checks"],
            "files": {
                file.name: {
                    "bytes": file.stat().st_size,
                    "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                }
                for file in sorted(OUTPUT.glob("learning-tree-*"))
                if file.suffix in (".png", ".gif")
            },
        }
        (review / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
