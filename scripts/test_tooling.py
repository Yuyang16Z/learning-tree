"""Safety boundaries of the developer launcher and optional MCP wrappers."""

import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tomllib
import unicodedata
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
    monkeypatch.setenv("ACCESS_TOKEN_FILE", "/private/real/phone-access-token")
    env = manage.isolated_env(tmp_path / "dev.db")
    assert env["DATABASE_URL"] == f"sqlite:///{(tmp_path / 'dev.db').as_posix()}"
    assert env["DEFAULT_API_KEY"] == "mock"
    assert env["VITE_API_BASE"] == "/api"
    # The phone-access token stays beside the isolated database.
    assert env["ACCESS_TOKEN_FILE"] == (tmp_path / "phone-access-token").resolve().as_posix()


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


def test_custom_learning_styles_are_ignored_except_their_readme():
    paths = ["learning-presets/my-style.md", "learning-presets/nested/notes.md"]
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
    for public in ("learning-presets/README.md", "app/learning_presets/socratic.md"):
        assert (
            subprocess.run(["git", "check-ignore", "--no-index", "-q", public], cwd=ROOT).returncode
            == 1
        )


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
        ("learning-presets/my-style.md", "private/generated"),
        ("learning-presets/README.md", None),
        ("app/learning_presets/socratic.md", None),
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
    # Every key term is cut from its question, so the question's own message always
    # matches; that match is counted apart. Only the English questions' terms also
    # occur in their node's answer, facts or note; the Chinese terms are whole questions.
    assert search["queries"] == search["own_question_found"] == 17
    assert search["node_found"] == 6 and search["node_found_share"] == round(6 / 17, 4)
    found = {case["node_id"] for case in search["cases"] if case["node_found"]}
    assert found == {21, 22, 23, 24, 25}
    for case in search["cases"]:
        assert case["own_question_rank"] is not None
        assert (case["node_rank"] is not None) is case["node_found"]
    scopes = report["memory_retrieval"]["scopes"]
    assert scopes["path"]["returned"]["tree"] == scopes["path"]["returned"]["other"] == 0
    assert scopes["tree"]["returned"]["tree"] > 0 and scopes["tree"]["returned"]["other"] == 0
    assert scopes["all"]["returned"]["other"] > 0
    for summary in scopes.values():
        # The default run is keyword-only: no similarity gate or reranker cutoff applies.
        assert summary["modes"] == {"lexical": 17} and summary["reranked"] == 0
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


# Report paths that reach the database, or a name beside it, through another spelling.
_REPORT_SPELLINGS = {
    "output_name_case": lambda folder, source: folder / source.name.upper(),
    "output_log_case": lambda folder, source: folder / f"{source.name.capitalize()}-WAL",
    "output_log_normalization": lambda folder, source: (
        folder / unicodedata.normalize("NFD", f"{source.name}-wal")
    ),
    "output_folder_case": lambda folder, source: (
        folder.with_name(folder.name.upper()) / source.name
    ),
    # On macOS /System/Volumes/Data/private/... is the same directory as /private/...
    "output_firmlink": lambda folder, source: Path(f"/System/Volumes/Data{source.resolve()}"),
}


@pytest.mark.parametrize(
    "case",
    ["missing", "not_sqlite", "log_without_shared_memory", "rollback_journal", "output"]
    + list(_REPORT_SPELLINGS),
)
def test_retrieval_evaluation_refuses_unsafe_sources(tmp_path, capsys, case):
    evaluate = load("scripts/evaluate_retrieval.py")
    folder = tmp_path / "data"
    folder.mkdir()
    # A name that NFD decomposes, for the normalization-insensitive spelling (APFS).
    name = "Écoles.db" if case == "output_log_normalization" else "learning.db"
    source = folder / unicodedata.normalize("NFC", name)
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
    elif case in _REPORT_SPELLINGS:
        output = _REPORT_SPELLINGS[case](folder, source)
        if case in ("output_folder_case", "output_firmlink") and not (
            output.exists() and os.path.samefile(output, source)
        ):
            pytest.skip("this file system has no such alias for the database")
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


