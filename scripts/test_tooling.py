"""Safety boundaries of the developer launcher and optional MCP wrappers."""

import json
import sqlite3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    spec = spec_from_file_location(Path(relative).stem, ROOT / relative)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dev_overrides_production_settings(tmp_path, monkeypatch):
    manage = load("scripts/manage.py")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///personal.db")
    monkeypatch.setenv("DEFAULT_API_KEY", "private-provider-key")
    monkeypatch.setenv("VITE_API_BASE", "http://127.0.0.1:8099/api")
    env = manage.isolated_env(tmp_path / "dev.db")
    assert env["DATABASE_URL"] == f"sqlite:///{(tmp_path / 'dev.db').as_posix()}"
    assert env["DEFAULT_API_KEY"] == "mock"
    assert env["VITE_API_BASE"] == "/api"


def test_dev_refuses_symlinked_database(tmp_path):
    manage = load("scripts/manage.py")
    manage.ROOT = tmp_path
    (tmp_path / ".runtime").mkdir()
    target = tmp_path / "personal.db"
    target.touch()
    (tmp_path / ".runtime/dev.db").symlink_to(target)
    with pytest.raises(RuntimeError, match="alias"):
        manage.dev_database()


def test_backup_handles_filename_uri_characters(tmp_path):
    backup = load("scripts/backup_data.py")
    source = tmp_path / "notes #1?.db"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE note (body TEXT)")
        database.execute("INSERT INTO note VALUES ('portable')")
    target = backup.backup(source, tmp_path / "copies")
    with sqlite3.connect(target) as database:
        assert database.execute("SELECT body FROM note").fetchone() == ("portable",)


def test_mcp_does_not_inherit_model_keys(monkeypatch):
    launch = load("integrations/mcp/launch.py")
    monkeypatch.setenv("DEFAULT_API_KEY", "private-provider-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-provider-key")
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider-key")
    monkeypatch.setenv("TAVILY_API_KEY", "private-search-key")
    env = launch.server_environment()
    assert not any(key.endswith("API_KEY") for key in env)
    assert "PATH" in env


def test_filesystem_only_allows_learning_library(tmp_path, monkeypatch):
    launch = load("integrations/mcp/launch.py")
    launch.DATA = tmp_path / "state"
    launch.LIBRARY = tmp_path / "学习资料"
    launch.PACKAGES = tmp_path / "packages"
    entry = launch.PACKAGES / "@modelcontextprotocol/server-filesystem/dist/index.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.setattr(launch.shutil, "which", lambda *args, **kwargs: "/test/node")
    args, env, cwd = launch.command_for("filesystem")
    assert args[2:] == [str(launch.LIBRARY)]
    assert cwd == launch.DATA
    assert str(tmp_path) not in args


def test_occupied_port_does_not_reuse_existing_server():
    import socket

    manage = load("scripts/manage.py")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with pytest.raises(RuntimeError, match="already in use"):
            manage.require_free_port(server.getsockname()[1])


@pytest.mark.parametrize(
    "existing_path",
    [
        "/old/project/integrations/mcp/launch.py",
        r"C:\Learning Tree\integrations\mcp\launch.py",
        r"C:\Learning Tree/integrations\mcp/launch.py",
    ],
)
def test_mcp_registration_is_idempotent_across_path_formats(tmp_path, monkeypatch, existing_path):
    register = load("integrations/mcp/register.py")
    directory = tmp_path / "integrations/mcp"
    directory.mkdir(parents=True)
    (directory / "catalog.json").write_text(
        json.dumps([{"key": "fetch", "label": "Web reading"}, {"key": "time", "label": "Time"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(register, "ROOT", tmp_path)
    monkeypatch.setattr(register, "__file__", str(directory / "register.py"))
    monkeypatch.setattr(register.subprocess, "run", lambda *args, **kwargs: None)
    custom = {
        "id": 90,
        "label": "My own service",
        "args": ["/custom/launch.py", "fetch"],
        "enabled": True,
    }
    servers = [
        {"id": 1, "label": "Old preset", "args": [existing_path, "fetch"], "enabled": False},
        custom.copy(),
    ]
    writes = []

    def fake_api(path, body=None, method=None):
        if path == "/health":
            return {"name": "学习树"}
        if body is None:
            assert path == "/mcp"
            return servers.copy()
        writes.append((path, method))
        if method == "PUT":
            server_id = int(path.rsplit("/", 1)[1])
            index = next(i for i, server in enumerate(servers) if server["id"] == server_id)
            servers[index] = {"id": server_id, **body}
        else:
            assert method == "POST"
            servers.append({"id": 100, **body})
        return {}

    monkeypatch.setattr(register, "api", fake_api)
    register.main()
    assert len(servers) == 3
    assert next(server for server in servers if server["id"] == 1)["enabled"] is False
    # A user may disable a newly registered preset before registering again.
    next(server for server in servers if server["id"] == 100)["enabled"] = False
    register.main()
    assert writes == [("/mcp/1", "PUT"), ("/mcp", "POST"), ("/mcp/1", "PUT"), ("/mcp/100", "PUT")]
    assert len(servers) == 3
    assert next(server for server in servers if server["id"] == 90) == custom
    assert all(not server["enabled"] for server in servers if server["id"] != 90)
