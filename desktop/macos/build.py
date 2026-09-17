#!/usr/bin/env python3
"""Build a native macOS window for an existing local LearningTree installation."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_ID = "app.learningtree.desktop"
APP_NAME = "LearningTree.app"
MIN_MACOS = "14.0"
DEFAULT_PROJECT = Path(__file__).resolve().parents[2]


class BuildError(RuntimeError):
    """An actionable build or installation problem."""


def project_version(project: Path) -> str:
    with (project / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def bundle_info(project: Path, python: Path, version: str) -> dict:
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "LearningTree",
        "CFBundleDisplayName": "学习树",
        "CFBundleExecutable": "LearningTree",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "LearningTree.icns",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": ["en", "zh-Hans"],
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSApplicationCategoryType": "public.app-category.education",
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "LearningTreeProjectPath": str(project),
        "LearningTreePythonPath": str(python),
    }


def validate_destination(destination: Path) -> None:
    """Never replace an unrelated application, directory, file, or symlink."""
    if destination.is_symlink():
        raise BuildError(f"Refusing to replace a symbolic link: {destination}")
    if not destination.exists():
        return
    try:
        with (destination / "Contents" / "Info.plist").open("rb") as source:
            bundle_id = plistlib.load(source).get("CFBundleIdentifier")
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as exc:
        raise BuildError(f"Destination is not a LearningTree application: {destination}") from exc
    if bundle_id != BUNDLE_ID:
        raise BuildError(f"Refusing to replace a different application: {destination}")


def replace_bundle(source: Path, destination: Path) -> None:
    """Stage on the destination volume and restore the old app if replacement fails."""
    validate_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".learningtree-install-", dir=destination.parent
    ) as tmp:
        staging = Path(tmp) / APP_NAME
        shutil.copytree(source, staging)
        validate_destination(staging)
        # Recheck immediately before mutation, including any newly appeared symlink.
        validate_destination(destination)
        # Keep the backup outside TemporaryDirectory so a failed rollback never
        # removes the user's previous application during temporary-file cleanup.
        backup = destination.parent / f".LearningTree.previous-{uuid.uuid4().hex}.app"
        had_previous = destination.exists()
        if had_previous:
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except OSError:
            if had_previous:
                try:
                    os.replace(backup, destination)
                except OSError as exc:
                    raise BuildError(
                        f"Replacement failed; the previous app is preserved at {backup}"
                    ) from exc
            raise
        if had_previous:
            shutil.rmtree(backup)


def validate_shortcut(shortcut: Path, application: Path) -> None:
    if shortcut.is_symlink():
        if shortcut.resolve() == application.resolve():
            return
        raise BuildError(f"Desktop shortcut already points elsewhere: {shortcut}")
    if shortcut.exists():
        raise BuildError(f"Refusing to replace an existing Desktop item: {shortcut}")


def install_bundle(source: Path, applications: Path, desktop: Path | None = None) -> Path:
    application = applications / APP_NAME
    shortcut = desktop / "学习树.app" if desktop is not None else None
    # A Desktop shortcut is opt-in; check it before updating the application.
    validate_destination(application)
    if shortcut is not None:
        validate_shortcut(shortcut, application)
    replace_bundle(source, application)
    if shortcut is not None:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        validate_shortcut(shortcut, application)
        if not shortcut.is_symlink():
            # Creation fails if another process creates a Desktop item first.
            shortcut.symlink_to(application, target_is_directory=True)
    return application


def run(command: list[str], *, cwd: Path, label: str) -> None:
    print(label, flush=True)
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise BuildError(f"{label} failed (exit {result.returncode}).\n{detail}")


def require_tool(name: str) -> str:
    tool = shutil.which(name)
    if not tool:
        raise BuildError(f"Required tool '{name}' was not found. See desktop/macos/README.md.")
    return tool


def build(project: Path, python: Path, *, skip_web_build: bool = False) -> Path:
    if platform.system() != "Darwin":
        raise BuildError("The native desktop application must be built on macOS.")
    arch = platform.machine()
    if arch not in {"arm64", "x86_64"}:
        raise BuildError(f"Unsupported macOS architecture: {arch}")
    source_dir = project / "desktop" / "macos"
    for path in [
        project / "app" / "main.py",
        source_dir / "LearningTree.swift",
        source_dir / "Icon.swift",
    ]:
        if not path.is_file():
            raise BuildError(f"Required project file was not found: {path}")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise BuildError(
            "Project Python environment is missing. Run the documented project setup first."
        )
    version = project_version(project)
    xcrun = require_tool("xcrun")
    iconutil = require_tool("iconutil")
    codesign = require_tool("codesign")
    output_dir = project / "build" / "macos"
    destination = output_dir / APP_NAME
    validate_destination(destination)
    if not skip_web_build:
        npm = require_tool("npm")
        if not (project / "web" / "node_modules").is_dir():
            raise BuildError(
                "Web dependencies are missing. Run the documented project setup first."
            )
        run([npm, "--prefix", "web", "run", "build"], cwd=project, label="Building web interface")
    if not (project / "web" / "dist" / "index.html").is_file():
        raise BuildError("Built web interface is missing. Build without --skip-web-build first.")

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".learningtree-build-", dir=output_dir) as tmp:
        work = Path(tmp)
        bundle = work / APP_NAME
        contents = bundle / "Contents"
        executable = contents / "MacOS" / "LearningTree"
        resources = contents / "Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir()
        with (contents / "Info.plist").open("wb") as target:
            plistlib.dump(bundle_info(project, python, version), target, sort_keys=True)

        compile_base = [xcrun, "swiftc", "-O", "-target", f"{arch}-apple-macosx{MIN_MACOS}"]
        run(
            compile_base
            + [
                "-framework",
                "AppKit",
                "-framework",
                "WebKit",
                str(source_dir / "LearningTree.swift"),
                "-o",
                str(executable),
            ],
            cwd=project,
            label="Compiling native application",
        )
        icon_generator = work / "draw-icon"
        run(
            compile_base
            + ["-framework", "AppKit", str(source_dir / "Icon.swift"), "-o", str(icon_generator)],
            cwd=project,
            label="Compiling icon artwork",
        )
        iconset = work / "LearningTree.iconset"
        run([str(icon_generator), str(iconset)], cwd=project, label="Drawing application icon")
        run(
            [
                iconutil,
                "--convert",
                "icns",
                "--output",
                str(resources / "LearningTree.icns"),
                str(iconset),
            ],
            cwd=project,
            label="Packaging application icon",
        )
        manifest = {
            "schema_version": 1,
            "version": version,
            "architecture": arch,
            "minimum_macos": MIN_MACOS,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "project_path": str(project),
            "python_path": str(python),
        }
        (resources / "build.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        run(
            [codesign, "--force", "--sign", "-", str(bundle)],
            cwd=project,
            label="Signing local application",
        )
        run(
            [codesign, "--verify", "--strict", str(bundle)],
            cwd=project,
            label="Verifying local signature",
        )
        replace_bundle(bundle, destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir", type=Path, default=DEFAULT_PROJECT, help="Existing LearningTree checkout"
    )
    parser.add_argument(
        "--python", type=Path, help="Existing project Python executable (default: .venv/bin/python)"
    )
    parser.add_argument(
        "--skip-web-build", action="store_true", help="Use the existing web/dist build"
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install in /Applications without creating a Desktop shortcut",
    )
    parser.add_argument(
        "--applications-dir",
        type=Path,
        default=Path("/Applications"),
        help="Application installation folder (default: /Applications)",
    )
    parser.add_argument(
        "--desktop-shortcut",
        action="store_true",
        help="Also create a Desktop shortcut (requires --install)",
    )
    args = parser.parse_args(argv)
    if args.desktop_shortcut and not args.install:
        parser.error("--desktop-shortcut requires --install")
    project = args.project_dir.expanduser().resolve()
    # Preserve the venv executable path: resolving its symlink would select base Python.
    python = (
        Path(os.path.abspath(args.python.expanduser()))
        if args.python
        else project / ".venv" / "bin" / "python"
    )
    try:
        bundle = build(project, python, skip_web_build=args.skip_web_build)
        print(f"Built: {bundle}")
        if args.install:
            desktop = Path.home() / "Desktop" if args.desktop_shortcut else None
            installed = install_bundle(
                bundle, args.applications_dir.expanduser().absolute(), desktop
            )
            print(f"Installed: {installed}")
            if desktop is not None:
                print(f"Desktop shortcut: {desktop / '学习树.app'}")
        return 0
    except (BuildError, OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"LearningTree: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
