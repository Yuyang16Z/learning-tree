"""Pinned local MCP launchers. stdout is reserved for MCP JSON-RPC."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "mcp-data"
LIBRARY = ROOT / "学习资料"
PACKAGES = Path(__file__).resolve().parent / "node_modules"
PYTHON = ROOT / ".runtime/mcp-python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def server_environment() -> dict[str, str]:
    # Also safe when launched directly from a terminal containing provider keys.
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
    env = {
        key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")
    }
    extra = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path.home() / ".local/bin"]
    env["PATH"] = os.pathsep.join(
        [env.get("PATH", ""), *(str(path) for path in extra if path.is_dir())]
    )
    env["PYTHONUNBUFFERED"] = "1"
    return env


def installed_chrome() -> Path | None:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return chrome if sys.platform == "darwin" and chrome.is_file() else None


def command_for(name: str) -> tuple[list[str], dict[str, str], Path]:
    env = server_environment()
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    if LIBRARY.is_symlink():
        raise RuntimeError("The learning library must not be a symlink to another directory.")
    LIBRARY.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(DATA, 0o700)
    if name in ("fetch", "time"):
        if not PYTHON.is_file():
            raise RuntimeError(
                "MCP dependencies missing. Run: uv run python integrations/mcp/install.py"
            )
        # The time server detects the operating system's timezone itself.
        return [str(PYTHON), "-m", f"mcp_server_{name}"], env, DATA
    node = shutil.which("node", path=env["PATH"])
    if not node:
        raise RuntimeError("Node.js is required for this MCP server.")
    entries = {
        "filesystem": PACKAGES / "@modelcontextprotocol/server-filesystem/dist/index.js",
        "memory": PACKAGES / "@modelcontextprotocol/server-memory/dist/index.js",
        "sequential-thinking": PACKAGES
        / "@modelcontextprotocol/server-sequential-thinking/dist/index.js",
        "playwright": PACKAGES / "@playwright/mcp/cli.js",
    }
    entry = entries.get(name)
    if entry is None or not entry.is_file():
        raise RuntimeError(f"MCP {name} is missing. Run: uv run python integrations/mcp/install.py")
    args = [node, str(entry)]
    cwd = DATA
    if name == "filesystem":
        args.append(str(LIBRARY))
    elif name == "memory":
        env["MEMORY_FILE_PATH"] = str(DATA / "knowledge.jsonl")
    elif name == "sequential-thinking":
        env["DISABLE_THOUGHT_LOGGING"] = "true"
    else:
        browser_output = DATA / "browser"
        browser_output.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Separate in-memory profile; never attach the user's everyday browser.
        args += [
            "--headless",
            "--isolated",
            "--image-responses",
            "omit",
            "--output-dir",
            str(browser_output),
            "--timeout-navigation",
            "30000",
        ]
        chrome = installed_chrome()
        args += ["--executable-path", str(chrome)] if chrome else ["--browser", "chromium"]
        cwd = LIBRARY
    return args, env, cwd


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("Supply one MCP preset name.")
        args, env, cwd = command_for(sys.argv[1])
        os.chdir(cwd)
        os.execvpe(args[0], args, env)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
