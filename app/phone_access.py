"""Find the private Tailscale address a phone can open, without changing any settings.

Only read-only status commands run, with fixed arguments and a short timeout. The
API has no login, so an address published to the internet through Funnel is
reported as a danger rather than as ready."""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter(prefix="/phone-access", tags=["phone-access"])
# GUI-launched servers often lack /usr/local/bin and Homebrew on PATH.
_KNOWN_CLIS = (
    Path("/usr/local/bin/tailscale"),
    Path("/opt/homebrew/bin/tailscale"),
    Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
)


def _cli() -> tuple[str, str] | None:
    """The executable, and how to type it in a terminal."""
    found = shutil.which("tailscale")
    if found:
        return found, "tailscale"
    for path in _KNOWN_CLIS:
        if path.is_file():
            typed = "tailscale" if path.parent.name == "bin" else shlex.quote(str(path))
            return str(path), typed
    return None


def _json(executable: str, *args: str):
    try:
        done = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=5, check=False
        )
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _local_proxy(target: str, port: int) -> bool:
    address = target.split("://", 1)[-1].rstrip("/")
    host, _, value = address.rpartition(":")
    return value == str(port) and host in ("", "localhost", "127.0.0.1", "[::1]")


def served_url(config: dict, port: int) -> tuple[str | None, bool]:
    """The HTTPS address serving this app at its root, and whether Funnel makes it public."""
    for layer in (config, *(config.get("Foreground") or {}).values()):
        https = {
            key for key, value in (layer.get("TCP") or {}).items() if (value or {}).get("HTTPS")
        }
        for host_port, web in (layer.get("Web") or {}).items():
            root = ((web or {}).get("Handlers") or {}).get("/") or {}
            host, _, listen = host_port.rpartition(":")
            if listen in https and _local_proxy(root.get("Proxy") or "", port):
                url = f"https://{host}" if listen == "443" else f"https://{host}:{listen}"
                return url, bool((layer.get("AllowFunnel") or {}).get(host_port))
    return None, False


def phone_access(port: int) -> dict:
    cli = _cli()
    if cli is None:
        return {"state": "not_installed"}
    executable, typed = cli
    status = _json(executable, "status", "--json")
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return {"state": "offline"}
    host = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".") or None
    config = _json(executable, "serve", "status", "--json")
    url, public = served_url(config if isinstance(config, dict) else {}, port)
    if url and public:
        return {"state": "public", "url": url, "command": f"{typed} funnel reset"}
    if url:
        return {"state": "ready", "url": url}
    return {"state": "not_served", "host": host, "command": f"{typed} serve --bg {port}"}


@router.get("")
def get_phone_access(request: Request) -> dict:
    # The listening socket, not the address a proxy used to reach it.
    server = request.scope.get("server") or ("127.0.0.1", 8099)
    return phone_access(int(server[1] or 8099))
