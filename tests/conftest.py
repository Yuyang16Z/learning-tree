"""Never send fixture conversations to a real summary provider."""

import os

import pytest

# Starlette's TestClient sends "Host: testserver"; the app only accepts loopback and
# Tailscale names by default (DNS-rebinding protection), so allow the test host.
os.environ.setdefault("ALLOWED_HOSTS", "testserver")
# That Host is not a loopback name either, so the phone-access check would treat every
# TestClient request as coming from another device. Tests of that check turn it on
# explicitly (tests/test_request_guards.py, tests/test_phone_access.py).
os.environ.setdefault("ACCESS_TOKEN_MODE", "off")


@pytest.fixture(autouse=True)
def offline_learning_summaries(monkeypatch):
    from app import learning_summaries

    # Provider unit tests exercise the separate provider module directly. Tests
    # needing generated summaries install their own controlled implementation.
    monkeypatch.setattr(learning_summaries, "summarize_learning_context", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def isolated_phone_access_token(tmp_path, monkeypatch):
    """No test may create or read the repository's real .runtime/phone-access-token."""
    from app import access_token

    monkeypatch.setattr(access_token, "TOKEN_PATH", tmp_path / "runtime" / "phone-access-token")
    access_token.pairing_failures.reset()
