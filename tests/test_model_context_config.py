"""Context budget settings preserve legacy model data and reject unusable budgets."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, create_engine, select

from app import db
from app.models import ModelConfig
from app.schemas import ModelConfigIn
from app.service import to_spec


@pytest.fixture
def legacy_models(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-context.db'}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE modelconfig (id INTEGER PRIMARY KEY, label TEXT NOT NULL, "
                "base_url TEXT NOT NULL, llm_model TEXT NOT NULL, api_key TEXT NOT NULL, "
                "protocol TEXT NOT NULL, max_tokens INTEGER NOT NULL, "
                "is_default BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        for identifier, protocol, maximum in [(71, "anthropic", 131072), (72, "openai", 4096)]:
            connection.execute(
                text(
                    "INSERT INTO modelconfig VALUES "
                    "(:id, 'Synthetic model', 'https://mock.invalid', 'mock', 'mock', "
                    ":protocol, :maximum, 0, '2026-01-01')"
                ),
                {"id": identifier, "protocol": protocol, "maximum": maximum},
            )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    yield engine
    engine.dispose()


def test_legacy_context_window_backfill_preserves_large_output_limits_and_later_choices(
    legacy_models,
):
    with Session(legacy_models) as session:
        models = session.exec(select(ModelConfig).order_by(ModelConfig.id)).all()
        assert [(model.id, model.max_tokens, model.context_window) for model in models] == [
            (71, 131072, 266240),
            (72, 4096, 32768),
        ]
        assert all(model.api_key == "mock" for model in models)
        models[1].context_window = 65536
        session.add(models[1])
        session.commit()
    db.init_db()
    with Session(legacy_models) as session:
        assert session.get(ModelConfig, 72).context_window == 65536


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_model_api_roundtrips_context_budget_without_exposing_keys(legacy_models, protocol):
    from app.main import app

    def session_override():
        with Session(legacy_models) as session:
            yield session

    app.dependency_overrides[db.get_session] = session_override
    try:
        api = TestClient(app)
        payload = {
            "label": "Synthetic budget",
            "base_url": "https://mock.invalid/v1",
            "llm_model": "mock",
            "api_key": "synthetic-test-value",
            "protocol": protocol,
            "max_tokens": 8192,
            "context_window": 65536,
        }
        created = api.post("/models", json=payload)
        assert created.status_code == 200, created.text
        identifier = created.json()["id"]
        assert created.json()["context_window"] == 65536
        assert "api_key" not in created.json() and "synthetic-test-value" not in created.text
        payload.update(context_window=98304, api_key="")
        updated = api.put(f"/models/{identifier}", json=payload)
        assert updated.status_code == 200, updated.text
        assert updated.json()["context_window"] == 98304
        with Session(legacy_models) as session:
            config = session.get(ModelConfig, identifier)
            assert config.api_key == "synthetic-test-value"
            assert to_spec(config).context_window == 98304
            assert to_spec(config).max_tokens == 8192
        payload["context_window"] = 9216  # Must leave more than 1024 tokens above the reserve.
        rejected = api.put(f"/models/{identifier}", json=payload)
        assert rejected.status_code == 422
        assert (
            next(item for item in api.get("/models").json() if item["id"] == identifier)[
                "context_window"
            ]
            == 98304
        )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("context_window", [8191, 2097153, 8192.5])
def test_model_context_window_rejects_invalid_capacity(context_window):
    with pytest.raises(ValueError):
        ModelConfigIn(
            label="Synthetic",
            base_url="https://mock.invalid",
            llm_model="mock",
            api_key="mock",
            context_window=context_window,
        )


@pytest.mark.parametrize("context_window", [8192, 2097152])
def test_model_context_window_accepts_supported_boundaries(context_window):
    config = ModelConfigIn(
        label="Synthetic",
        base_url="https://mock.invalid",
        llm_model="mock",
        api_key="mock",
        context_window=context_window,
    )
    assert config.context_window == context_window


def test_answer_reserve_must_leave_space_after_proportional_headroom():
    with pytest.raises(ValueError):
        ModelConfigIn(
            label="Synthetic",
            base_url="https://mock.invalid",
            llm_model="mock",
            api_key="mock",
            context_window=132200,
            max_tokens=131072,
        )
