"""Phone access reads Tailscale state only; fixtures stand in for the CLI."""

import shlex

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import phone_access

HOST = "study-mac.tail1a2b3c.ts.net"
SERVED = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8099"}}}},
}
RUNNING = {"BackendState": "Running", "Self": {"DNSName": f"{HOST}."}}


@pytest.fixture
def cli(monkeypatch):
    replies = {}
    monkeypatch.setattr(phone_access, "_cli", lambda: ("/fake/tailscale", "tailscale"))
    monkeypatch.setattr(phone_access, "_json", lambda _exe, *args: replies.get(args))
    return replies


def test_served_url_requires_https_root_proxy_to_this_port():
    assert phone_access.served_url(SERVED, 8099) == (f"https://{HOST}", False)
    assert phone_access.served_url(SERVED, 8100) == (None, False)
    custom = {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {f"{HOST}:8443": {"Handlers": {"/": {"Proxy": "localhost:8099"}}}},
    }
    assert phone_access.served_url(custom, 8099) == (f"https://{HOST}:8443", False)
    plain_http = {**SERVED, "TCP": {"443": {"HTTP": True}}}
    assert phone_access.served_url(plain_http, 8099) == (None, False)
    subpath = {**SERVED, "Web": {f"{HOST}:443": {"Handlers": {"/tree": {"Proxy": "8099"}}}}}
    assert phone_access.served_url(subpath, 8099) == (None, False)
    remote = {
        **SERVED,
        "Web": {f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://10.0.0.2:8099"}}}},
    }
    assert phone_access.served_url(remote, 8099) == (None, False)


def test_foreground_sessions_and_funnel_are_detected():
    foreground = {"Foreground": {"session": {**SERVED, "AllowFunnel": {f"{HOST}:443": True}}}}
    assert phone_access.served_url(foreground, 8099) == (f"https://{HOST}", True)


def test_states_guide_setup_without_changing_anything(cli, monkeypatch):
    assert phone_access.phone_access(8099) == {"state": "offline"}
    cli[("status", "--json")] = {"BackendState": "NeedsLogin"}
    assert phone_access.phone_access(8099) == {"state": "offline"}

    cli[("status", "--json")] = RUNNING
    assert phone_access.phone_access(8099) == {
        "state": "not_served",
        "host": HOST,
        "command": "tailscale serve --bg 8099",
    }
    cli[("serve", "status", "--json")] = SERVED
    assert phone_access.phone_access(8099) == {"state": "ready", "url": f"https://{HOST}"}
    cli[("serve", "status", "--json")] = {**SERVED, "AllowFunnel": {f"{HOST}:443": True}}
    assert phone_access.phone_access(8099) == {
        "state": "public",
        "url": f"https://{HOST}",
        "command": "tailscale funnel reset",
    }

    monkeypatch.setattr(phone_access, "_cli", lambda: None)
    assert phone_access.phone_access(8099) == {"state": "not_installed"}


def test_app_bundle_cli_is_typed_with_its_full_path(monkeypatch, tmp_path):
    bundled = tmp_path / "Tailscale.app" / "Contents" / "MacOS" / "Tailscale"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(phone_access.shutil, "which", lambda _name: None)
    monkeypatch.setattr(phone_access, "_KNOWN_CLIS", (tmp_path / "missing", bundled))
    assert phone_access._cli() == (str(bundled), shlex.quote(str(bundled)))
    installed = tmp_path / "bin" / "tailscale"
    installed.parent.mkdir()
    installed.write_text("")
    monkeypatch.setattr(phone_access, "_KNOWN_CLIS", (installed, bundled))
    assert phone_access._cli() == (str(installed), "tailscale")


def test_endpoint_reports_the_listening_port(monkeypatch):
    ports = []
    monkeypatch.setattr(
        phone_access, "phone_access", lambda port: ports.append(port) or {"state": "x"}
    )
    response = TestClient(main.app).get("/api/phone-access")
    assert response.status_code == 200 and response.json() == {"state": "x"}
    assert ports == [80]  # TestClient's server socket; uvicorn reports 8099.
