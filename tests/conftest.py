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
def no_background_summary_jobs(monkeypatch):
    """No post-answer summary job outlives its test. Tests of that job run the real one
    and join its thread (tests/test_context_integration.py)."""
    from app.routers import nodes

    monkeypatch.setattr(nodes, "_queue_context_summaries", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def isolated_phone_access_token(tmp_path, monkeypatch):
    """No test may create or read the repository's real .runtime/phone-access-token."""
    from app import access_token

    monkeypatch.setattr(access_token, "TOKEN_PATH", tmp_path / "runtime" / "phone-access-token")
    access_token.pairing_failures.reset()


@pytest.fixture(autouse=True)
def isolated_learning_presets(tmp_path, monkeypatch):
    """No test may read the learner's own styles in the repository's learning-presets/."""
    from app import learning_presets

    monkeypatch.setattr(learning_presets, "CUSTOM_DIRECTORY", tmp_path / "learning-presets")
