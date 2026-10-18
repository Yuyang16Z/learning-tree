"""Phone access reads Tailscale state only; fixtures stand in for the CLI. Pairing hands
a phone the access token through the QR link and keeps it in a strict cookie."""

import shlex
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import access_token, phone_access
from app.config import Settings, settings

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


LOCAL = "http://127.0.0.1:8099"
COOKIE = access_token.COOKIE_NAME


@pytest.fixture
def required(monkeypatch):
    """The production default; tests/conftest.py turns the check off for other suites."""
    monkeypatch.setattr(settings, "access_token_mode", "required")
    monkeypatch.setattr(
        phone_access, "phone_access", lambda port: {"state": "ready", "url": f"https://{HOST}"}
    )


def test_token_is_created_lazily_with_private_permissions():
    assert access_token.current_token() is None and not access_token.TOKEN_PATH.exists()
    token = access_token.ensure_token()
    assert len(token) >= 43 and access_token.ensure_token() == token
    assert stat.S_IMODE(access_token.TOKEN_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(access_token.TOKEN_PATH.parent.stat().st_mode) == 0o700
    rotated = access_token.rotate_token()
    assert rotated != token and access_token.current_token() == rotated
    assert stat.S_IMODE(access_token.TOKEN_PATH.stat().st_mode) == 0o600
    assert [path.name for path in access_token.TOKEN_PATH.parent.iterdir()] == [
        "phone-access-token"
    ]


def test_symlinked_or_malformed_token_files_are_never_trusted(tmp_path):
    path = access_token.TOKEN_PATH
    path.parent.mkdir(parents=True)
    planted = tmp_path / "planted"
    planted.write_text("p" * 43)
    path.symlink_to(planted)
    assert access_token.current_token() is None
    assert not access_token.token_matches("p" * 43)
    path.unlink()
    path.write_text("too-short")
    assert access_token.current_token() is None and not access_token.token_matches("too-short")


def test_token_is_not_written_through_a_symlinked_runtime_directory(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(access_token, "TOKEN_PATH", link / "phone-access-token")
    with pytest.raises(OSError):
        access_token.ensure_token()
    assert list(real.iterdir()) == []


def test_settings_on_this_computer_get_the_pairing_link(required, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", " Learning.Example,localhost,learning.example")
    body = TestClient(main.app, base_url=LOCAL).get("/api/phone-access").json()
    token = access_token.current_token()
    assert token is not None
    assert body == {
        "state": "ready",
        "url": f"https://{HOST}",
        "access_url": f"https://{HOST}/#token={token}",
        "can_rotate": True,
        "pairing_token": token,
        "pairing_hosts": ["learning.example"],
    }


def test_other_devices_never_see_or_create_the_token(required):
    token = access_token.ensure_token()
    phone = TestClient(main.app, base_url=f"https://{HOST}", cookies={COOKIE: token})
    assert phone.get("/api/phone-access").json() == {"state": "ready", "url": f"https://{HOST}"}
    proxied = TestClient(main.app, base_url=LOCAL, cookies={COOKIE: token})
    body = proxied.get("/api/phone-access", headers={"x-forwarded-for": "100.64.0.9"}).json()
    assert body == {"state": "ready", "url": f"https://{HOST}"}
    access_token.TOKEN_PATH.unlink()
    assert phone.get("/api/phone-access").status_code == 401
    assert (
        proxied.get("/api/phone-access", headers={"x-forwarded-for": "100.64.0.9"}).status_code
        == 401
    )
    assert access_token.current_token() is None


@pytest.mark.parametrize(
    "access",
    [
        {"state": "not_installed"},
        {"state": "offline"},
        {"state": "not_served", "host": HOST, "command": "tailscale serve --bg 8099"},
    ],
)
def test_this_computer_gets_the_token_in_every_tailscale_state(required, monkeypatch, access):
    # ALLOWED_HOSTS names, reverse proxies and Docker need no Tailscale address to pair.
    monkeypatch.setattr(settings, "allowed_hosts", "learning.example")
    monkeypatch.setattr(phone_access, "phone_access", lambda port: dict(access))
    body = TestClient(main.app, base_url=LOCAL).get("/api/phone-access").json()
    token = access_token.current_token()
    assert token is not None
    assert body == {
        **access,
        "can_rotate": True,
        "pairing_token": token,
        "pairing_hosts": ["learning.example"],
    }


def test_a_public_funnel_offers_replacement_but_no_pairing(required, monkeypatch):
    public = {"state": "public", "url": f"https://{HOST}", "command": "tailscale funnel reset"}
    monkeypatch.setattr(phone_access, "phone_access", lambda port: dict(public))
    body = TestClient(main.app, base_url=LOCAL).get("/api/phone-access").json()
    assert body == {**public, "can_rotate": True}
    assert access_token.current_token() is not None


def test_token_mode_off_keeps_the_plain_address(required, monkeypatch):
    monkeypatch.setattr(settings, "access_token_mode", "off")
    body = TestClient(main.app, base_url=LOCAL).get("/api/phone-access").json()
    assert body == {"state": "ready", "url": f"https://{HOST}"}
    assert access_token.current_token() is None


def test_unwritable_token_is_reported_with_a_fixed_message(required, monkeypatch):
    def refuse():
        raise PermissionError("/private/path/that/must/not/leak")

    monkeypatch.setattr(access_token, "ensure_token", refuse)
    body = TestClient(main.app, base_url=LOCAL).get("/api/phone-access").json()
    assert body["access_error"] == phone_access.TOKEN_UNAVAILABLE and "access_url" not in body
    assert "/private/path" not in str(body)


def cookie_attributes(response) -> dict:
    parts = [part.strip() for part in response.headers["set-cookie"].split(";")]
    name, _, value = parts[0].partition("=")
    attributes = {part.split("=", 1)[0].lower(): part.partition("=")[2] for part in parts[1:]}
    return {"name": name, "value": value, **attributes}


def test_pairing_sets_a_long_lived_http_only_strict_cookie(required):
    token = access_token.ensure_token()
    phone = TestClient(main.app, base_url=f"https://{HOST}")
    # A database-free route stands in for the API; the guard runs before routing.
    assert phone.get("/api/phone-access").status_code == 401
    paired = phone.post(
        "/api/phone-access/session", json={"token": token}, headers={"origin": f"https://{HOST}"}
    )
    assert paired.status_code == 200 and paired.json() == {"paired": True}
    cookie = cookie_attributes(paired)
    assert cookie["name"] == COOKIE and cookie["value"] == token
    assert cookie["max-age"] == "34560000" and cookie["path"] == "/"
    assert cookie["samesite"].lower() == "strict"
    assert "httponly" in cookie and "secure" in cookie
    assert phone.get("/api/phone-access").status_code == 200  # The client sends the cookie.


@pytest.mark.parametrize(
    "headers, secure",
    [({}, False), ({"x-forwarded-proto": "https"}, True), ({"x-forwarded-proto": "http"}, False)],
)
def test_secure_flag_follows_https_or_tailscale(required, monkeypatch, headers, secure):
    monkeypatch.setattr(settings, "allowed_hosts", "learning.local")
    token = access_token.ensure_token()
    client = TestClient(main.app, base_url="http://learning.local:8099")
    response = client.post("/phone-access/session", json={"token": token}, headers=headers)
    assert response.status_code == 200
    assert ("secure" in cookie_attributes(response)) is secure


def test_wrong_tokens_are_rate_limited_in_memory(required):
    token = access_token.ensure_token()
    phone = TestClient(main.app, base_url=f"https://{HOST}")
    for _ in range(access_token.pairing_failures.limit):
        wrong = phone.post("/api/phone-access/session", json={"token": "w" * 43})
        assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
    blocked = phone.post("/api/phone-access/session", json={"token": token})
    assert blocked.status_code == 429 and "set-cookie" not in blocked.headers
    access_token.pairing_failures.reset()
    assert phone.post("/api/phone-access/session", json={"token": token}).status_code == 200


def test_this_computer_needs_no_pairing(required):
    token = access_token.ensure_token()
    response = TestClient(main.app, base_url=LOCAL).post(
        "/api/phone-access/session", json={"token": token}
    )
    assert response.status_code == 400 and "set-cookie" not in response.headers


def test_only_this_computer_can_rotate_the_token(required):
    old = access_token.ensure_token()
    phone = TestClient(main.app, base_url=f"https://{HOST}", cookies={COOKIE: old})
    assert phone.post("/api/phone-access/rotate").status_code == 403
    assert access_token.current_token() == old
    rotated = TestClient(main.app, base_url=LOCAL).post("/api/phone-access/rotate").json()
    new = access_token.current_token()
    assert new != old and rotated["access_url"] == f"https://{HOST}/#token={new}"
    assert phone.get("/api/phone-access").status_code == 401  # The phone must scan again.


def test_without_tailscale_this_computer_still_creates_a_token_other_hosts_pair_with(
    required, monkeypatch
):
    # ALLOWED_HOSTS and Docker setups have no Tailscale address on this machine.
    monkeypatch.setattr(settings, "allowed_hosts", "learning.example")
    monkeypatch.setattr(phone_access, "phone_access", lambda port: {"state": "not_installed"})
    TestClient(main.app, base_url=LOCAL).get("/api/phone-access")
    token = access_token.current_token()
    assert token is not None, "opening the panel on this computer creates the token"
    other = TestClient(main.app, base_url="https://learning.example")
    assert other.get("/api/phone-access").status_code == 401
    paired = other.post(
        "/api/phone-access/session",
        json={"token": token},
        headers={"origin": "https://learning.example"},
    )
    assert paired.status_code == 200
    assert other.get("/api/phone-access").json() == {"state": "not_installed"}


def test_the_token_file_follows_access_token_file(monkeypatch, tmp_path):
    root = access_token.PROJECT_ROOT
    monkeypatch.delenv("ACCESS_TOKEN_FILE", raising=False)
    default = Settings(_env_file=None).access_token_file
    assert access_token.token_path(default) == root / ".runtime" / "phone-access-token"
    monkeypatch.setenv("ACCESS_TOKEN_FILE", str(tmp_path / "data" / "phone-access-token"))
    configured = Settings(_env_file=None).access_token_file
    assert access_token.token_path(configured) == tmp_path / "data" / "phone-access-token"
    # Relative paths start at the project root, whatever the working directory.
    monkeypatch.chdir(tmp_path)
    assert access_token.token_path("state/token") == root / "state" / "token"
    assert access_token.token_path("  ") == root / ".runtime" / "phone-access-token"


def test_a_configured_directory_keeps_its_permissions(monkeypatch, tmp_path):
    volume = tmp_path / "data"
    volume.mkdir(mode=0o755)
    volume.chmod(0o755)
    monkeypatch.setattr(access_token, "TOKEN_PATH", volume / "phone-access-token")
    access_token.ensure_token()
    assert stat.S_IMODE(volume.stat().st_mode) == 0o755
    assert stat.S_IMODE(access_token.TOKEN_PATH.stat().st_mode) == 0o600


def test_the_docker_image_keeps_the_token_in_its_data_volume():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    # /app belongs to root in the image; the non-root app user can write only /data.
    assert "ACCESS_TOKEN_FILE=/data/phone-access-token" in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile and "chown app /data" in dockerfile