def test_retrieval_evaluation_labels_leave_out_facts_the_prompt_already_holds(tmp_path):
    evaluate = load("scripts/evaluate_retrieval.py")
    source = tmp_path / "labels.db"
    with closing(sqlite3.connect(source)) as database:
        database.executescript(
            """
            CREATE TABLE knowledgetree (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL,
                created_at DATETIME NOT NULL);
            CREATE TABLE node (id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL,
                parent_id INTEGER, title VARCHAR NOT NULL, seed_text VARCHAR, summary VARCHAR,
                learning_note VARCHAR, created_at DATETIME NOT NULL);
            CREATE TABLE message (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL,
                role VARCHAR NOT NULL, content VARCHAR NOT NULL, answered_by VARCHAR,
                created_at DATETIME NOT NULL);
            CREATE TABLE memory (id INTEGER PRIMARY KEY, kind VARCHAR NOT NULL,
                content VARCHAR NOT NULL, tree_id INTEGER, source_node_id INTEGER,
                created_at DATETIME NOT NULL);
            INSERT INTO knowledgetree VALUES (1, 'Fixture heaps', '2025-03-01 09:00:00');
            INSERT INTO node VALUES (1, 1, NULL, 'Fixture root', NULL, NULL, NULL,
                '2025-03-01 09:01:00');
            INSERT INTO message VALUES (1, 1, 'user', 'What does a heap guarantee?', NULL,
                '2025-03-01 09:01:00');
            INSERT INTO message VALUES (2, 1, 'assistant',
                'A heap keeps the smallest key at its root.', 'Fixture model',
                '2025-03-01 09:02:00');
            -- Facts 2 and 3 share a text, so retrieval can return only one of them.
            INSERT INTO memory VALUES (1, 'fact', 'A heap keeps the smallest key at its root.',
                1, 1, '2025-03-01 09:03:00');
            INSERT INTO memory VALUES (2, 'fact', 'A binary heap is stored as an array.',
                1, 1, '2025-03-01 09:03:00');
            INSERT INTO memory VALUES (3, 'fact', 'A binary heap is stored as an array.',
                1, 1, '2025-03-01 09:03:00');
            -- A branch whose quoted passage is fact 1, word for word.
            INSERT INTO node VALUES (2, 1, 1, 'Fixture branch',
                'A heap keeps the smallest key at its root.', NULL, NULL, '2025-03-01 09:05:00');
            INSERT INTO message VALUES (3, 2, 'user',
                'Why is the root of a binary heap the smallest key?', NULL,
                '2025-03-01 09:05:00');
            INSERT INTO message VALUES (4, 2, 'assistant',
                'Every parent is ordered before its children.', 'Fixture model',
                '2025-03-01 09:06:00');
            -- A follow-up whose learning note holds the text of facts 2 and 3.
            INSERT INTO node VALUES (3, 1, 1, 'Fixture follow-up', NULL, NULL,
                'Remember: a binary heap is stored as an array.', '2025-03-01 09:07:00');
            INSERT INTO message VALUES (5, 3, 'user',
                'How do I find the children of a heap node?', NULL, '2025-03-01 09:07:00');
            INSERT INTO message VALUES (6, 3, 'assistant',
                'The children of index i are at 2i + 1 and 2i + 2.', 'Fixture model',
                '2025-03-01 09:08:00');
            """
        )
    output = tmp_path / "report.json"
    arguments = ["--database", str(source), "--scopes", "path", "--output", str(output)]
    assert evaluate.main(arguments) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    cases = {case["question_message_id"]: case for case in report["memory_retrieval"]["cases"]}
    # Retrieval drops fact 1 for the branch because its quote already holds it, and
    # returns one of the two identical facts: the label is one text, and it was found.
    found = {"source_node_id": 1, "relevant": 1, "found": 1, "rank": 1}
    assert cases[3]["memory_ids"] == [3]
    assert cases[3]["self"] == [] and cases[3]["parent"] == {**found, "in_prompt": 1}
    # The follow-up's note holds the text of facts 2 and 3, so only fact 1 is left.
    assert cases[5]["memory_ids"] == [1]
    assert cases[5]["self"] == [{**found, "in_prompt": 2}]
    assert cases[5]["parent"] == {**found, "in_prompt": 2}
    summary = report["memory_retrieval"]["scopes"]["path"]
    perfect = {"cases": 2, "recall_at_k": 1.0, "hit_rate_at_k": 1.0, "mrr_at_k": 1.0}
    assert summary["parent_recall"] == {**perfect, "facts_in_prompt": 3}
    assert summary["self_recall"] == {**perfect, "cases": 1, "facts_in_prompt": 2}


