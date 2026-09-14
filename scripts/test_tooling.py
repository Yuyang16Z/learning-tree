"""Safety boundaries of the developer launcher and optional MCP wrappers."""

import json
import sqlite3
import subprocess
import tomllib
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


def test_basic_dependency_manifest_excludes_local_model_runtime():
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = manifest["project"]
    heavy = ("torch", "transformers", "sentence-transformers")
    assert not any(name in requirement for requirement in project["dependencies"] for name in heavy)
    retrieval = project["optional-dependencies"]["retrieval"]
    assert any(requirement.startswith("sentence-transformers") for requirement in retrieval)


def test_default_setup_never_prepares_models_or_removes_installed_extras(monkeypatch):
    manage = load("scripts/manage.py")
    commands = []
    monkeypatch.setattr(manage, "executable", lambda name: name)
    monkeypatch.setattr(manage, "run", lambda args, **kwargs: commands.append(args))
    monkeypatch.setattr(manage, "retrieval", lambda: pytest.fail("unsolicited model installation"))
    manage.setup()
    assert ["uv", "sync", "--frozen", "--inexact"] in commands
    assert ["npm", "ci", "--prefix", "web"] in commands
    assert not any(
        "retrieval" in part or "prepare_retrieval" in part for args in commands for part in args
    )


@pytest.mark.parametrize("failure", ["dependencies", "weights"])
def test_optional_setup_failure_keeps_basic_app_ready_and_is_retryable(
    monkeypatch, capsys, failure
):
    manage = load("scripts/manage.py")
    commands = []
    monkeypatch.setattr(manage, "executable", lambda name: name)

    def failing_run(args, **kwargs):
        commands.append(args)
        if (failure == "dependencies" and "--extra" in args) or (
            failure == "weights" and "scripts/prepare_retrieval.py" in args
        ):
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(manage, "run", failing_run)
    manage.setup(with_retrieval=True)
    output = capsys.readouterr()
    assert "Basic installation is ready" in output.out
    assert "basic app remains usable" in output.err
    assert "scripts/manage.py retrieval" in output.err
    assert commands.index(["npm", "ci", "--prefix", "web"]) < next(
        index for index, args in enumerate(commands) if "--extra" in args
    )
    with pytest.raises(subprocess.CalledProcessError):
        manage.retrieval()


@pytest.mark.parametrize("state", ["not_installed", "degraded", "disabled"])
def test_retrieval_smoke_is_nonzero_when_inference_cannot_be_verified(monkeypatch, state):
    prepare = load("scripts/prepare_retrieval.py")
    monkeypatch.setattr(prepare.sys, "argv", ["prepare_retrieval.py", "--smoke"])
    monkeypatch.setattr(
        prepare,
        "prepare",
        lambda: {"state": state, "embedding_ready": False, "reranker_ready": False},
    )
    monkeypatch.setattr(prepare, "smoke", lambda: pytest.fail("inference with unavailable models"))
    assert prepare.main() == 1


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
    launch.LIBRARY = tmp_path / "learning-materials"
    launch.LEGACY_LIBRARY = tmp_path / "学习资料"
    launch.PACKAGES = tmp_path / "packages"
    entry = launch.PACKAGES / "@modelcontextprotocol/server-filesystem/dist/index.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.setattr(launch.shutil, "which", lambda *args, **kwargs: "/test/node")
    args, env, cwd = launch.command_for("filesystem")
    assert args[2:] == [str(launch.LIBRARY)]
    assert cwd == launch.DATA
    assert str(tmp_path) not in args


def test_existing_learning_files_stay_accessible_without_merging(tmp_path, monkeypatch):
    launch = load("integrations/mcp/launch.py")
    launch.DATA = tmp_path / "state"
    launch.LIBRARY = tmp_path / "learning-materials"
    launch.LEGACY_LIBRARY = tmp_path / "学习资料"
    launch.PACKAGES = tmp_path / "packages"
    for directory, content in [(launch.LIBRARY, "new"), (launch.LEGACY_LIBRARY, "legacy")]:
        directory.mkdir()
        (directory / "notes.txt").write_text(content)
    entry = launch.PACKAGES / "@modelcontextprotocol/server-filesystem/dist/index.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.setattr(launch.shutil, "which", lambda *args, **kwargs: "/test/node")
    args, _, _ = launch.command_for("filesystem")
    assert args[2:] == [str(launch.LIBRARY), str(launch.LEGACY_LIBRARY)]
    assert str(tmp_path) not in args
    assert (launch.LIBRARY / "notes.txt").read_text() == "new"
    assert (launch.LEGACY_LIBRARY / "notes.txt").read_text() == "legacy"
    assert launch.library_directories() == [launch.LIBRARY, launch.LEGACY_LIBRARY]


@pytest.mark.parametrize("directory_name", ["learning-materials", "学习资料"])
@pytest.mark.parametrize("target_exists", [True, False])
def test_learning_library_rejects_symlinked_roots(tmp_path, directory_name, target_exists):
    launch = load("integrations/mcp/launch.py")
    launch.LIBRARY = tmp_path / "learning-materials"
    launch.LEGACY_LIBRARY = tmp_path / "学习资料"
    outside = tmp_path / "outside"
    if target_exists:
        outside.mkdir()
    (tmp_path / directory_name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        launch.library_directories()


def test_private_learning_files_are_ignored_in_both_directory_names():
    paths = [
        "learning-materials/private.txt",
        "learning-materials/nested/private.txt",
        "学习资料/private.txt",
        "学习资料/使用说明.md",
    ]
    # Git may quote non-ASCII paths; use -z to compare exact path bytes instead.
    ignored = (
        subprocess.check_output(
            ["git", "check-ignore", "--no-index", "--stdin", "-z"],
            input=b"\0".join(path.encode() for path in paths) + b"\0",
            cwd=ROOT,
        )
        .decode()
        .rstrip("\0")
        .split("\0")
    )
    assert ignored == paths
    assert (
        subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "learning-materials/README.md"],
            cwd=ROOT,
        ).returncode
        == 1
    )


@pytest.mark.parametrize(
    "name, expected",
    [
        ("learning-materials/private.txt", "private/generated"),
        ("学习资料/private.txt", "private/generated"),
        ("docs/中文.md", "ASCII"),
        ("learning-materials/README.md", None),
    ],
)
def test_release_check_rejects_private_libraries_and_non_ascii_paths(
    monkeypatch, capsys, name, expected
):
    release = load("scripts/check_release.py")

    def fake_git(*args):
        if args == ("ls-files", "--stage", "-z"):
            return f"100644 {'0' * 40} 0\t{name}\0".encode()
        assert args == ("cat-file", "blob", "0" * 40)
        return b"Public documentation\n"

    monkeypatch.setattr(release, "git", fake_git)
    assert release.main() == (1 if expected else 0)
    if expected:
        assert expected in capsys.readouterr().out


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
