"""Host and Origin checks against DNS rebinding and cross-site writes, the phone-access
token for requests from other devices, and image limits on /ask."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.main as main
import app.service as service
from app import access_token
from app.config import settings
from app.models import ModelConfig
from app.routers import nodes
from app.schemas import MAX_IMAGE_URL_CHARS, MAX_QUESTION_IMAGES

TAILSCALE = "laptop.tail1234.ts.net"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The production default, unlike the rest of the suite (see tests/conftest.py).
    monkeypatch.setattr(settings, "access_token_mode", "required")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'guards.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add(
            ModelConfig(
                label="演示", base_url="mock", llm_model="mock", api_key="mock", is_default=True
            )
        )
        session.commit()
    yield TestClient(main.app, base_url="http://127.0.0.1:8099")
    main.app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8099", "localhost:8099", "[::1]:8099", "laptop.tail1234.ts.net"]
)
def test_local_and_tailscale_hosts_are_served(client, host):
    assert client.get("/health", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["attacker.example:8099", "192.168.1.20:8099", "ts.net.evil.com"])
def test_other_hosts_are_rejected(client, host):
    response = client.get("/trees", headers={"host": host})
    assert response.status_code == 400


def test_configured_extra_host_is_served(client, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "learning.local, other.local")
    assert client.get("/health", headers={"host": "learning.local:8099"}).status_code == 200


@pytest.mark.parametrize(
    "origin", ["https://attacker.example", "null", "http://127.0.0.1.evil.com"]
)
def test_cross_site_writes_are_rejected(client, origin):
    response = client.post("/trees", json={"title": "x"}, headers={"origin": origin})
    assert response.status_code == 403
    assert client.get("/trees").json() == []  # Nothing was created.


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8099", "http://localhost:5174", "http://[::1]:8099"]
)
def test_local_pages_may_write(client, origin):
    response = client.post("/trees", json={"title": "ok"}, headers={"origin": origin})
    assert response.status_code == 200


def test_same_origin_tailscale_page_may_write(client):
    # A paired phone: same origin and the access cookie (unpaired: next test).
    token = access_token.ensure_token()
    response = client.post(
        "/trees",
        json={"title": "phone"},
        headers={
            "host": TAILSCALE,
            "origin": f"https://{TAILSCALE}",
            "cookie": f"{access_token.COOKIE_NAME}={token}",
        },
    )
    assert response.status_code == 200


def test_unpaired_tailscale_page_cannot_read_or_write(client):
    access_token.ensure_token()
    headers = {"host": TAILSCALE, "origin": f"https://{TAILSCALE}"}
    denied = client.post("/trees", json={"title": "phone"}, headers=headers)
    assert denied.status_code == 401 and denied.json()["detail"] == main.PAIRING_REQUIRED
    assert client.get("/trees", headers={"host": TAILSCALE}).status_code == 401
    assert client.get("/trees").json() == []  # Nothing was created.


@pytest.mark.parametrize("cookie", [None, "", "wrong-token-" + "x" * 40, "令牌"])
def test_missing_or_wrong_token_is_refused(client, cookie):
    access_token.ensure_token()
    headers = {"host": TAILSCALE}
    if cookie is not None:
        # Raw bytes: a forged cookie need not be ASCII.
        headers["cookie"] = f"{access_token.COOKIE_NAME}={cookie}".encode()
    assert client.get("/trees", headers=headers).status_code == 401


def test_no_token_yet_refuses_every_remote_request(client):
    assert access_token.current_token() is None
    for cookie in ("", "anything"):
        headers = {"host": TAILSCALE, "cookie": f"{access_token.COOKIE_NAME}={cookie}"}
        assert client.get("/trees", headers=headers).status_code == 401


def test_rotated_token_unpairs_every_device(client):
    old = access_token.ensure_token()
    paired = {"host": TAILSCALE, "cookie": f"{access_token.COOKIE_NAME}={old}"}
    assert client.get("/trees", headers=paired).status_code == 200
    new = access_token.rotate_token()
    assert new != old
    assert client.get("/trees", headers=paired).status_code == 401
    repaired = {"host": TAILSCALE, "cookie": f"{access_token.COOKIE_NAME}={new}"}
    assert client.get("/trees", headers=repaired).status_code == 200


@pytest.mark.parametrize("path", ["/", "/health", "/assets/index.js"])
def test_app_shell_assets_and_health_need_no_token(client, path):
    assert client.get(path, headers={"host": TAILSCALE}).status_code != 401


@pytest.mark.parametrize("path", ["/phone-access/session", "/api/phone-access/session"])
def test_pairing_route_is_reachable_without_a_token(client, path):
    response = client.post(path, json={"token": "wrong"}, headers={"host": TAILSCALE})
    # The route itself answered: a wrong pairing token, not the guard's refusal.
    assert response.status_code == 401 and response.json()["detail"] != main.PAIRING_REQUIRED


@pytest.mark.parametrize(
    "path", ["/api/trees", "/docs", "/phone-access", "/api/phone-access/rotate", "/assets"]
)
def test_everything_else_needs_the_token(client, path):
    assert client.get(path, headers={"host": TAILSCALE}).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        ("x-forwarded-for", "100.64.0.7"),
        ("x-forwarded-host", TAILSCALE),
        ("tailscale-user-login", "learner@example.com"),
        ("forwarded", "for=100.64.0.7"),
    ],
)
def test_proxied_requests_count_as_remote_even_with_a_loopback_host(client, header):
    # Tailscale Serve proxies from this computer: the Host may look local, the headers
    # still reveal the proxy.
    headers = {"host": "127.0.0.1:8099", header[0]: header[1]}
    assert client.get("/trees", headers=headers).status_code == 401
    token = access_token.ensure_token()
    headers["cookie"] = f"{access_token.COOKIE_NAME}={token}"
    assert client.get("/trees", headers=headers).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1:8099", "localhost:5174", "[::1]:8099"])
def test_this_computer_never_needs_a_token(client, host):
    access_token.ensure_token()
    assert client.get("/trees", headers={"host": host}).status_code == 200
    assert client.post("/trees", json={"title": "local"}, headers={"host": host}).status_code == 200


def test_configured_extra_host_counts_as_remote(client, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "learning.local")
    assert client.get("/trees", headers={"host": "learning.local:8099"}).status_code == 401
    token = access_token.ensure_token()
    cookie = f"{access_token.COOKIE_NAME}={token}"
    headers = {"host": "learning.local:8099", "cookie": cookie}
    assert client.get("/trees", headers=headers).status_code == 200


def test_token_mode_off_restores_host_only_checks(client, monkeypatch):
    monkeypatch.setattr(settings, "access_token_mode", "off")
    assert client.get("/trees", headers={"host": TAILSCALE}).status_code == 200
    assert client.get("/trees", headers={"host": "attacker.example"}).status_code == 400


def test_requests_without_origin_are_allowed(client):
    # Scripts and same-origin navigations; the Host check still applies.
    assert client.post("/trees", json={"title": "cli"}).status_code == 200


def test_ask_rejects_too_many_or_oversized_images(client):
    root = client.post("/trees", json={"title": "images"}).json()["root_node_id"]
    small = "data:image/png;base64,AAAA"
    too_many = [small] * (MAX_QUESTION_IMAGES + 1)
    response = client.post(f"/nodes/{root}/ask", json={"question": "看图", "images": too_many})
    assert response.status_code == 422
    huge = "data:image/png;base64," + "A" * MAX_IMAGE_URL_CHARS
    response = client.post(f"/nodes/{root}/ask", json={"question": "看图", "images": [huge]})
    assert response.status_code == 422
    ok = client.post(f"/nodes/{root}/ask", json={"question": "看图", "images": [small] * 4})
    assert ok.status_code == 200