@pytest.mark.parametrize("missing", ["runtime", "reranker"])
def test_retrieval_evaluation_hybrid_without_local_models_exits_2(
    tmp_path, monkeypatch, capsys, missing
):
    evaluate = load("scripts/evaluate_retrieval.py")
    import app.semantic_models as semantic

    if missing == "runtime":
        monkeypatch.setattr(semantic, "_dependencies_available", lambda: False)
    else:
        # The embedding model is cached but the separately downloaded reranker is not.
        monkeypatch.setattr(semantic, "get_backend", lambda: _FakeModels(reranker="missing"))
    output = tmp_path / "report.json"
    assert evaluate.main(["--synthetic", "--hybrid", "--output", str(output)]) == 2
    assert not output.exists()
    error = capsys.readouterr().err
    assert "Hybrid retrieval was requested" in error
    if missing == "reranker":
        assert "reranking model" in error and "embedding" not in error


class _FakeModels:
    """Deterministic stand-in for the local models; either one may fail or be missing.

    reranker: "ready", "missing" (not loaded), "fails" (raises from its third call on)
    or "invalid" (returns scores outside [0, 1], which retrieval rejects).
    """

    model_key = "synthetic-evaluation-model"

    def __init__(self, fail_after=None, reranker="ready"):
        self.fail_after = fail_after
        self.reranker = reranker
        self.queries = 0
        self.reranks = 0

    def prepare(self, allow_download):
        assert allow_download is False
        ready = self.reranker != "missing"
        return {
            "state": "ready" if ready else "degraded",
            "embedding_ready": True,
            "reranker_ready": ready,
        }

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
        self.reranks += 1
        if self.reranker == "missing" or (self.reranker == "fails" and self.reranks > 2):
            raise RuntimeError("synthetic reranking failure")
        return [1.5 if self.reranker == "invalid" else 0.5 for _ in documents]


@pytest.mark.parametrize(
    "fail_after, reranker, expected",
    [(None, "ready", 0), (3, "ready", 2), (None, "fails", 2), (None, "invalid", 2)],
)
def test_retrieval_evaluation_records_whether_hybrid_was_achieved(
    tmp_path, monkeypatch, capsys, fail_after, reranker, expected
):
    evaluate = load("scripts/evaluate_retrieval.py")
    import app.semantic_models as semantic

    models = _FakeModels(fail_after, reranker)
    monkeypatch.setattr(semantic, "get_backend", lambda: models)
    output = tmp_path / "report.json"
    arguments = ["--synthetic", "--hybrid", "--scopes", "path", "--output", str(output)]
    assert evaluate.main(arguments) == expected
    report = json.loads(output.read_text(encoding="utf-8"))
    semantic_report = report["semantic"]
    path = report["memory_retrieval"]["scopes"]["path"]
    assert semantic_report["requested"] is True and semantic_report["embedding_ready"] is True
    assert semantic_report["reranker_ready"] is True
    assert semantic_report["achieved"] is (expected == 0)
    assert semantic_report["semantic_attempts"] == path["semantic_attempts"] > 0
    assert semantic_report["rerank_attempts"] == path["rerank_attempts"] > 0
    assert semantic_report["rerank_fallbacks"] == path["rerank_fallbacks"]
    if expected == 0:
        assert semantic_report["fallback_cases"] == 0 and semantic_report["hybrid_cases"] > 0
        assert semantic_report["rerank_fallbacks"] == 0
        assert path["reranked"] == path["rerank_attempts"] <= models.reranks
        assert capsys.readouterr().err == ""
    elif fail_after is not None:
        assert semantic_report["fallback_cases"] > 0
    else:
        # The embedding model kept working, so these cases still report mode "hybrid",
        # but the reranker's result was not applied.
        assert semantic_report["hybrid_cases"] == semantic_report["semantic_attempts"]
        assert path["reranked"] == path["rerank_attempts"] - path["rerank_fallbacks"]
        assert semantic_report["rerank_fallbacks"] > 0
        assert semantic_report["fallback_cases"] == semantic_report["rerank_fallbacks"]
        assert "reranking" in capsys.readouterr().err


