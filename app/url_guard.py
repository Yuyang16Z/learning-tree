"""Outbound URL checks for the built-in fetch tool (server-side request forgery protection).

A model can be steered by page content into requesting any URL, so the fetch tool only
reaches public internet addresses: every address a host name resolves to must be global,
each redirect is checked again, and the connection goes to the checked address so a second
DNS answer cannot swap in a private one (DNS rebinding). HTTPS still verifies the
certificate for the original host name.
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_REDIRECTS = 5

Resolver = Callable[..., list]


class BlockedURL(ValueError):
    """The URL points somewhere a model-chosen request must not reach."""


def _check_address(address: str) -> None:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # Drop an IPv6 zone index.
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global excludes loopback, private, link-local (cloud metadata), shared CGNAT
    # (including Tailscale's 100.64.0.0/10), unspecified and reserved ranges.
    if not ip.is_global or ip.is_multicast:
        raise BlockedURL(f"不允许访问本机或内网地址 {ip}")


def resolve_public(url: str, resolver: Resolver = socket.getaddrinfo) -> str:
    """Return one checked address for the URL's host; every resolved address must be public."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("只允许 http 或 https 链接")
    if not parts.hostname:
        raise BlockedURL("链接缺少主机名")
    if parts.username or parts.password:
        raise BlockedURL("链接不能包含用户名或密码")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = resolver(parts.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as exc:
        raise BlockedURL(f"无法解析主机名：{exc}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise BlockedURL("无法解析主机名")
    for address in addresses:
        _check_address(address)
    return addresses[0]


def _pinned(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Point the request at the checked address, keeping the Host header and TLS name."""
    parts = urlsplit(url)
    host = f"[{address}]" if ":" in address else address
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    headers = {"Host": parts.netloc.rsplit("@", 1)[-1]}
    extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
    return pinned, headers, extensions


def guarded_get(
    url: str,
    *,
    timeout: float = 10,
    headers: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[str, httpx.Response]:
    """GET a public URL, following up to MAX_REDIRECTS checked redirects.

    Returns the final URL and its response; raises BlockedURL for a disallowed hop."""
    current = url
    with httpx.Client(
        timeout=timeout, follow_redirects=False, headers=headers, transport=transport
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            address = resolve_public(current, resolver)
            pinned, host_header, extensions = _pinned(current, address)
            response = client.get(pinned, headers=host_header, extensions=extensions)
            location = response.headers.get("location")
            if response.is_redirect and location:
                current = urljoin(current, location)
                continue
            return current, response
    raise BlockedURL(f"重定向超过 {MAX_REDIRECTS} 次")
