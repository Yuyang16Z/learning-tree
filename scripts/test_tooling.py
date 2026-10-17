"""Safety boundaries of the developer launcher and optional MCP wrappers."""

import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tomllib
from contextlib import closing
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


def test_combined_web_launcher_only_receives_its_search_key(tmp_path, monkeypatch):
    launch = load("integrations/mcp/launch.py")
    launch.DATA = tmp_path / "state"
    launch.LIBRARY = tmp_path / "learning-materials"
    launch.LEGACY_LIBRARY = tmp_path / "legacy"
    launch.PYTHON = tmp_path / "python"
    launch.PYTHON.touch()
    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    monkeypatch.setenv("DEFAULT_API_KEY", "test-model-key")
    args, env, cwd = launch.command_for("web-research")
    assert args == [str(launch.PYTHON), str(ROOT / "integrations/mcp/web_research.py")]
    assert env["TAVILY_API_KEY"] == "test-search-key"
    assert "DEFAULT_API_KEY" not in env
    assert cwd == launch.DATA
    for key in ("fetch", "time"):
        assert "TAVILY_API_KEY" not in launch.command_for(key)[1]


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
        ("build/macos/LearningTree.app/Contents/Info.plist", "private/generated"),
        ("downloads/LearningTree.app/Contents/Info.plist", "private/generated"),
        ("desktop/macos/LearningTree.swift", None),
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


@pytest.mark.parametrize("old_label", ["网页阅读", "My reading tool"])
def test_web_preset_upgrade_reuses_id_and_preserves_other_configuration(monkeypatch, old_label):
    register = load("integrations/mcp/register.py")
    monkeypatch.setattr(register.subprocess, "run", lambda *args, **kwargs: None)
    previous = {
        "id": 7,
        "label": old_label,
        "command": "python",
        "args": ["/old/project/integrations/mcp/launch.py", "fetch"],
        "enabled": False,
    }
    custom = {
        "id": 8,
        "label": "My own service",
        "command": "python",
        "args": ["/custom/launch.py", "fetch"],
        "enabled": True,
    }
    servers = [previous.copy(), custom.copy()]
    writes = []

    def fake_api(path, body=None, method=None):
        if path == "/health":
            return {"name": "学习树"}
        if body is None:
            assert path == "/mcp"
            return servers.copy()
        assert path == "/mcp/7" and method == "PUT"
        writes.append(body)
        servers[0] = {"id": 7, **body}

    monkeypatch.setattr(register, "api", fake_api)
    register.main(["--preset", "web-research"])
    register.main(["--preset", "web-research"])
    assert len(writes) == 2
    assert len(servers) == 2
    assert servers[1] == custom
    assert servers[0]["id"] == 7
    assert servers[0]["enabled"] is False
    assert servers[0]["args"][-1] == "web-research"
    expected_label = "联网搜索" if old_label == "网页阅读" else old_label
    assert servers[0]["label"] == expected_label


# ---------- Offline retrieval evaluation (scripts/evaluate_retrieval.py) ----------

# Runs the script as a fresh process would, then reports what it imported and where the
# application's own engine pointed, so the import-order guarantee is checked end to end.
EVALUATION_PROBE = """
import json, runpy, sys
from pathlib import Path

script, output = sys.argv[1], sys.argv[2]
sys.argv = [script, "--synthetic", "--output", output]
try:
    runpy.run_path(script, run_name="__main__")
    code = 0
except SystemExit as exc:
    code = exc.code
bound = sys.modules["app.db"].engine.url.database
print(json.dumps({
    "code": code,
    "app_main": "app.main" in sys.modules,
    "routers": sorted(name for name in sys.modules if name.startswith("app.routers")),
    "bound_to_copy": Path(bound).parent.name.startswith("learning-tree-eval-"),
}))
"""


def _stored_texts(database):
    """Every stored text value of a fixture database and its user questions."""
    with closing(sqlite3.connect(database)) as connection:
        values = connection.execute(
            "SELECT content FROM message UNION ALL SELECT content FROM memory "
            "UNION ALL SELECT title FROM node UNION ALL SELECT seed_text FROM node "
            "UNION ALL SELECT learning_note FROM node UNION ALL SELECT title FROM knowledgetree"
        ).fetchall()
        questions = [
            row[0] for row in connection.execute("SELECT content FROM message WHERE role = 'user'")
        ]
    return {value for (value,) in values if value}, questions


def _assert_private(raw, *paths):
    for path in paths:
        assert str(path) not in raw
    # No JSON string starts like an absolute POSIX or Windows path.
    assert not re.search(r'"(?:/|[A-Za-z]:\\\\)', raw)


