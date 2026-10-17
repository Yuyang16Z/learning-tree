"""The phone-access token: a random secret that remote requests must present.

Requests from this computer stay token-free: the desktop window, scripts such as
integrations/mcp/register.py and scripts/manage.py, and Docker's loopback-published
port all use a loopback Host and no proxy. A request is remote when its Host is not
a loopback name, or when a proxy forwarded it. Tailscale Serve proxies from this same
computer, so the peer address is always loopback and cannot tell the two apart; it
keeps the client's Host header and always adds X-Forwarded-Host, plus X-Forwarded-For,
X-Forwarded-Proto: https on TLS and Tailscale-User-Login for signed-in users
(tailscale/tailscale ipn/ipnlocal/serve.go: the reverseProxy Rewrite sets
r.Out.Host = r.In.Host, then addProxyForwardedHeaders and addTailscaleIdentityHeaders).

The token is 32 random bytes (URL-safe text) in .runtime/phone-access-token, created
with mode 0600 in a 0700 directory when Settings -> Phone access is opened on this
computer. A paired phone keeps it in an HttpOnly cookie; rotating it unpairs every
device at once.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from pathlib import Path

TOKEN_PATH = Path(__file__).resolve().parents[1] / ".runtime" / "phone-access-token"
COOKIE_NAME = "lt_access"
COOKIE_MAX_AGE = 34_560_000  # 400 days, the longest lifetime browsers keep a cookie.
LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
# Any of these means a proxy forwarded the request (Forwarded is RFC 7239's form).
PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "forwarded",
    "tailscale-user-login",
    "tailscale-funnel-request",
)
# Reachable without the token so a phone can load the app and pair it.
_EXEMPT_GET = frozenset({"/", "/health"})
_EXEMPT_POST = frozenset({"/phone-access/session", "/api/phone-access/session"})
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_lock = threading.RLock()
_cache: tuple[tuple, str] | None = None


def hostname(host_header: str) -> str:
    value = host_header.strip().lower()
    if value.startswith("["):  # [::1]:8099
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def is_remote(headers) -> bool:
    """Not this computer: a non-loopback Host, or any proxy header."""
    if hostname(headers.get("host", "")) not in LOCAL_HOSTNAMES:
        return True
    return any(name in headers for name in PROXY_HEADERS)


def exempt(method: str, path: str) -> bool:
    """The app shell, its assets, the health check and pairing itself."""
    if method in ("GET", "HEAD"):
        return path in _EXEMPT_GET or path.startswith("/assets/")
    return method == "POST" and path in _EXEMPT_POST


def current_token() -> str | None:
    """The stored token, or None when none exists yet or it cannot be read safely."""
    global _cache
    path = TOKEN_PATH
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None  # Never follow a symlink planted in place of the token.
    key = (str(path), info.st_ino, info.st_mtime_ns, info.st_size)
    with _lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(descriptor, encoding="ascii") as handle:
            token = handle.read().strip()
    except (OSError, ValueError):
        return None
    if not _TOKEN.fullmatch(token):
        return None
    with _lock:
        _cache = (key, token)
    return token


def _write(token: str) -> None:
    directory = TOKEN_PATH.parent
    if directory.is_symlink():
        raise OSError("The runtime directory must not be a symlink.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # Not ours to change; the file itself stays 0600.
    temporary = directory / f".phone-access-token.{secrets.token_hex(8)}"
    # Created 0600 from the start, then swapped in whole: no reader sees a partial
    # token and no window exists with wider permissions.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(token + "\n")
        os.replace(temporary, TOKEN_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_token() -> str:
    """The stored token, created on first use."""
    with _lock:
        token = current_token()
        if token is None:
            token = secrets.token_urlsafe(32)
            _write(token)
        return token


def rotate_token() -> str:
    """Replace the token; every paired device must pair again."""
    with _lock:
        token = secrets.token_urlsafe(32)
        _write(token)
        return token


def token_matches(presented: str | None) -> bool:
    token = current_token()
    if not token or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8", "replace"), token.encode("ascii"))


class FailureLimiter:
    """Count failed pairing attempts in memory; too many in a window blocks further tries."""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._failures: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self.window:
            self._failures.popleft()

    def blocked(self) -> bool:
        with self._lock:
            self._prune(time.monotonic())
            return len(self._failures) >= self.limit

    def record(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._failures.append(now)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


pairing_failures = FailureLimiter()