def test_retrieval_evaluation_hybrid_needs_a_question_that_reaches_the_models(
    tmp_path, monkeypatch, capsys
):
    evaluate = load("scripts/evaluate_retrieval.py")
    import app.semantic_models as semantic

    models = _FakeModels()
    monkeypatch.setattr(semantic, "get_backend", lambda: models)
    source = tmp_path / "learning.db"
    evaluate.build_synthetic(source)
    with closing(sqlite3.connect(source)) as database:
        database.execute("DELETE FROM memory")  # Questions and answers, but no facts yet.
        database.commit()
    output = tmp_path / "report.json"
    arguments = ["--database", str(source), "--hybrid", "--output", str(output)]
    assert evaluate.main(arguments) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["database"]["completed_questions"] == 17
    assert report["semantic"]["semantic_attempts"] == models.queries == 0
    assert report["semantic"]["achieved"] is False
    assert "never used" in capsys.readouterr().err


def test_retrieval_evaluation_workspace_is_private_and_removed():
    evaluate = load("scripts/evaluate_retrieval.py")
    with evaluate.workspace() as directory:
        assert directory.name.startswith("learning-tree-eval-")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        (directory / "evaluation.db").write_bytes(b"scratch")
    assert not directory.exists()


def _replay_cost(evaluate, index_padding):
    """SQLite VM steps of one fact replay on a copy whose search index holds extra rows,
    and whether the copy's triggers and index are intact afterwards."""
    from sqlalchemy import event

    from app.search_index import index_count, source_count

    schema = "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    with evaluate.workspace() as directory:
        database = directory / "evaluation.db"
        evaluate.build_synthetic(database)
        with closing(sqlite3.connect(database)) as connection:
            # Index rows of messages that do not exist: retrieval never reads the index.
            connection.executemany(
                "INSERT INTO search_index (kind, ref_id, tree_id, node_id, content) "
                "VALUES ('message', ?, 0, 0, 'padding for the full-text index')",
                [(100000 + index,) for index in range(index_padding)],
            )
            connection.commit()
            triggers = connection.execute(schema).fetchall()
        engine = evaluate._engine(database)
        steps = [0]

        def count():
            steps[0] += 1
            return 0  # Never interrupt the statement.

        event.listen(engine, "connect", lambda raw, record: raw.set_progress_handler(count, 100))
        try:
            snapshot = evaluate.load_snapshot(engine)
            steps[0] = 0
            evaluate.measure_retrieval(
                engine, snapshot, ["path"], 5, evaluate._LexicalOnly(), False
            )
            cost = steps[0]
            with engine.connect() as connection:
                intact = connection.exec_driver_sql(schema).all() == triggers and (
                    index_count(connection) == source_count(connection) + index_padding
                )
        finally:
            engine.dispose()
    return cost, intact


def test_retrieval_evaluation_replay_cost_does_not_grow_with_the_search_index():
    evaluate = load("scripts/evaluate_retrieval.py")
    small, small_intact = _replay_cost(evaluate, 0)
    large, large_intact = _replay_cost(evaluate, 3000)
    assert small_intact and large_intact
    # Hiding and revealing facts must not rescan the full-text index once per fact.
    assert large <= small * 1.05


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
        # A CJK character weighs as two letters: a short CJK run loses to a long word.
        ("请解释 Transformer 的 attention 机制", "Transformer"),
        ("harness 是什么？", "harness"),
        ("What is the CAP theorem 在分布式系统里怎么用？", "在分布式系统里怎么用"),
        ("为何？ok", None),
    ],
)
def test_retrieval_evaluation_key_terms(question, term):
    assert load("scripts/evaluate_retrieval.py").key_term(question) == term


