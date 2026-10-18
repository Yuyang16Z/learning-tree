"""Context-window discovery: provider model lists, the approximate catalog and the route."""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import httpx
import pytest
from anthropic import Anthropic
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlmodel import Session, SQLModel, create_engine

from app import anthropic_provider as adapter
from app import db, llm, model_catalog
from app.llm import DISCOVERY_NOTES, LLMSpec, discover_limits
from app.models import ModelConfig


@pytest.mark.parametrize(
    "model_id,window",
    [
        ("deepseek-v4-flash", 1_048_576),
        ("deepseek-flash", 1_048_576),
        ("deepseek-chat", 131_072),
        ("deepseek-reasoner", 131_072),
        ("claude-sonnet-4-6", 1_000_000),
        ("claude-opus-5-5", 1_000_000),
        ("claude-fable-5-1", 1_000_000),
        ("claude-sonnet-4-5-20250929", 200_000),
        # A date suffix is not a minor version: these are Claude 4.0 ids.
        ("claude-sonnet-4-20250514", 200_000),
        ("claude-opus-4-20250514", 200_000),
        ("claude-sonnet-4-20250514-thinking", 200_000),
        ("anthropic/claude-opus-4-20250514", 200_000),
        ("claude-opus-4-1-20250805", 200_000),
        ("claude-3-5-sonnet-20241022", 200_000),
        ("claude-sonnet-4-6-20260217", 1_000_000),
        ("claude-3-7-sonnet", 200_000),
        ("anthropic/claude-sonnet-4-6", 1_000_000),
        ("gpt-4.1-mini", 1_047_576),
        ("gpt-4o-mini", 128_000),
        # OpenAI lists gpt-5 at 400,000 including up to 128,000 of output; its input
        # stops at 272,000, which is what this app's window minus reserve must not pass.
        ("gpt-5", 272_000),
        ("gpt-5-mini", 272_000),
        ("gpt-5.1", 272_000),
        ("gpt-5-chat-latest", 128_000),
        ("gpt-5.1-chat-latest", 128_000),
        ("openai/gpt-5-chat", 128_000),
        ("o3", 200_000),
        ("o4-mini", 200_000),
        ("qwen3-235b-a22b", 131_072),
        ("kimi-k2-0905", 262_144),
        ("kimi-k2-0905-preview", 262_144),
        ("kimi-k2-turbo-preview", 262_144),
        ("kimi-k2-thinking", 262_144),
        ("moonshotai/Kimi-K2-Instruct-0905", 262_144),
        # The July 2025 weights have a 128K window.
        ("kimi-k2-0711-preview", 131_072),
        ("moonshotai/Kimi-K2-Instruct", 131_072),
        ("glm-4.6", 204_800),
        ("glm-4.5-air", 131_072),
        ("gemini-2.5-pro", 1_048_576),
        ("grok-4", 262_144),
        ("mistral-large-latest", 131_072),
        ("custom-model", None),
        ("", None),
    ],
)
def test_catalog_matches_known_families_and_gateway_prefixes(model_id, window):
    assert model_catalog.lookup(model_id) == window
    assert model_catalog.lookup(model_id.upper()) == window


