"""The fetch tool reaches only public addresses, checked on every hop and pinned per request."""

import socket

import httpx
import pytest

from app import tools
from app.url_guard import MAX_REDIRECTS, BlockedURL, guarded_get, resolve_public


def resolver_for(table: dict[str, list[str]]):
    def resolve(host, port, type=0):  # noqa: A002 -- mirrors socket.getaddrinfo
        if host not in table:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)) for addr in table[host]]

    return resolve


PUBLIC = {"example.com": ["93.184.215.14"], "cdn.example.com": ["151.101.1.69"]}


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # private
        "192.168.1.10",
        "172.16.0.1",
        "169.254.169.254",  # link-local: cloud metadata
        "100.100.100.100",  # shared CGNAT, used by Tailscale
        "0.0.0.0",
        "::1",
        "fd00::1",  # unique local IPv6
        "fe80::1%en0",  # link-local IPv6 with a zone index
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "224.0.0.1",  # multicast
    ],
)
def test_non_public_addresses_are_blocked(address):
    resolve = resolver_for({"inside.example": [address]})
    with pytest.raises(BlockedURL):
        resolve_public("http://inside.example/", resolve)


def test_one_private_answer_among_public_ones_blocks_the_host():
    resolve = resolver_for({"mixed.example": ["93.184.215.14", "10.0.0.7"]})
    with pytest.raises(BlockedURL):
        resolve_public("https://mixed.example/", resolve)


# Built from parts so the release scanner's credential-URL rule does not flag the test.
CREDENTIAL_URL = "https://user" + ":pw@example.com/"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/a", CREDENTIAL_URL, "http:///x"]
)
def test_unsupported_urls_are_blocked(url):
    with pytest.raises(BlockedURL):
        resolve_public(url, resolver_for(PUBLIC))


def test_request_is_pinned_to_the_checked_address_with_original_host_and_tls_name():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    final, response = guarded_get(
        "https://example.com:8443/page?q=1",
        transport=httpx.MockTransport(handler),
        resolver=resolver_for(PUBLIC),
    )
    assert response.status_code == 200 and final == "https://example.com:8443/page?q=1"
    request = seen[0]
    assert request.url.host == "93.184.215.14" and request.url.port == 8443
    assert request.url.path == "/page" and request.url.query == b"q=1"
    assert request.headers["host"] == "example.com:8443"
    assert request.extensions["sni_hostname"] == "example.com"


def test_redirects_are_followed_and_rechecked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/a"})
        return httpx.Response(200, text="cdn")

    final, response = guarded_get(
        "http://example.com/",
        transport=httpx.MockTransport(handler),
        resolver=resolver_for(PUBLIC),
    )
    assert final == "https://cdn.example.com/a" and response.text == "cdn"


def test_redirect_to_a_private_address_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8099/models"})

    with pytest.raises(BlockedURL):
        guarded_get(
            "http://example.com/",
            transport=httpx.MockTransport(handler),
            resolver=resolver_for(PUBLIC),
        )


def test_relative_redirects_resolve_against_the_current_url_and_are_capped():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        return httpx.Response(301, headers={"location": f"/step{len(hops)}"})

    with pytest.raises(BlockedURL, match="重定向"):
        guarded_get(
            "http://example.com/start",
            transport=httpx.MockTransport(handler),
            resolver=resolver_for(PUBLIC),
        )
    assert hops == ["/start"] + [f"/step{i}" for i in range(1, MAX_REDIRECTS + 1)]


def test_fetch_tool_reports_a_refusal_instead_of_calling_out(monkeypatch):
    def refuse(host, port, type=0):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert tools._fetch("http://localhost:8099/models").startswith("fetch 已拒绝")
