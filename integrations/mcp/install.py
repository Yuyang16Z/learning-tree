#!/usr/bin/env python3
"""Install optional MCP dependencies from lockfiles; registration is explicit."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from launch import installed_chrome


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register",
        action="store_true",
        help="Also register presets in the running localhost:8099 app",
    )
    parser.add_argument(
        "--with-browser-deps",
        action="store_true",
        help="Ask Playwright to install Chromium OS dependencies (Linux; may use sudo)",
    )
    args = parser.parse_args()
    os.umask(0o077)
    npm, uv = shutil.which("npm"), shutil.which("uv")
    if not npm or not uv:
        raise SystemExit("Install Node.js/npm and uv first, then run manage.py setup.")

    def run(command):
        subprocess.run(command, cwd=ROOT, check=True)

    run([npm, "ci", "--prefix", "integrations/mcp", "--ignore-scripts"])
    venv = ROOT / ".runtime/mcp-python"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        run([uv, "venv", "--python", sys.executable, str(venv)])
    run([uv, "pip", "sync", "--python", str(python), "integrations/mcp/requirements.lock"])
    if not installed_chrome():
        command = [
            shutil.which("node"),
            "integrations/mcp/node_modules/playwright/cli.js",
            "install",
        ]
        if args.with_browser_deps:
            command.append("--with-deps")
        run([*command, "chromium"])
    if args.register:
        run([sys.executable, "integrations/mcp/register.py"])
    else:
        print("Installed. Start the app, then run: uv run python integrations/mcp/register.py")


if __name__ == "__main__":
    main()