def openai_models(monkeypatch, items, status=200, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        if status != 200:
            return httpx.Response(
                status, json={"error": {"message": "secret-provider-detail must not leak"}}
            )
        return httpx.Response(200, json={"object": "list", "data": items})

    monkeypatch.setattr(
        llm,
        "_client",
        lambda spec: OpenAI(
            base_url=spec.base_url,
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )


def spec(model="custom-model", protocol="openai", key="synthetic-key"):
    return LLMSpec("发现", "https://provider.test/v1", model, key, protocol=protocol)


def test_openai_compatible_list_extras_supply_provider_limits(monkeypatch):
    openai_models(
        monkeypatch,
        [
            {"id": "other", "object": "model", "owned_by": "x", "context_length": 8192},
            {
                "id": "custom-model",
                "object": "model",
                "owned_by": "x",
                "context_length": 131072,
                "max_output_tokens": 8192,
            },
        ],
    )
    result = discover_limits(spec())
    assert result == {
        "context_window": 131072,
        "max_output_tokens": 8192,
        "source": "provider",
        "note": DISCOVERY_NOTES["provider"],
    }


@pytest.mark.parametrize(
    "extras,window",
    [
        ({"context_window": 65536}, 65536),
        ({"max_model_len": 32768}, 32768),
        ({"max_context_length": 16384, "max_tokens": 4096}, 16384),
        ({"top_provider": {"context_length": 200000, "max_completion_tokens": 32000}}, 200000),
    ],
)
def test_every_known_window_key_is_read_without_created_timestamps(monkeypatch, extras, window):
    openai_models(monkeypatch, [{"id": "custom-model", "object": "model", **extras}])
    result = discover_limits(spec())
    assert result["context_window"] == window and result["source"] == "provider"


def test_deepseek_style_list_without_window_falls_back_to_the_catalog(monkeypatch):
    openai_models(monkeypatch, [{"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"}])
    result = discover_limits(spec("deepseek-chat"))
    assert result == {
        "context_window": 131072,
        "max_output_tokens": None,
        "source": "catalog",
        "note": DISCOVERY_NOTES["catalog"],
    }
    assert discover_limits(spec("custom-model")) == {
        "context_window": None,
        "max_output_tokens": None,
        "source": "unknown",
        "note": DISCOVERY_NOTES["unknown"],
    }


@pytest.mark.parametrize("model,source", [("gpt-4o", "catalog"), ("custom-model", "unknown")])
def test_provider_errors_never_leak_and_still_offer_the_catalog(monkeypatch, model, source):
    openai_models(monkeypatch, [], status=401)
    result = discover_limits(spec(model))
    assert result["source"] == source
    assert result["note"] == DISCOVERY_NOTES[f"{source}_after_error"]
    assert "secret-provider-detail" not in json.dumps(result)
    assert (result["context_window"] == 128000) == (source == "catalog")


def test_anthropic_model_info_supplies_input_and_output_limits(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "claude-sonnet-4-6",
                "type": "model",
                "display_name": "Claude Sonnet 4.6",
                "created_at": "2026-02-17T00:00:00Z",
                "max_input_tokens": 1000000,
                "max_tokens": 64000,
            },
        )

    monkeypatch.setattr(
        adapter,
        "client",
        lambda spec: Anthropic(
            base_url=adapter.base_url(spec.base_url),
            api_key=spec.api_key,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )
    result = discover_limits(spec("claude-sonnet-4-6", protocol="anthropic"))
    assert requests[0].url.path == "/v1/models/claude-sonnet-4-6"
    assert requests[0].headers["x-api-key"] == "synthetic-key"
    assert result == {
        "context_window": 1000000,
        "max_output_tokens": 64000,
        "source": "provider",
        "note": DISCOVERY_NOTES["provider"],
    }


def test_every_discovery_note_has_an_exact_interface_translation():
    from pathlib import Path

    table = (Path(__file__).resolve().parents[1] / "web/src/i18n/workspace.ts").read_text()
    assert all(f'["{note}", "' in table for note in DISCOVERY_NOTES.values())


def test_demo_models_never_construct_a_provider_client(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda spec: pytest.fail("no client in demo mode"))
    monkeypatch.setattr(adapter, "client", lambda spec: pytest.fail("no client in demo mode"))
    for demo in (spec("mock", key="mock"), spec(protocol="anthropic", key="mock")):
        assert discover_limits(demo)["source"] == "unknown"
        assert discover_limits(demo)["note"] == DISCOVERY_NOTES["mock"]


def test_discover_route_uses_the_stored_key_and_never_echoes_it(tmp_path, monkeypatch):
    from app.main import app

    engine = create_engine(
        f"sqlite:///{tmp_path / 'discover.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ModelConfig(
                id=4,
                label="Stored",
                base_url="https://provider.test/v1",
                llm_model="custom-model",
                api_key="stored-secret-key",
            )
        )
        session.commit()
    requests = []
    openai_models(
        monkeypatch,
        [{"id": "custom-model", "object": "model", "context_length": 65536}],
        requests=requests,
    )

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db.get_session] = session_override
    try:
        api = TestClient(app)
        body = {
            "protocol": "openai",
            "base_url": "https://provider.test/v1",
            "llm_model": "custom-model",
        }
        found = api.post("/models/discover", json={**body, "api_key": "", "config_id": 4})
        assert found.status_code == 200, found.text
        assert found.json() == {
            "context_window": 65536,
            "max_output_tokens": None,
            "source": "provider",
            "note": DISCOVERY_NOTES["provider"],
        }
        assert requests[-1].headers["authorization"] == "Bearer stored-secret-key"
        assert "stored-secret-key" not in found.text
        typed = api.post("/api/models/discover", json={**body, "api_key": "typed-key"})
        assert (
            typed.status_code == 200 and requests[-1].headers["authorization"] == "Bearer typed-key"
        )
        assert api.post("/models/discover", json={**body, "config_id": 99}).status_code == 404
        assert api.post("/models/discover", json={"llm_model": "x"}).status_code == 422
        with Session(engine) as session:
            assert session.get(ModelConfig, 4).context_window == 32768, "nothing is stored"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://some-other-relay.test/v1"},
        {"base_url": "https://provider.test/other/v1"},
        {"protocol": "anthropic"},
    ],
    ids=["other-host", "other-path", "other-protocol"],
)
def test_discover_route_never_sends_a_stored_key_to_another_endpoint(
    tmp_path, monkeypatch, changes
):
    from app.main import app

    engine = create_engine(
        f"sqlite:///{tmp_path / 'discover.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ModelConfig(
                id=4,
                label="Stored",
                base_url="https://provider.test/v1",
                llm_model="deepseek-chat",
                api_key="stored-secret-key",
            )
        )
        session.commit()
    requests = []
    openai_models(
        monkeypatch,
        [{"id": "deepseek-chat", "object": "model", "context_length": 65536}],
        requests=requests,
    )
    monkeypatch.setattr(adapter, "client", lambda spec: pytest.fail("no request may be sent"))

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db.get_session] = session_override
    try:
        api = TestClient(app)
        body = {
            "protocol": "openai",
            "base_url": "https://provider.test/v1",
            "llm_model": "deepseek-chat",
            "api_key": "",
            "config_id": 4,
            **changes,
        }
        found = api.post("/models/discover", json=body)
        assert found.status_code == 200, found.text
        assert requests == [], "the stored key stays with its own address and protocol"
        assert found.json() == {
            "context_window": model_catalog.lookup("deepseek-chat"),
            "max_output_tokens": None,
            "source": "catalog",
            "note": DISCOVERY_NOTES["catalog_needs_key"],
        }
        unknown = api.post("/models/discover", json={**body, "llm_model": "custom-model"})
        assert unknown.json() == {
            "context_window": None,
            "max_output_tokens": None,
            "source": "unknown",
            "note": DISCOVERY_NOTES["unknown_needs_key"],
        }
        assert requests == [] and "stored-secret-key" not in found.text + unknown.text
        # The saved endpoint, spelled with a trailing slash, still uses the stored key.
        same = {**body, "protocol": "openai", "base_url": "https://provider.test/v1/"}
        assert api.post("/models/discover", json=same).json()["source"] == "provider"
        assert [r.headers["authorization"] for r in requests] == ["Bearer stored-secret-key"]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
