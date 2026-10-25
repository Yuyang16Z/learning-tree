"""A learner can stop a memory from being used in answers; restated facts are not stored twice.

Temporary SQLite and offline doubles only.
"""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.service as service
from app.answer_sources import resolve_sources
from app.answer_sources import sources_for_storage as service_storage
from app.llm import LLMSpec
from app.models import KnowledgeTree, Memory, Node
from app.retrieval import RetrievalConfig, retrieve_memory
from app.routers import memory_router, nodes, trees
from app.search_index import make_history_search, search_text

SPEC = LLMSpec(
    label="Offline", base_url="https://offline.invalid/v1", llm_model="synthetic", api_key="k"
)
FACT = "Quicksort 不是稳定排序：相等元素的次序可能改变。"
NO_SCOPES = {"include_siblings": False, "include_other_trees": False}


class LexicalOnly:
    model_key = "unused"

    def embed_query(self, query):
        raise RuntimeError("semantic models unavailable")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (service, nodes, database):
        monkeypatch.setattr(module, "engine", engine)
    application = FastAPI()
    for router in (trees.router, nodes.router, memory_router.router):
        application.include_router(router)

    def session_override():
        with Session(engine) as session:
            yield session

    application.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="排序"))
        session.add_all(
            [
                Node(id=1, tree_id=1, title="根", status="complete"),
                Node(id=2, tree_id=1, parent_id=1, title="追问", status="complete"),
                Node(id=3, tree_id=1, parent_id=1, title="兄弟分支", status="complete"),
            ]
        )
        session.commit()
    yield TestClient(application), engine
    nodes._GENERATIONS.clear()
    engine.dispose()


_TURNS = iter(range(1, 1000))


def extract(monkeypatch, node_id, facts):
    """Extraction of one new turn: each turn is claimed once, so every call asks anew."""
    extracted = []

    def extract_memories(*args):
        extracted.append(True)
        return {"preferences": [], "facts": facts}

    monkeypatch.setattr(service, "extract_memories", extract_memories)
    service.extract_and_save(SPEC, 1, node_id, f"问题 {next(_TURNS)}", "回答")
    assert extracted, "the extraction ran"


def facts(engine):
    with Session(engine) as session:
        return session.exec(select(Memory).where(Memory.kind == "fact").order_by(Memory.id)).all()


def recalled(engine, path, config=None):
    with Session(engine) as session:
        result = retrieve_memory(session, 1, path, "稳定排序", config=config, backend=LexicalOnly())
        return [hit.memory_id for hit in result.facts]


def test_a_muted_fact_leaves_answers_but_stays_listed_and_can_be_used_again(setup, monkeypatch):
    client, engine = setup
    extract(monkeypatch, 1, [FACT])
    (memory,) = facts(engine)
    assert recalled(engine, [1, 2]) == [memory.id]
    response = client.put(f"/memories/{memory.id}/use", json={"muted": True})
    assert response.status_code == 200 and response.json()["muted"] is True
    assert recalled(engine, [1, 2]) == []
    wide = RetrievalConfig(include_siblings=True, include_other_trees=True)
    assert recalled(engine, [1, 3], wide) == [] and recalled(engine, [3], wide) == []
    listed = client.get("/memories/facts").json()["items"]
    assert [(item["id"], item["muted"]) for item in listed] == [(memory.id, True)]
    # The learner's own map search still finds it; the answer model's history search does not.
    with Session(engine) as session:
        assert [hit.kind for hit in search_text(session, "稳定排序", tree_id=1)] == ["memory"]
    search = make_history_search(engine, 1, [1, 2], NO_SCOPES)
    assert json.loads(search({"query": "稳定排序"}))["results"] == []
    response = client.put(f"/memories/{memory.id}/use", json={"muted": False})
    assert response.status_code == 200 and response.json()["muted"] is False
    assert recalled(engine, [1, 2]) == [memory.id]
    assert [item["kind"] for item in json.loads(search({"query": "稳定排序"}))["results"]] == [
        "memory"
    ]


def test_the_referenced_line_knows_which_memories_are_muted(setup, monkeypatch):
    client, engine = setup
    extract(monkeypatch, 1, [FACT])
    (memory,) = facts(engine)
    client.put(f"/memories/{memory.id}/use", json={"muted": True})
    stored = [{"memory_id": memory.id, "source_node_id": 1, "tree_id": 1, "scope": "path"}]
    with Session(engine) as session:
        ((source,),) = resolve_sources(session, [stored])
        assert source["memory_id"] == memory.id and source["muted"] is True
        # The flag is read live, never stored with an answer.
        assert service_storage(session, stored) == stored


def test_the_use_endpoint_answers_only_for_existing_facts(setup):
    client, engine = setup
    assert client.put("/memories/404/use", json={"muted": True}).status_code == 404
    with Session(engine) as session:
        session.add(Memory(id=9, kind="preference", content="喜欢先看例子"))
        session.commit()
    assert client.put("/memories/9/use", json={"muted": True}).status_code == 422
    assert client.put("/memories/9/use", json={"muted": "yes"}).status_code == 422


def test_a_restated_fact_on_the_same_path_is_not_stored_twice(setup, monkeypatch):
    client, engine = setup
    extract(monkeypatch, 1, [FACT])
    # Case, width and spacing differences are the same fact, as recall compares them.
    extract(monkeypatch, 2, ["quicksort  不是稳定排序：相等元素的次序可能改变。"])
    assert [memory.content for memory in facts(engine)] == [FACT]
    # A muted fact is not brought back by being extracted again.
    (memory,) = facts(engine)
    client.put(f"/memories/{memory.id}/use", json={"muted": True})
    extract(monkeypatch, 2, [FACT])
    assert len(facts(engine)) == 1 and recalled(engine, [1, 2]) == []
    # A sibling branch keeps its own copy of a fact from the other branch: node 2 is not
    # on its path. (The root's fact is on every path below the root, so it stays single.)
    other = "Mergesort 是稳定排序。"
    extract(monkeypatch, 2, [other])
    extract(monkeypatch, 3, [other, FACT])
    copies = [memory.source_node_id for memory in facts(engine) if memory.content == other]
    assert copies == [2, 3]
    assert [memory.content for memory in facts(engine)].count(FACT) == 1


def test_existing_facts_start_in_use_after_the_migration(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-memory.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE memory (id INTEGER PRIMARY KEY, kind VARCHAR NOT NULL, "
                "content VARCHAR NOT NULL, tree_id INTEGER, source_node_id INTEGER, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO memory VALUES (7, 'fact', 'Legacy fact', 1, 1, '2026-01-01')")
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    with Session(engine) as session:
        assert session.get(Memory, 7).muted is False
    engine.dispose()
