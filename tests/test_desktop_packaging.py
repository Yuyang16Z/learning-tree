"""Bundle metadata and safe updates; never use the real Applications or Desktop."""

import importlib.util
import plistlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "desktop" / "macos" / "build.py"
spec = importlib.util.spec_from_file_location("desktop_build", SCRIPT)
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


def make_bundle(path: Path, identity: str = packaging.BUNDLE_ID, marker: str = "new") -> Path:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identity}))
    (path / "Contents" / "marker").write_text(marker)
    return path


def test_metadata_preserves_unicode_paths_without_broad_network_exemption(tmp_path):
    project = tmp_path / "学习树 project"
    python = project / ".venv" / "bin" / "python"
    metadata = plistlib.loads(plistlib.dumps(packaging.bundle_info(project, python, "0.2.0")))
    assert metadata["LearningTreeProjectPath"] == str(project)
    assert metadata["LearningTreePythonPath"] == str(python)
    assert metadata["CFBundleIdentifier"] == packaging.BUNDLE_ID
    assert metadata["CFBundleExecutable"] == "LearningTree"
    assert metadata["CFBundleShortVersionString"] == "0.2.0"
    assert metadata["NSAppTransportSecurity"] == {"NSAllowsLocalNetworking": True}


def test_install_and_update_preserve_shortcut(tmp_path):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME, marker="first")
    applications = tmp_path / "Applications"
    desktop = tmp_path / "桌面 with spaces"
    application = packaging.install_bundle(source, applications, desktop)
    shortcut = desktop / "学习树.app"
    assert shortcut.is_symlink()
    assert shortcut.resolve() == application
    (source / "Contents" / "marker").write_text("second")
    packaging.install_bundle(source, applications, desktop)
    assert (application / "Contents" / "marker").read_text() == "second"
    assert shortcut.resolve() == application
    assert not list(applications.glob(".LearningTree.previous-*.app"))


def test_install_without_desktop_creates_only_application(tmp_path):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    applications = tmp_path / "Applications"
    application = packaging.install_bundle(source, applications)
    assert application == applications / packaging.APP_NAME
    assert (application / "Contents" / "marker").read_text() == "new"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Applications", "build"]


@pytest.mark.parametrize("custom_directory", [False, True])
def test_install_cli_uses_applications_without_desktop_by_default(
    tmp_path, monkeypatch, custom_directory
):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    captured = {}

    def fake_install(bundle, applications, desktop):
        captured.update(bundle=bundle, applications=applications, desktop=desktop)
        return applications / packaging.APP_NAME

    monkeypatch.setattr(packaging, "build", lambda *args, **kwargs: source)
    monkeypatch.setattr(packaging, "install_bundle", fake_install)
    args = ["--install", "--skip-web-build"]
    expected = Path("/Applications")
    if custom_directory:
        expected = tmp_path / "我的应用 with spaces"
        args += ["--applications-dir", str(expected)]
    assert packaging.main(args) == 0
    assert captured == {"bundle": source, "applications": expected, "desktop": None}


def test_desktop_shortcut_requires_explicit_opt_in(tmp_path, monkeypatch):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    captured = {}
    monkeypatch.setattr(packaging, "build", lambda *args, **kwargs: source)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_install(bundle, applications, desktop):
        captured["desktop"] = desktop
        return applications / packaging.APP_NAME

    monkeypatch.setattr(packaging, "install_bundle", fake_install)
    assert packaging.main(["--install", "--desktop-shortcut"]) == 0
    assert captured["desktop"] == tmp_path / "Desktop"


@pytest.mark.parametrize("kind", ["file", "directory", "different_app", "symlink"])
def test_does_not_replace_unrelated_applications(tmp_path, kind):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    applications = tmp_path / "Applications"
    applications.mkdir()
    destination = applications / packaging.APP_NAME
    if kind == "file":
        destination.write_text("keep me")
    elif kind == "directory":
        destination.mkdir()
    elif kind == "different_app":
        make_bundle(destination, identity="org.example.unrelated")
    else:
        destination.symlink_to(source, target_is_directory=True)
    with pytest.raises(packaging.BuildError):
        packaging.install_bundle(source, applications, tmp_path / "Desktop")
    assert destination.exists()
    assert not (tmp_path / "Desktop").exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_shortcut_conflict_stops_before_updating_existing_app(tmp_path, kind):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    applications = tmp_path / "Applications"
    current = make_bundle(applications / packaging.APP_NAME, marker="keep original")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    shortcut = desktop / "学习树.app"
    if kind == "file":
        shortcut.write_text("keep me")
    elif kind == "directory":
        shortcut.mkdir()
    else:
        shortcut.symlink_to(tmp_path / "missing unrelated app")
    with pytest.raises(packaging.BuildError):
        packaging.install_bundle(source, applications, desktop)
    assert (current / "Contents" / "marker").read_text() == "keep original"
    assert shortcut.exists() or shortcut.is_symlink()


def test_failed_replacement_restores_previous_app(tmp_path, monkeypatch):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    destination = make_bundle(tmp_path / "Applications" / packaging.APP_NAME, marker="original")
    original_replace = packaging.os.replace

    def interrupted_replace(src, dst):
        if Path(src).parent.name.startswith(".learningtree-install-"):
            raise OSError("simulated interruption")
        return original_replace(src, dst)

    monkeypatch.setattr(packaging.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        packaging.replace_bundle(source, destination)
    assert (destination / "Contents" / "marker").read_text() == "original"
    assert not list(destination.parent.glob(".LearningTree.previous-*.app"))


def test_failed_rollback_preserves_backup_outside_temporary_cleanup(tmp_path, monkeypatch):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME)
    destination = make_bundle(tmp_path / "Applications" / packaging.APP_NAME, marker="original")
    original_replace = packaging.os.replace

    def interrupted_replace(src, dst):
        if Path(src) == destination:
            return original_replace(src, dst)
        raise OSError("simulated interruption")

    monkeypatch.setattr(packaging.os, "replace", interrupted_replace)
    with pytest.raises(packaging.BuildError, match="previous app is preserved"):
        packaging.replace_bundle(source, destination)
    backups = list(destination.parent.glob(".LearningTree.previous-*.app"))
    assert len(backups) == 1
    assert (backups[0] / "Contents" / "marker").read_text() == "original"


def test_replacement_rejects_invalid_source_before_modifying_current_app(tmp_path):
    source = make_bundle(tmp_path / "build" / packaging.APP_NAME, identity="org.example.unrelated")
    destination = make_bundle(tmp_path / "Applications" / packaging.APP_NAME, marker="original")
    with pytest.raises(packaging.BuildError):
        packaging.replace_bundle(source, destination)
    assert (destination / "Contents" / "marker").read_text() == "original"


def test_python_override_keeps_virtual_environment_symlink(tmp_path, monkeypatch):
    project = tmp_path / "学习树 project"
    python = tmp_path / "环境" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/usr/bin/python3")
    captured = {}

    def fake_build(project_path, python_path, **options):
        captured.update(project=project_path, python=python_path, options=options)
        return tmp_path / packaging.APP_NAME

    monkeypatch.setattr(packaging, "build", fake_build)
    assert (
        packaging.main(["--project-dir", str(project), "--python", str(python), "--skip-web-build"])
        == 0
    )
    assert captured["python"] == python
    assert captured["options"] == {"skip_web_build": True}
