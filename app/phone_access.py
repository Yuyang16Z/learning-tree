"""Find the private Tailscale address a phone can open, and pair phones with this computer.

Only read-only Tailscale status commands run, with fixed arguments and a short timeout;
no Tailscale setting is changed. A phone pairs once by opening the QR code's link, whose
URL fragment carries the access token (see app/access_token.py); the token protects the
API but is no sign-in system, so an address published to the internet through Funnel is
still reported as a danger rather than as ready. Devices that reach the app under an
ALLOWED_HOSTS name (a reverse proxy, a Docker host) pair with the same token, so this
computer manages it whatever the Tailscale state."""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import access_token
from .config import settings

router = APIRouter(prefix="/phone-access", tags=["phone-access"])
TOKEN_UNAVAILABLE = "无法保存手机访问令牌，请检查令牌文件所在目录的权限。"
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
    access = phone_access(int(server[1] or 8099))
    if settings.access_token_mode != "required" or access_token.is_remote(request.headers):
        return access
    # Only this computer sees the token, in every Tailscale state: devices that reach the
    # app under an ALLOWED_HOSTS name or through a proxy in front of Docker pair with it
    # too. Pairing links carry it in the URL fragment, which browsers never send to a
    # server or put in a Referer.
    try:
        token = access_token.ensure_token()
    except OSError:
        return {**access, "access_error": TOKEN_UNAVAILABLE}
    access = {**access, "can_rotate": True}
    if access.get("state") == "public":
        return access  # Funnel publishes the app: no pairing until that is reset.
    if access.get("state") == "ready":
        access["access_url"] = f"{access['url']}/#token={token}"
    # For links to other addresses; only hosts the Host check accepts can pair.
    return {**access, "pairing_token": token, "pairing_hosts": access_token.extra_hosts()}


class PairingIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@router.post("/session")
def start_session(body: PairingIn, request: Request) -> JSONResponse:
    """Pair this browser: a valid token becomes an HttpOnly, same-site-only cookie."""
    if not access_token.is_remote(request.headers):
        raise HTTPException(400, "本机访问无需配对。")
    if access_token.pairing_failures.blocked():
        raise HTTPException(429, "配对尝试过多，请稍后再试。")
    if not access_token.token_matches(body.token):
        access_token.pairing_failures.record()
        raise HTTPException(401, "配对链接无效或已更换，请在电脑上重新扫描二维码。")
    host = access_token.hostname(request.headers.get("host", ""))
    secure = (
        request.headers.get("x-forwarded-proto", "").lower() == "https"
        or request.url.scheme == "https"
        or host.endswith(".ts.net")
    )
    response = JSONResponse({"paired": True})
    response.set_cookie(
        access_token.COOKIE_NAME,
        body.token,
        max_age=access_token.COOKIE_MAX_AGE,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/rotate")
def rotate(request: Request) -> dict:
    """Replace the token from this computer; every paired phone must scan again."""
    if access_token.is_remote(request.headers):
        raise HTTPException(403, "只能在这台电脑上更换访问令牌。")
    try:
        access_token.rotate_token()
    except OSError:
        raise HTTPException(500, TOKEN_UNAVAILABLE) from None
    return get_phone_access(request)
