"""Never send fixture conversations to a real summary provider."""

import os

import pytest

# Starlette's TestClient sends "Host: testserver"; the app only accepts loopback and
# Tailscale names by default (DNS-rebinding protection), so allow the test host.
os.environ.setdefault("ALLOWED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def offline_learning_summaries(monkeypatch):
    from app import learning_summaries

    # Provider unit tests exercise the separate provider module directly. Tests
    # needing generated summaries install their own controlled implementation.
    monkeypatch.setattr(learning_summaries, "summarize_learning_context", lambda *a, **k: None)