def test_retrieval_evaluation_runs_the_synthetic_fixture_end_to_end(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    fixture = tmp_path / "fixture" / "fixture.db"
    fixture.parent.mkdir()
    evaluate.build_synthetic(fixture)
    texts, questions = _stored_texts(fixture)
    texts |= {term for term in map(evaluate.key_term, questions) if term}
    output = tmp_path / "report.json"
    decoy = tmp_path / "inherited.db"
    result = subprocess.run(
        [sys.executable, "-c", EVALUATION_PROBE, str(ROOT / "scripts/evaluate_retrieval.py")]
        + [str(output)],
        cwd=tmp_path,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{decoy}"},
        capture_output=True,
        text=True,
        check=True,
    )
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    assert probe == {"code": 0, "app_main": False, "routers": [], "bound_to_copy": True}
    assert not decoy.exists()
    raw = output.read_text(encoding="utf-8")
    _assert_private(raw, tmp_path, ROOT, Path.home())
    assert not re.search("[㐀-鿿]", raw)
    assert [text for text in texts if text in raw] == []
    report = json.loads(raw)
    assert report["source"] == "synthetic fixture"
    assert report["database"]["completed_questions"] == 17
    assert report["semantic"]["requested"] is False and report["semantic"]["achieved"] is False
    small, large = report["estimator"]["policies"]
    assert (small["window_tokens"], large["window_tokens"]) == (32768, 1048576)
    # The long fixture path compacts under the byte estimator only, the longest under both.
    assert small["would_compact"] == {"utf8_bytes": 2, "estimate": 1}
    assert large["would_compact"] == {"utf8_bytes": 0, "estimate": 0}
    search = report["full_text_search"]
    assert search["queries"] == 17 and search["node_found_share"] == 1.0
    scopes = report["memory_retrieval"]["scopes"]
    assert scopes["path"]["returned"]["tree"] == scopes["path"]["returned"]["other"] == 0
    assert scopes["tree"]["returned"]["tree"] > 0 and scopes["tree"]["returned"]["other"] == 0
    assert scopes["all"]["returned"]["other"] > 0
    for summary in scopes.values():
        assert summary["modes"] == {"lexical": 17}
        for proxy in (summary["self_recall"], summary["parent_recall"]):
            assert proxy["cases"] > 0
            assert all(0 <= proxy[key] <= 1 for key in ("recall_at_k", "hit_rate_at_k", "mrr_at_k"))
    # Each question only saw facts that existed, unsuperseded, when it was asked;
    # archived topics never contribute.
    with closing(sqlite3.connect(fixture)) as connection:
        created = dict(connection.execute("SELECT id, created_at FROM memory"))
        asked = dict(connection.execute("SELECT id, created_at FROM message"))
        archived = {
            row[0]
            for row in connection.execute(
                "SELECT m.id FROM memory m JOIN knowledgetree t ON t.id = m.tree_id "
                "WHERE t.archived"
            )
        }
        replaced = dict(
            connection.execute(
                "SELECT old.id, new.created_at FROM memory old "
                "JOIN memory new ON new.id = old.superseded_by"
            )
        )
    cases = report["memory_retrieval"]["cases"]
    for case in cases:
        moment = asked[case["question_message_id"]]
        for memory_id in case["memory_ids"]:
            assert created[memory_id] < moment
            assert memory_id not in archived
            assert memory_id not in replaced or replaced[memory_id] >= moment
    (superseded,) = replaced
    assert any(superseded in case["memory_ids"] for case in cases)


def test_retrieval_evaluation_writes_text_only_when_asked(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    output = tmp_path / "report.json"
    arguments = ["--synthetic", "--scopes", "path", "--k", "3", "--output", str(output)]
    assert evaluate.main([*arguments, "--include-text"]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["settings"]["scopes"] == ["path"] and report["memory_retrieval"]["k"] == 3
    cases = report["memory_retrieval"]["cases"]
    assert {case["question"] for case in cases} >= {"快速排序在什么情况下会退化？"}
    assert any(case["facts"] for case in cases)
    assert all("term" in case for case in report["full_text_search"]["cases"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_retrieval_evaluation_reads_only_a_copy_of_the_database(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    folder = tmp_path / "data"
    folder.mkdir()
    source = folder / "learning.db"
    evaluate.build_synthetic(source)
    # A cleanly closed write-ahead-log database: a plain read-only open would leave
    # -wal and -shm files beside it.
    assert [path.name for path in folder.iterdir()] == ["learning.db"]
    assert source.read_bytes()[18] == 2
    original = source.read_bytes()
    output = tmp_path / "report.json"
    assert evaluate.main(["--database", str(source), "--output", str(output)]) == 0
    assert source.read_bytes() == original
    assert [path.name for path in folder.iterdir()] == ["learning.db"]
    raw = output.read_text(encoding="utf-8")
    _assert_private(raw, tmp_path, ROOT)
    assert "learning.db" not in raw
    report = json.loads(raw)
    assert report["source"] == "copy of a user database"
    assert report["database"]["completed_questions"] == 17


def test_retrieval_evaluation_reads_a_live_database_through_its_log(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    folder = tmp_path / "live"
    folder.mkdir()
    source = folder / "learning.db"
    evaluate.build_synthetic(source)
    output = tmp_path / "report.json"
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO knowledgetree (id, title, archived, created_at) "
            "VALUES (9, 'Fixture live topic', 0, '2026-02-01 09:00:00.000000')"
        )
        writer.commit()
        siblings = sorted(path.name for path in folder.iterdir())
        assert siblings == ["learning.db", "learning.db-shm", "learning.db-wal"]
        main_file = source.read_bytes()
        assert evaluate.main(["--database", str(source), "--output", str(output)]) == 0
        assert sorted(path.name for path in folder.iterdir()) == siblings
        assert source.read_bytes() == main_file
    finally:
        writer.close()
    # The committed but not yet checkpointed topic reached the copy.
    assert json.loads(output.read_text(encoding="utf-8"))["database"]["topics"] == 5


@pytest.mark.parametrize(
    "case", ["missing", "not_sqlite", "log_without_shared_memory", "rollback_journal", "output"]
)
def test_retrieval_evaluation_refuses_unsafe_sources(tmp_path, capsys, case):
    evaluate = load("scripts/evaluate_retrieval.py")
    folder = tmp_path / "data"
    folder.mkdir()
    source = folder / "learning.db"
    output = tmp_path / "report.json"
    if case == "not_sqlite":
        source.write_text("not a database", encoding="utf-8")
    elif case != "missing":
        evaluate.build_synthetic(source)
    if case == "log_without_shared_memory":
        Path(f"{source}-wal").write_bytes(b"")
    elif case == "rollback_journal":
        Path(f"{source}-journal").write_bytes(b"")
    elif case == "output":
        output = Path(f"{source}-wal")
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    assert evaluate.main(["--database", str(source), "--output", str(output)]) == 1
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
    assert not (tmp_path / "report.json").exists()
    error = capsys.readouterr().err
    assert error.strip() and str(tmp_path) not in error


def test_retrieval_evaluation_migrates_only_the_copy(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    source = tmp_path / "legacy.db"
    with closing(sqlite3.connect(source)) as database:
        # The shape of a database written before structured facts and the search index.
        database.executescript(
            """
            CREATE TABLE knowledgetree (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL,
                created_at DATETIME NOT NULL);
            CREATE TABLE node (id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL,
                parent_id INTEGER, title VARCHAR NOT NULL, seed_text VARCHAR, summary VARCHAR,
                created_at DATETIME NOT NULL);
            CREATE TABLE message (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL,
                role VARCHAR NOT NULL, content VARCHAR NOT NULL, answered_by VARCHAR,
                created_at DATETIME NOT NULL);
            CREATE TABLE memory (id INTEGER PRIMARY KEY, kind VARCHAR NOT NULL,
                content VARCHAR NOT NULL, tree_id INTEGER, source_node_id INTEGER,
                created_at DATETIME NOT NULL);
            INSERT INTO knowledgetree VALUES (1, 'Fixture legacy topic', '2025-03-01 09:00:00');
            INSERT INTO node VALUES (1, 1, NULL, 'Fixture root', NULL, NULL,
                '2025-03-01 09:01:00');
            INSERT INTO node VALUES (2, 1, 1, 'Fixture follow-up', NULL, NULL,
                '2025-03-01 09:05:00');
            INSERT INTO message VALUES (1, 1, 'user', 'What does a heap guarantee?', NULL,
                '2025-03-01 09:01:00');
            INSERT INTO message VALUES (2, 1, 'assistant', 'A heap keeps its root ordered.',
                'Fixture model', '2025-03-01 09:02:00');
            INSERT INTO memory VALUES (1, 'fact', 'A heap keeps the smallest key at its root.',
                1, 1, '2025-03-01 09:03:00');
            INSERT INTO message VALUES (3, 2, 'user', 'Why is the heap root the smallest key?',
                NULL, '2025-03-01 09:05:00');
            INSERT INTO message VALUES (4, 2, 'assistant', 'Every parent is ordered first.',
                'Fixture model', '2025-03-01 09:06:00');
            """
        )
    original = source.read_bytes()
    output = tmp_path / "report.json"
    assert evaluate.main(["--database", str(source), "--output", str(output)]) == 0
    assert source.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["legacy.db", "report.json"]
    with closing(sqlite3.connect(source)) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(memory)")}
    assert "superseded_by" not in columns
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["database"]["completed_questions"] == 2
    parent = report["memory_retrieval"]["scopes"]["path"]["parent_recall"]
    assert parent["cases"] == 1 and parent["hit_rate_at_k"] == 1.0


def test_retrieval_evaluation_hybrid_without_local_models_exits_2(tmp_path, monkeypatch, capsys):
    evaluate = load("scripts/evaluate_retrieval.py")
    import app.semantic_models as semantic

    monkeypatch.setattr(semantic, "_dependencies_available", lambda: False)
    output = tmp_path / "report.json"
    assert evaluate.main(["--synthetic", "--hybrid", "--output", str(output)]) == 2
    assert not output.exists()
    assert "Hybrid retrieval was requested" in capsys.readouterr().err


class _FakeModels:
    """Deterministic stand-in for the local models; it may start failing mid-run."""

    model_key = "synthetic-evaluation-model"

    def __init__(self, fail_after=None):
        self.fail_after = fail_after
        self.queries = 0

    def prepare(self, allow_download):
        assert allow_download is False
        return {"state": "ready", "embedding_ready": True, "reranker_ready": True}

    @staticmethod
    def _vector(text):
        vector = [0.0] * 8
        for character in text:
            vector[ord(character) % 8] += 1.0
        return vector

    def embed_query(self, query):
        self.queries += 1
        if self.fail_after is not None and self.queries > self.fail_after:
            raise RuntimeError("synthetic inference failure")
        return self._vector(query)

    def embed_documents(self, documents):
        return [self._vector(document) for document in documents]

    def rerank(self, query, documents):
        return [0.5 for _ in documents]


@pytest.mark.parametrize("fail_after, expected", [(None, 0), (3, 2)])
def test_retrieval_evaluation_records_whether_hybrid_was_achieved(
    tmp_path, monkeypatch, fail_after, expected
):
    evaluate = load("scripts/evaluate_retrieval.py")
    import app.semantic_models as semantic

    models = _FakeModels(fail_after)
    monkeypatch.setattr(semantic, "get_backend", lambda: models)
    output = tmp_path / "report.json"
    arguments = ["--synthetic", "--hybrid", "--scopes", "path", "--output", str(output)]
    assert evaluate.main(arguments) == expected
    report = json.loads(output.read_text(encoding="utf-8"))
    semantic_report = report["semantic"]
    assert semantic_report["requested"] is True and semantic_report["embedding_ready"] is True
    assert semantic_report["achieved"] is (fail_after is None)
    if fail_after is None:
        assert semantic_report["fallback_cases"] == 0 and semantic_report["hybrid_cases"] > 0
        assert report["memory_retrieval"]["scopes"]["path"]["reranked"] > 0
    else:
        assert semantic_report["fallback_cases"] > 0


def test_retrieval_evaluation_workspace_is_private_and_removed():
    evaluate = load("scripts/evaluate_retrieval.py")
    with evaluate.workspace() as directory:
        assert directory.name.startswith("learning-tree-eval-")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        (directory / "evaluation.db").write_bytes(b"scratch")
    assert not directory.exists()


def test_retrieval_evaluation_request_estimates_match_the_app(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    from sqlmodel import Session, create_engine

    from app.context import _full_system, _units, _wire
    from app.context_budget import estimate_request_tokens
    from app.models import Node
    from app.service import get_ancestors, get_messages

    fixture = tmp_path / "fixture.db"
    evaluate.build_synthetic(fixture)
    engine = create_engine(f"sqlite:///{fixture}")
    try:
        section = evaluate.measure_estimator(evaluate.load_snapshot(engine), 0.6)
        assert section["paths"] == 16
        with Session(engine) as session:
            for case in section["cases"]:
                node = session.get(Node, case["node_id"])
                ancestors = get_ancestors(session, node)
                sources = [*ancestors, (node, get_messages(session, node.id))]
                system = _full_system(ancestors, node, "")
                expected = estimate_request_tokens(system, _wire(_units(sources)))
                assert case["request_estimate"] == expected
                assert case["request_bytes"] > case["request_estimate"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "question, term",
    [
        ("快速排序的平均时间复杂度是多少？", "快速排序的平均时间复杂度是多少"),
        ("What is overfitting in machine learning?", "overfitting"),
        ("What does 过拟合 mean?", "过拟合"),
        ("为何？ok", None),
    ],
)
def test_retrieval_evaluation_key_terms(question, term):
    assert load("scripts/evaluate_retrieval.py").key_term(question) == term
