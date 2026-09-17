"""Never send fixture conversations to a real summary provider."""

import pytest


@pytest.fixture(autouse=True)
def offline_learning_summaries(monkeypatch):
    from app import learning_summaries

    # Provider unit tests exercise the separate provider module directly. Tests
    # needing generated summaries install their own controlled implementation.
    monkeypatch.setattr(learning_summaries, "summarize_learning_context", lambda *a, **k: None)
