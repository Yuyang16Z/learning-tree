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


LATEST = "再次修正：quicksort 不稳定，堆排序也不稳定。"


def chain(engine, monkeypatch) -> tuple[int, int, int]:
    """The root's fact is corrected on the branch, and that correction on the branch's child."""
    old_id, middle_id = supersede(engine, monkeypatch)
    with Session(engine) as session:
        session.add(Node(id=3, tree_id=1, parent_id=2, title="子分支", status="complete"))
        session.commit()
    extract(
        monkeypatch,
        3,
        "问题三",
        [{"text": LATEST, "type": "misconception", "terms": [], "supersedes": middle_id}],
    )
    latest_id = fact(engine, LATEST).id
    assert fact(engine, NEW).superseded_by == latest_id
    assert fact(engine, OLD).superseded_by == middle_id
    return old_id, middle_id, latest_id


def recalled(engine, path) -> set[int]:
    with Session(engine) as session:
        found = retrieve_memory(session, 1, path, query="quicksort 稳定", backend=LexicalOnly())
        return {hit.memory_id for hit in found.facts}


def delete_facts(client, deletion, ids) -> None:
    if deletion == "selected":
        assert client.post("/memories/delete", json={"ids": ids}).status_code == 200
    else:
        for memory_id in ids:
            assert client.delete(f"/memories/{memory_id}").status_code == 200


@pytest.mark.parametrize("deletion", ["selected", "single"])
def test_deleting_a_correction_that_was_corrected_again_keeps_the_earlier_fact_superseded(
    setup, monkeypatch, deletion
):
    client, engine = setup
    old_id, middle_id, latest_id = chain(engine, monkeypatch)
    assert recalled(engine, [1, 2, 3]) == {latest_id}
    delete_facts(client, deletion, [middle_id])
    # Corrected twice: the correction that remains still replaces the original.
    assert fact(engine, OLD).superseded_by == latest_id
    assert not current(engine, old_id)
    assert recalled(engine, [1, 2, 3]) == {latest_id}
    listed = client.get("/memories/facts").json()["items"]
    assert {item["id"]: item["superseded_by"] for item in listed} == {
        old_id: latest_id,
        latest_id: None,
    }
    # Once no correction is left, the original is current again.
    delete_facts(client, deletion, [latest_id])
    assert fact(engine, OLD).superseded_by is None
    assert current(engine, old_id)
    assert recalled(engine, [1, 2, 3]) == {old_id}


@pytest.mark.parametrize(
    ("deleted", "current_after"),
    [
        (("middle", "latest"), "old"),  # Every correction goes: the original returns.
        (("old", "middle"), "latest"),  # The original goes with its first correction.
        (("old",), "latest"),  # Deleting the original leaves the chain's end alone.
        (("latest",), "middle"),  # The last correction goes: the one before it returns.
    ],
)
def test_deleting_several_facts_of_a_correction_chain_together(
    setup, monkeypatch, deleted, current_after
):
    client, engine = setup
    ids = dict(zip(("old", "middle", "latest"), chain(engine, monkeypatch)))
    delete_facts(client, "selected", [ids[name] for name in deleted])
    with Session(engine) as session:
        remaining = {row.id: row.superseded_by for row in session.exec(select(Memory))}
    assert {memory_id for memory_id, successor in remaining.items() if successor is None} == {
        ids[current_after]
    }
    # Every fact that stays superseded points at a fact that still exists.
    assert all(successor in remaining for successor in remaining.values() if successor)
    assert recalled(engine, [1, 2, 3]) == {ids[current_after]}


def test_deleting_the_branch_of_a_twice_corrected_fact_restores_the_original(setup, monkeypatch):
    """Both corrections come from the deleted branch, so nothing replaces the root's fact."""
    client, engine = setup
    old_id, _, _ = chain(engine, monkeypatch)
    assert client.delete("/nodes/2").status_code == 200
    assert fact(engine, OLD).superseded_by is None
    assert current(engine, old_id)


def test_a_hand_damaged_correction_cycle_never_makes_a_fact_replace_itself(setup, monkeypatch):
    client, engine = setup
    old_id, middle_id = supersede(engine, monkeypatch)
    with Session(engine) as session:
        middle = session.get(Memory, middle_id)
        middle.superseded_by = old_id
        session.add(middle)
        session.commit()
    delete_facts(client, "single", [middle_id])
    assert fact(engine, OLD).superseded_by is None
    assert current(engine, old_id)


def test_facts_stored_before_edits_were_recorded_are_never_superseded(tmp_path, monkeypatch):
    """Earlier versions changed fact text in place without recording the edit."""
    from sqlalchemy import text

    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE memory (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
                "content TEXT NOT NULL, tree_id INTEGER, source_node_id INTEGER, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO memory VALUES (1, 'fact', '我改过的结论：先校验再执行。', 1, 1, "
                "'2026-01-01')"
            )
        )
    for module in (service, database):
        monkeypatch.setattr(module, "engine", engine)
    database.init_db()
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="旧主题"))
        session.add(Node(id=1, tree_id=1, title="根", status="complete"))
        session.commit()
    extract(
        monkeypatch,
        1,
        "新问题",
        [{"text": "自动结论：执行后再校验。", "type": "misconception", "supersedes": 1}],
    )
    with Session(engine) as session:
        legacy = session.get(Memory, 1)
        assert legacy.superseded_by is None and legacy.origin == "legacy"
        added = session.exec(select(Memory).where(Memory.id != 1)).one()
        assert added.content == "自动结论：执行后再校验。" and added.origin == "extracted"
    engine.dispose()


def test_a_fact_edited_in_settings_is_never_superseded(setup, monkeypatch):
    client, engine = setup
    extract(monkeypatch, 1, "问题一", [OLD])
    old = fact(engine, OLD)
    assert old.origin == "extracted"
    edited = "我的修改：quicksort 在原地实现时不稳定。"
    response = client.patch(
        f"/memories/{old.id}", json={"content": edited, "expected_content": OLD}
    )
    assert response.status_code == 200
    extract(
        monkeypatch,
        1,
        "问题二",
        [{"text": NEW, "type": "misconception", "supersedes": old.id}],
    )
    assert fact(engine, edited).superseded_by is None
