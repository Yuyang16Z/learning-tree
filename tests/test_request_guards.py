"""Host and Origin checks against DNS rebinding and cross-site writes; image limits on /ask."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.main as main
import app.service as service
from app.config import settings
from app.models import ModelConfig
from app.routers import nodes
from app.schemas import MAX_IMAGE_URL_CHARS, MAX_QUESTION_IMAGES


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    host = "laptop.tail1234.ts.net"
    response = client.post(
        "/trees",
        json={"title": "phone"},
        headers={"host": host, "origin": f"https://{host}"},
    )
    assert response.status_code == 200


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
