"""Memory editing uses isolated SQLite records and deterministic extraction doubles."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.service as service
from app.context import build_context
from app.context_budget import ContextPolicy, estimate_request_tokens
from app.llm import LLMSpec
from app.memory_preferences import PROFILE_HEADING
from app.models import KnowledgeTree, Memory, MemoryEmbedding, Message, Node, PreferenceProfile
from app.retrieval import retrieve_memory
from app.routers.memory_router import router


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memories.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(service, "engine", engine)
    app = FastAPI()
    app.include_router(router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="Harness"), KnowledgeTree(id=2, title="MCP")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="根", status="complete"),
                Node(id=2, tree_id=1, title="兄弟分支", status="complete"),
                Node(id=3, tree_id=2, title="另一话题", status="complete"),
            ]
        )
        session.commit()
    yield TestClient(app), engine
    engine.dispose()


def add(engine, content, kind="fact", tree_id=1, source_node_id=1):
    with Session(engine) as session:
        memory = Memory(content=content, kind=kind, tree_id=tree_id, source_node_id=source_node_id)
        session.add(memory)
        session.commit()
        session.refresh(memory)
        return memory


def save(client, content, revision=None):
    revision = revision or client.get("/memories/preferences").json()["revision"]
    return client.put("/memories/preferences", json={"content": content, "revision": revision})


def test_additive_profile_table_preserves_existing_memory_ids(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(engine, tables=[Memory.__table__])
    with Session(engine) as session:
        session.add(Memory(id=104, kind="preference", content="原有偏好"))
        session.commit()
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    assert "preferenceprofile" in inspect(engine).get_table_names()
    with Session(engine) as session:
        assert session.get(Memory, 104).content == "原有偏好"
        assert session.get(PreferenceProfile, 1) is None
    engine.dispose()


def test_initial_profile_aggregates_without_mutation_or_truncation(setup):
    client, engine = setup
    add(engine, "  喜欢例子  ", "preference", None, None)
    add(engine, "喜欢例子", "preference", None, None)
    long_preference = "保留原文。" * 1300
    add(engine, long_preference, "preference", None, None)
    response = client.get("/memories/preferences").json()
    assert response["content"] == "喜欢例子\n\n" + long_preference
    assert not response["managed"]
    assert client.get("/memories/preferences").json() == response
    assert save(client, response["content"], response["revision"]).status_code == 422
    with Session(engine) as session:
        assert len(session.exec(select(Memory)).all()) == 3
        assert session.get(PreferenceProfile, 1) is None


def test_profile_first_save_and_updates_reject_stale_editors(setup):
    client, engine = setup
    initial = client.get("/memories/preferences").json()
    add(engine, "自动提取的新偏好", "preference", None, None)
    assert save(client, "旧草稿", initial["revision"]).status_code == 409
    saved = save(client, "  我会自己编辑偏好。  ")
    assert saved.status_code == 200
    assert saved.json()["content"] == "我会自己编辑偏好。"
    assert saved.json()["managed"]
    revision = saved.json()["revision"]
    assert save(client, "新的偏好", revision).status_code == 200
    assert save(client, "过时的偏好", revision).status_code == 409
    assert client.get("/memories/preferences").json()["content"] == "新的偏好"


def test_concurrent_first_saves_have_one_winner(setup):
    client, engine = setup
    revision = client.get("/memories/preferences").json()["revision"]
    barrier = Barrier(2)

    def update(content):
        barrier.wait(timeout=5)
        return save(client, content, revision).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ["中文说明", "英文说明"]))
    assert sorted(results) == [200, 409]
    with Session(engine) as session:
        assert len(session.exec(select(PreferenceProfile)).all()) == 1


def test_full_manual_profile_is_retrieved_without_legacy_or_fake_ids(setup):
    client, engine = setup
    add(engine, "旧自动偏好", "preference", None, None)
    content = "请先举例，最后再给出公式。\n" * 100 + "最后一句也必须保留。"
    assert save(client, content).status_code == 200
    with Session(engine) as session:
        result = retrieve_memory(session, 1, [1])
    assert result.preference_profile == content
    assert content in result.text
    assert "旧自动偏好" not in result.text
    assert not result.preferences
    assert "[记忆" not in result.text
    assert "当前明确要求优先" in result.text


def test_empty_profile_overrides_legacy_preferences_and_survives_clear(setup):
    client, engine = setup
    legacy = add(engine, "旧自动偏好", "preference", None, None)
    with Session(engine) as session:
        assert "旧自动偏好" in retrieve_memory(session, 1, [1]).text
    saved = save(client, "").json()
    with Session(engine) as session:
        assert retrieve_memory(session, 1, [1]).text == ""
        assert session.get(Memory, legacy.id), "legacy rows are retained for compatibility"
    assert client.delete("/memories").json() == {"cleared": 1}
    assert save(client, "过时草稿", saved["revision"]).status_code == 409
    assert client.get("/memories/preferences").json()["managed"]
    assert client.get("/memories/preferences").json()["content"] == ""


def test_manual_profile_is_atomic_during_context_compaction(setup):
    client, engine = setup
    content = "偏好保留前提和条件。\n" * 110 + "结尾：不要删除否定条件。"
    assert save(client, content).status_code == 200
    with Session(engine) as session:
        note = retrieve_memory(session, 1, [1]).text
    root = Node(id=1, tree_id=1, title="过去")
    current = Node(id=2, tree_id=1, title="当前")
    old = [Message(id=1, node_id=1, role="user", content="很长的历史内容。" * 8000)]
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    diagnostics = {}
    system, messages = build_context(
        [(root, old)],
        current,
        [],
        "现在的问题",
        memory_note=note,
        policy=policy,
        diagnostics=diagnostics,
    )
    assert diagnostics["compacted"]
    assert content in system
    assert system.count(PROFILE_HEADING) == 1
    assert estimate_request_tokens(system, messages) <= policy.input_budget
    # Small contexts must still answer the mandatory question without a chopped preference.
    small = ContextPolicy(window_tokens=4096, output_tokens=1024)
    system, messages = build_context(
        [(root, old)], current, [], "现在的问题", memory_note=note, policy=small
    )
    assert PROFILE_HEADING not in system
    assert "不要删除否定条件" not in system
    assert estimate_request_tokens(system, messages) <= small.input_budget


SPEC = LLMSpec(label="test", base_url="https://example.invalid", llm_model="test", api_key="test")


def test_first_manual_save_during_extraction_prevents_automatic_preferences(setup, monkeypatch):
    client, engine = setup

    def extraction(*args):
        assert save(client, "只使用我编辑的偏好").status_code == 200
        return {"preferences": ["不应自动写入"], "facts": ["保留的学习事实"]}

    monkeypatch.setattr(service, "extract_memories", extraction)
    service.extract_and_save(SPEC, 1, 1, "问题", "回答")
    memories = client.get("/memories").json()
    assert [memory["content"] for memory in memories] == ["保留的学习事实"]
    assert client.get("/memories/preferences").json()["content"] == "只使用我编辑的偏好"


def test_clear_all_prevents_inflight_extraction_from_restoring_memories(setup, monkeypatch):
    client, engine = setup
    add(engine, "准备清除的事实")

    def extraction(*args):
        client.delete("/memories")
        return {"preferences": ["不应恢复"], "facts": ["不应恢复的事实"]}

    monkeypatch.setattr(service, "extract_memories", extraction)
    service.extract_and_save(SPEC, 1, 1, "问题", "回答")
    assert client.get("/memories").json() == []
    assert client.get("/memories/preferences").json()["content"] == ""


def test_unverified_legacy_extractor_strings_do_not_create_new_preferences(setup, monkeypatch):
    client, engine = setup
    monkeypatch.setattr(
        service,
        "extract_memories",
        lambda *args: {"preferences": ["长期偏好生活化例子"], "facts": ["话题结论"]},
    )
    service.extract_and_save(SPEC, 1, 1, "问题", "回答")
    service.extract_and_save(SPEC, 1, 1, "问题", "回答")
    assert len(client.get("/memories").json()) == 1
    profile = client.get("/memories/preferences").json()
    assert profile["content"] == "" and not profile["managed"]


def test_slow_retrieval_refreshes_profile_and_cannot_recreate_edited_fact_vector(setup):
    client, engine = setup
    fact = add(engine, "Original schema fact")
    assert save(client, "原来的偏好").status_code == 200

    class EditingBackend:
        model_key = "concurrent-test-model"

        def embed_query(self, query):
            return [1.0, 0.0]

        def embed_documents(self, documents):
            assert save(client, "检索期间更新的偏好").status_code == 200
            response = client.patch(
                f"/memories/{fact.id}",
                json={"content": "Edited schema fact", "expected_content": fact.content},
            )
            assert response.status_code == 200
            return [[1.0, 0.0] for _ in documents]

        def rerank(self, query, documents):
            return [1.0 for _ in documents]

    with Session(engine) as session:
        result = retrieve_memory(session, 1, [1], query="schema", backend=EditingBackend())
    assert "检索期间更新的偏好" in result.text
    assert "原来的偏好" not in result.text
    assert not result.facts
    with Session(engine) as session:
        assert session.exec(select(MemoryEmbedding)).all() == []
        result = retrieve_memory(session, 1, [1])
        assert "Edited schema fact" in result.text


def test_facts_search_topics_and_stable_pagination(setup):
    client, engine = setup
    records = [add(engine, f"Harness fact {index}") for index in range(12)]
    add(engine, "Other topic", tree_id=2, source_node_id=3)
    add(engine, "不要出现在事实中的偏好", "preference", None, None)
    literal = add(engine, "Literal 100%_match")
    first = client.get("/memories/facts", params={"tree_id": 1, "page_size": 5}).json()
    second = client.get("/memories/facts", params={"tree_id": 1, "page_size": 5, "page": 2}).json()
    assert first["total"] == 13 and len(first["items"]) == len(second["items"]) == 5
    assert first["items"][0]["id"] == literal.id
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    assert first["topics"] == [
        {"tree_id": 1, "title": "Harness", "count": 13},
        {"tree_id": 2, "title": "MCP", "count": 1},
    ]
    found = client.get("/memories/facts", params={"q": "hARnEss fact 11"}).json()
    assert found["total"] == 1 and found["items"][0]["id"] == records[-1].id
    assert found["topics"] == first["topics"]
    assert found["items"][0]["tree_title"] == "Harness"
    assert found["items"][0]["source_node_id"] == 1
    assert found["items"][0]["created_at"]
    assert client.get("/memories/facts", params={"q": "%_"}).json()["total"] == 1
    assert client.get("/memories/facts", params={"page": 0}).status_code == 422
    assert client.get("/memories/facts", params={"page_size": 101}).status_code == 422


def test_fact_edit_is_optimistic_invalidates_vectors_and_retains_source_boundary(setup):
    client, engine = setup
    fact = add(engine, "原事实")
    sibling = add(engine, "兄弟分支事实", source_node_id=2)
    preference = add(engine, "个人偏好", "preference", None, None)
    with Session(engine) as session:
        session.add(
            MemoryEmbedding(memory_id=fact.id, model_key="test", content_hash="old", vector=[1.0])
        )
        session.commit()
    patched = client.patch(
        f"/memories/{fact.id}", json={"content": "  新事实  ", "expected_content": fact.content}
    )
    assert patched.status_code == 200
    assert patched.json()["content"] == "新事实"
    assert patched.json()["source_node_id"] == fact.source_node_id
    assert patched.json()["tree_id"] == fact.tree_id
    assert (
        client.patch(
            f"/memories/{fact.id}", json={"content": "旧草稿", "expected_content": fact.content}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/memories/{fact.id}", json={"content": " \n ", "expected_content": "新事实"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/memories/{preference.id}",
            json={"content": "不能在事实区修改偏好", "expected_content": preference.content},
        ).status_code
        == 422
    )
    with Session(engine) as session:
        assert session.exec(select(MemoryEmbedding)).all() == []
        result = retrieve_memory(session, 1, [1])
        assert {hit.memory_id for hit in result.facts} == {fact.id}
        assert "新事实" in result.text and sibling.content not in result.text


def test_batch_delete_only_facts_clears_vectors_and_preserves_other_records(setup):
    client, engine = setup
    fact = add(engine, "删除这一条")
    retained = add(engine, "保留这一条")
    preference = add(engine, "保留个人偏好", "preference", None, None)
    with Session(engine) as session:
        session.add(
            MemoryEmbedding(memory_id=fact.id, model_key="test", content_hash="old", vector=[1.0])
        )
        session.commit()
    response = client.post("/memories/delete", json={"ids": [fact.id, fact.id, preference.id, 999]})
    assert response.json() == {"deleted": 1}
    with Session(engine) as session:
        assert session.get(Memory, fact.id) is None
        assert session.get(Memory, retained.id) and session.get(Memory, preference.id)
        assert session.exec(select(MemoryEmbedding)).all() == []
    assert client.post("/memories/delete", json={"ids": list(range(1001))}).status_code == 422
    assert client.delete(f"/memories/{retained.id}").status_code == 200