def test_demo_recording_doubles_take_the_calls_the_app_makes(tmp_path, monkeypatch):
    """record_demo.py swaps only the provider boundary, so its doubles must accept every
    argument the routes pass, including keyword options added later."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import Session, SQLModel, create_engine

    import app.service as service
    from app.models import ModelConfig
    from app.routers import nodes, trees

    demo = load("scripts/record_demo.py")
    monkeypatch.setattr(demo, "CHUNK_DELAY", 0)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'demo.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(nodes, "engine", engine)
    monkeypatch.setattr(service, "engine", engine)
    # The script replaces llm.stream_chat and llm.complete before the routers import them.
    monkeypatch.setattr(nodes, "stream_chat", demo.scripted_stream)
    monkeypatch.setattr(nodes, "complete", demo.scripted_complete)
    nodes._GENERATIONS.clear()
    with Session(engine) as session:
        session.add(
            ModelConfig(
                label="Offline demo",
                base_url="mock://offline",
                llm_model="mock",
                api_key="mock",
                is_default=True,
            )
        )
        session.commit()
    app = FastAPI()
    app.include_router(nodes.router)
    app.include_router(trees.router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[nodes.get_session] = session_override
    try:
        client = TestClient(app)
        root = client.post("/trees", json={"title": "Overfitting"}).json()["root_node_id"]
        response = client.post(f"/nodes/{root}/ask", json={"question": demo.QUESTION})
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1].get("done") is True, events[-1]
        assert "".join(event.get("delta", "") for event in events) == demo.MAIN_ANSWER
        explained = client.post(f"/nodes/{root}/explain", json={"text": "validation set"})
        assert explained.json() == {"explanation": demo.EXPLANATION}
    finally:
        for generation in list(nodes._GENERATIONS.values()):
            generation.stop.set()
        nodes._GENERATIONS.clear()
        engine.dispose()


# ---------- Human relevance labels (scripts/label_retrieval.py, --labels) ----------


def _fixture_rows(tmp_path):
    """Questions and facts of the synthetic fixture, as the labelling and the evaluation see them."""
    evaluate = load("scripts/evaluate_retrieval.py")
    fixture = tmp_path / "rows" / "fixture.db"
    fixture.parent.mkdir()
    evaluate.build_synthetic(fixture)
    with closing(sqlite3.connect(fixture)) as connection:
        # Questions the replay scores: non-empty, answered by a completed answer (the
        # fixture writes each answer right after its question).
        questions = connection.execute(
            "SELECT m.id, m.content, m.node_id, n.tree_id FROM message m "
            "JOIN node n ON n.id = m.node_id "
            "JOIN message a ON a.id = m.id + 1 AND a.node_id = m.node_id "
            "WHERE m.role = 'user' AND a.role = 'assistant' AND a.status = 'complete' "
            "AND trim(m.content) != '' ORDER BY m.id"
        ).fetchall()
        facts = connection.execute(
            "SELECT id, content, source_node_id, tree_id FROM memory WHERE kind = 'fact'"
        ).fetchall()
    return evaluate, questions, facts


def _digest(text):
    import hashlib

    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _scripted(monkeypatch, answers):
    """Answer the labelling prompts in order; record each prompt."""
    replies, prompts = iter(answers), []

    def ask(prompt=""):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("builtins.input", ask)
    return prompts


def _prompted_questions(output):
    return [int(match) for match in re.findall(r"Question (\d+):", output)]


def test_labelling_saves_ids_grades_and_text_hashes_only(tmp_path, monkeypatch, capsys):
    label = load("scripts/label_retrieval.py")
    _, questions, facts = _fixture_rows(tmp_path)
    labels = tmp_path / "labels.json"
    prompts = _scripted(monkeypatch, ["1=2 2=1", "", "q"])
    assert label.main(["--synthetic", "--labels", str(labels)]) == 0
    shown = capsys.readouterr().out
    first, second = _prompted_questions(shown)[:2]
    data = json.loads(labels.read_text(encoding="utf-8"))
    assert (data["format"], data["version"]) == ("learning-tree-retrieval-labels", 1)
    assert set(data["questions"]) == {str(first), str(second)}, "q saves and stops"
    texts = {row[0]: row[1] for row in questions}
    entry = data["questions"][str(first)]
    assert entry["question_sha256"] == _digest(texts[first])
    grades = sorted(fact["grade"] for fact in entry["facts"].values())
    assert grades[-2:] == [1, 2] and set(grades[:-2]) <= {0}, "unlisted candidates are 0"
    fact_texts = {row[0]: row[1] for row in facts}
    for memory_id, fact in entry["facts"].items():
        assert fact["fact_sha256"] == _digest(fact_texts[int(memory_id)])
    assert all(fact["grade"] == 0 for fact in data["questions"][str(second)]["facts"].values())
    # Candidate texts are shown in the terminal only, never written to the file.
    raw = labels.read_text(encoding="utf-8")
    assert not re.search("[㐀-鿿]", raw)
    assert [text for text in fact_texts.values() if text in raw] == []
    _assert_private(raw, tmp_path, ROOT, Path.home())
    assert stat.S_IMODE(labels.stat().st_mode) == 0o600
    assert re.search(r"Grades for facts 1-\d+", prompts[0])


def test_labelling_asks_only_for_facts_without_a_current_grade(tmp_path, monkeypatch, capsys):
    label = load("scripts/label_retrieval.py")
    labels = tmp_path / "labels.json"
    _scripted(monkeypatch, ["", "q"])
    assert label.main(["--synthetic", "--labels", str(labels)]) == 0
    first = _prompted_questions(capsys.readouterr().out)[0]
    # The judged question is not asked again.
    _scripted(monkeypatch, ["q"])
    assert label.main(["--synthetic", "--labels", str(labels)]) == 0
    assert _prompted_questions(capsys.readouterr().out)[0] != first
    # An edited fact (here: a changed text hash) makes only that fact unjudged again.
    data = json.loads(labels.read_text(encoding="utf-8"))
    judged = data["questions"][str(first)]["facts"]
    edited = sorted(judged, key=int)[0]
    judged[edited]["fact_sha256"] = "0" * 64
    labels.write_text(json.dumps(data), encoding="utf-8")
    prompts = _scripted(monkeypatch, ["q"])
    assert label.main(["--synthetic", "--labels", str(labels)]) == 0
    assert _prompted_questions(capsys.readouterr().out)[0] == first
    assert "Grades for facts 1-1 " in prompts[0]


def test_labelling_status_reports_coverage_without_asking(tmp_path, monkeypatch, capsys):
    label = load("scripts/label_retrieval.py")
    labels = tmp_path / "labels.json"
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--status never asks"))
    assert label.main(["--synthetic", "--labels", str(labels), "--status"]) == 0
    status = capsys.readouterr().out
    assert "questions with candidates" in status and "judged 0" in status
    assert not labels.exists(), "--status writes nothing"


def test_labelling_rejects_bad_answers_and_label_files(tmp_path, monkeypatch, capsys):
    label = load("scripts/label_retrieval.py")
    labels = tmp_path / "labels.json"
    prompts = _scripted(monkeypatch, ["99", "1=3", "x", "q"])
    assert label.main(["--synthetic", "--labels", str(labels)]) == 0
    assert len(prompts) == 4, "each invalid answer is asked again"
    assert not labels.exists(), "nothing was graded"
    labels.write_text("{not json", encoding="utf-8")
    assert label.main(["--synthetic", "--labels", str(labels)]) == 1
    assert "labels file" in capsys.readouterr().err


def test_graded_metrics_follow_their_definitions():
    evaluate = load("scripts/evaluate_retrieval.py")
    # Gains 2^grade - 1 discounted by log2(rank + 1): 3/1 + 0 + 1/2 against the ideal 2, 2, 1.
    ideal = 3 + 3 / math.log2(3) + 1 / 2
    score = evaluate._graded(["a", "b", "c"], {"a": 2, "c": 1, "d": 2}, 3)
    assert score == {
        "returned": 3,
        "judged": 2,
        "relevant": 3,
        "found": 2,
        "rank": 1,
        "ndcg": round(3.5 / ideal, 4),
    }
    summary = evaluate._labelled([score], 3, stale_questions=0, stale_facts=0)
    assert summary["precision_at_k"] == round(2 / 3, 4)
    assert summary["recall_at_k"] == round(2 / 3, 4)
    assert summary["mrr_at_k"] == 1.0
    assert summary["ndcg_at_k"] == round(3.5 / ideal, 4)
    assert summary["judged_at_k"] == round(2 / 3, 4)
    nothing = evaluate._graded([], {"a": 0}, 3)
    assert evaluate._labelled([nothing], 3, stale_questions=0, stale_facts=0) == {
        "questions": 1,
        "with_relevant": 0,
        "precision_at_k": None,
        "recall_at_k": None,
        "mrr_at_k": None,
        "ndcg_at_k": None,
        "judged_at_k": None,
        "stale_questions": 0,
        "stale_facts": 0,
    }


def test_retrieval_evaluation_scores_human_labels(tmp_path):
    evaluate, questions, facts = _fixture_rows(tmp_path)
    labelled = {}
    for message_id, text, node_id, tree_id in questions:
        grades = {
            str(fact_id): {"grade": 2 if source == node_id else 0, "fact_sha256": _digest(content)}
            for fact_id, content, source, fact_tree in facts
            if fact_tree == tree_id
        }
        labelled[str(message_id)] = {"question_sha256": _digest(text), "facts": grades}
    stale_question, stale_fact_question = list(labelled)[:2]
    labelled[stale_question]["question_sha256"] = "0" * 64
    some_fact = next(iter(labelled[stale_fact_question]["facts"]))
    labelled[stale_fact_question]["facts"][some_fact]["fact_sha256"] = "0" * 64
    labels = tmp_path / "labels.json"
    payload = {"format": "learning-tree-retrieval-labels", "version": 1, "questions": labelled}
    labels.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "report.json"
    assert evaluate.main(["--synthetic", "--labels", str(labels), "--output", str(output)]) == 0
    raw = output.read_text(encoding="utf-8")
    _assert_private(raw, tmp_path, ROOT, Path.home())
    report = json.loads(raw)
    replayed = report["database"]["completed_questions"]
    assert report["labels"]["stale_questions"] == 1 and report["labels"]["stale_facts"] == 1
    assert 0 < report["labels"]["questions"] < replayed
    for summary in report["memory_retrieval"]["scopes"].values():
        scored = summary["labelled"]
        assert scored["questions"] == report["labels"]["questions"]
        for key in ("precision_at_k", "recall_at_k", "mrr_at_k", "ndcg_at_k", "judged_at_k"):
            assert scored[key] is None or 0 <= scored[key] <= 1
    assert report["memory_retrieval"]["scopes"]["path"]["labelled"]["mrr_at_k"] > 0
    cases = report["memory_retrieval"]["cases"]
    assert all(
        ("labelled" in case) == (str(case["question_message_id"]) in labelled)
        for case in cases
        if str(case["question_message_id"]) != stale_question
    )
    assert not any(
        "labelled" in case for case in cases if str(case["question_message_id"]) == stale_question
    )
    assert "human relevance labels" in report["scope"].lower()


@pytest.mark.parametrize(
    "content,message",
    [
        (None, "Labels file not found"),
        ("{not json", "labels file"),
        ('{"format": "other", "version": 1, "questions": {}}', "labels file"),
        (
            '{"format": "learning-tree-retrieval-labels", "version": 1, "questions": {"1": {"question_sha256": "x", "facts": {"2": {"grade": 5, "fact_sha256": "y"}}}}}',
            "labels file",
        ),
    ],
)
def test_retrieval_evaluation_refuses_unusable_labels(tmp_path, capsys, content, message):
    evaluate = load("scripts/evaluate_retrieval.py")
    labels = tmp_path / "labels.json"
    if content is not None:
        labels.write_text(content, encoding="utf-8")
    output = tmp_path / "report.json"
    assert evaluate.main(["--synthetic", "--labels", str(labels), "--output", str(output)]) == 1
    assert message in capsys.readouterr().err
    assert not output.exists()
