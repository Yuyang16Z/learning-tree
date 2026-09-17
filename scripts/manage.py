#!/usr/bin/env python3
"""Portable local development commands. Run from any working directory with uv."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"


def executable(name: str) -> str:
    result = shutil.which(name)
    if not result:
        raise RuntimeError(f"{name} is required. Install it and run setup again.")
    return result


def run(args: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def isolated_env(database: Path) -> dict[str, str]:
    """Override .env and inherited provider settings before the app is imported."""
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{database.resolve().as_posix()}",
        "DEFAULT_API_KEY": "mock",
        "DEFAULT_LABEL": "Demo (offline)",
        "DEFAULT_BASE_URL": "https://mock.invalid/v1",
        "DEFAULT_LLM_MODEL": "mock",
        "TAVILY_API_KEY": "",
        "MEMORY_RETRIEVAL_MODE": "lexical",
        "VITE_API_BASE": "/api",
    }


def dev_database() -> Path:
    directory = ROOT / ".runtime"
    if directory.is_symlink():
        raise RuntimeError("Development data directory .runtime must not be a symlink.")
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / "dev.db"
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise RuntimeError("Development database must not alias another file.")
    return path


def require_free_port(port: int) -> None:
    with socket.socket() as sock:
        try:
            sock.bind((HOST, port))
        except OSError:
            raise RuntimeError(
                f"Port {port} is already in use; no existing server was changed."
            ) from None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def server_args(port: int, reload: bool = False) -> list[str]:
    args = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(port)]
    if reload:
        args += ["--reload", "--reload-dir", "app"]
    return args


def stop(process: subprocess.Popen) -> None:
    # npm/Vite and uvicorn reload create children; stop our entire process group.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()


@contextmanager
def background(args: list[str], env: dict[str, str]):
    process = subprocess.Popen(args, cwd=ROOT, env=env, start_new_session=os.name == "posix")
    try:
        yield process
    finally:
        stop(process)


def wait_ready(base: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Local server exited before becoming ready.")
        try:
            with urllib.request.urlopen(base + "/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.15)
    raise RuntimeError("Local server did not become ready within 30 seconds.")


def setup(with_retrieval: bool = False) -> None:
    # A regular setup must not remove an existing optional retrieval install.
    run([executable("uv"), "sync", "--frozen", "--inexact"])
    run([executable("npm"), "ci", "--prefix", "web"])
    print("Basic installation is ready; keyword memory retrieval needs no local models.")
    if with_retrieval:
        try:
            retrieval()
        except (RuntimeError, subprocess.CalledProcessError):
            print(
                "Optional semantic retrieval preparation did not finish. "
                "The basic app remains usable. Retry: uv run python scripts/manage.py retrieval",
                file=sys.stderr,
            )
    print("Run: uv run python scripts/manage.py dev (or start)")


def retrieval() -> None:
    """Explicitly install the optional runtime and prepare public model weights."""
    uv = executable("uv")
    run([uv, "sync", "--frozen", "--inexact", "--extra", "retrieval"])
    run(
        [uv, "run", "--frozen", "--extra", "retrieval", "python", "scripts/prepare_retrieval.py"],
        # An explicit install may prepare models while normal runtime remains
        # deliberately lexical. Never edit the user's .env or provider settings.
        env={**os.environ, "MEMORY_RETRIEVAL_MODE": "hybrid"},
    )
    print("Local retrieval models are prepared. Restart the app to load them.")


def build() -> None:
    run(
        [executable("npm"), "--prefix", "web", "run", "build"],
        env={**os.environ, "VITE_API_BASE": "/api"},
    )


def dev() -> None:
    require_free_port(8100)
    require_free_port(5174)
    env = isolated_env(dev_database())
    print("Development: http://127.0.0.1:5174 · separate database .runtime/dev.db", flush=True)
    with background(server_args(8100, reload=True), env) as api:
        wait_ready("http://127.0.0.1:8100", api)
        with background(
            [
                executable("npm"),
                "--prefix",
                "web",
                "run",
                "dev",
                "--",
                "--host",
                HOST,
                "--port",
                "5174",
                "--strictPort",
            ],
            env,
        ) as vite:
            while api.poll() is None and vite.poll() is None:
                time.sleep(0.3)
            if api.poll() not in (None, 0) or vite.poll() not in (None, 0):
                raise RuntimeError("A development process exited unexpectedly.")


def start(open_browser: bool = False) -> None:
    # Preserve the desktop launcher's open-existing behavior without starting a
    # second worker against the same SQLite database.
    if open_browser:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8099/health", timeout=0.5) as response:
                running = json.load(response).get("name") == "学习树"
        except (OSError, ValueError):
            running = False
        if running:
            webbrowser.open("http://127.0.0.1:8099")
            return
    require_free_port(8099)
    run([sys.executable, str(ROOT / "scripts/backup_data.py")])
    build()
    env = {**os.environ, "VITE_API_BASE": "/api"}
    with background(server_args(8099), env) as api:
        wait_ready("http://127.0.0.1:8099", api)
        if open_browser:
            webbrowser.open("http://127.0.0.1:8099")
        if api.wait() != 0:
            raise RuntimeError("Local server exited unexpectedly.")


def check() -> None:
    paths = ["app", "tests", "scripts", "examples", "integrations/mcp", "desktop/macos"]
    run([sys.executable, "-m", "ruff", "check", *paths])
    run([sys.executable, "-m", "ruff", "format", "--check", *paths])
    with tempfile.TemporaryDirectory(prefix="learning-tree-check-") as directory:
        run(
            [sys.executable, "-m", "pytest", "tests", "scripts/test_tooling.py"],
            env=isolated_env(Path(directory) / "check.db"),
        )
    run([executable("npm"), "--prefix", "web", "test"])
    build()


def e2e() -> None:
    build()
    with tempfile.TemporaryDirectory(prefix="learning-tree-e2e-") as directory:
        env = isolated_env(Path(directory) / "test.db")
        port = free_port()
        base = f"http://{HOST}:{port}"
        env.update(
            LEARNING_TREE_E2E="1",
            LEARNING_TREE_E2E_BASE=base,
            LEARNING_TREE_E2E_PYTHON=sys.executable,
        )
        with background(server_args(port), env) as api:
            wait_ready(base, api)
            run([executable("node"), "scripts/smoke.cjs"], env=env)
            run([executable("node"), "scripts/document_smoke.cjs"], env=env)
            run([executable("node"), "scripts/memory_smoke.cjs"], env=env)
            run([executable("node"), "scripts/topic_smoke.cjs"], env=env)
            run([executable("node"), "scripts/chat_navigation_smoke.cjs"], env=env)
            run([executable("node"), "scripts/node_deletion_smoke.cjs"], env=env)
            run([executable("node"), "scripts/web_tools_smoke.cjs"], env=env)


def main() -> None:
    os.chdir(ROOT)
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("setup", "retrieval", "dev", "start", "check", "e2e", "build", "backup")
    )
    parser.add_argument("--open", action="store_true", help="Open the browser after start is ready")
    parser.add_argument(
        "--with-retrieval",
        action="store_true",
        help="Also install optional local models during setup",
    )
    args = parser.parse_args()
    if args.with_retrieval and args.command != "setup":
        parser.error("--with-retrieval is only valid with setup")
    try:
        if args.command == "setup":
            setup(args.with_retrieval)
        elif args.command == "start":
            start(args.open)
        elif args.command == "backup":
            run([sys.executable, str(ROOT / "scripts/backup_data.py")])
        else:
            globals()[args.command]()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
