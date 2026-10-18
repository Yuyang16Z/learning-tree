"""A superseded fact stays current only while the fact that replaced it exists."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.service as service
from app.llm import LLMSpec
from app.models import KnowledgeTree, Memory, Node
from app.retrieval import retrieve_memory
from app.routers import memory_router, nodes, trees
from app.search_index import search_text

SPEC = LLMSpec(
    label="Offline", base_url="https://offline.invalid/v1", llm_model="synthetic", api_key="k"
)
OLD = "旧结论：quicksort 总是稳定的排序。"
NEW = "修正：quicksort 不是稳定的排序。"


class LexicalOnly:
    model_key = "unused"

    def embed_query(self, query):
        raise RuntimeError("semantic models unavailable")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'supersede.db'}", connect_args={"check_same_thread": False}
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
                Node(id=2, tree_id=1, parent_id=1, title="分支", status="complete"),
            ]
        )
        session.commit()
    yield TestClient(application), engine
    nodes._GENERATIONS.clear()
    engine.dispose()


def extract(monkeypatch, node_id, question, facts):
    monkeypatch.setattr(
        service, "extract_memories", lambda *args: {"preferences": [], "facts": facts}
    )
    service.extract_and_save(SPEC, 1, node_id, question, "回答")


def fact(engine, content) -> Memory:
    with Session(engine) as session:
        return session.exec(select(Memory).where(Memory.content == content)).one()


def supersede(engine, monkeypatch) -> tuple[int, int]:
    """The root's fact is corrected by a fact extracted on the branch below it."""
    extract(monkeypatch, 1, "问题一", [OLD])
    old = fact(engine, OLD)
    extract(
        monkeypatch,
        2,
        "问题二",
        [{"text": NEW, "type": "misconception", "terms": [], "supersedes": old.id}],
    )
    new = fact(engine, NEW)
    assert fact(engine, OLD).superseded_by == new.id
    return old.id, new.id


def current(engine, memory_id) -> bool:
    """Whether recall on the root and topic search both find the fact."""
    with Session(engine) as session:
        recalled = retrieve_memory(session, 1, [1], query="quicksort 稳定", backend=LexicalOnly())
        indexed = search_text(session, "总是稳定", kinds=["memory"])
        return memory_id in {hit.memory_id for hit in recalled.facts} and [
            hit.ref_id for hit in indexed
        ] == [memory_id]


@pytest.mark.parametrize("deletion", ["branch", "selected", "single"])
def test_deleting_the_replacing_fact_or_its_branch_restores_the_earlier_fact(
    setup, monkeypatch, deletion
):
    client, engine = setup
    old_id, new_id = supersede(engine, monkeypatch)
    assert not current(engine, old_id)
    if deletion == "branch":
        response = client.delete("/nodes/2")
    elif deletion == "selected":
        response = client.post("/memories/delete", json={"ids": [new_id]})
    else:
        response = client.delete(f"/memories/{new_id}")
    assert response.status_code == 200
    assert fact(engine, OLD).superseded_by is None
    assert current(engine, old_id)
    # A later fact may reuse the deleted id; the earlier fact must not point at it.
    extract(monkeypatch, 1, "问题三", ["另一条事实：堆排序也不稳定。"])
    assert fact(engine, "另一条事实：堆排序也不稳定。").id == new_id
    assert fact(engine, OLD).superseded_by is None
    # Being restored is not a hand edit: a later correction can still supersede it.
    assert fact(engine, OLD).updated_at is None
    extract(
        monkeypatch,
        1,
        "问题四",
        [{"text": "再次修正：quicksort 不稳定。", "type": "misconception", "supersedes": old_id}],
    )
    assert fact(engine, OLD).superseded_by == fact(engine, "再次修正：quicksort 不稳定。").id
