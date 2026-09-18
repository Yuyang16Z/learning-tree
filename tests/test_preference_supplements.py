"""Separate AI preferences preserve ownership, source scope, and stale-editor protection."""

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
from app.models import (
    KnowledgeTree,
    Memory,
    Node,
    PreferenceExtraction,
    PreferenceLearningState,
    PreferenceProfile,
    PreferenceSupplement,
)
from app.preference_supplements import eligible_supplements
from app.routers.memory_router import router


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'preferences.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router)

    def get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[database.get_session] = get_session
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="数学"), KnowledgeTree(id=2, title="英语")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="数学来源", status="complete"),
                Node(id=2, tree_id=2, title="英语来源", status="complete"),
                Node(id=3, tree_id=1, title="失败来源", status="error"),
            ]
        )
        session.commit()
    yield TestClient(app), engine
    engine.dispose()


def add(engine, **overrides):
    values = {
        "content": "以后先举例再解释。",
        "evidence": "以后请先举例再解释",
        "source_node_id": 1,
        "source_tree_id": 1,
    }
    values.update(overrides)
    with Session(engine) as session:
        row = PreferenceSupplement(**values)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def test_additive_tables_preserve_legacy_rows_and_profile(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(engine, tables=[Memory.__table__, PreferenceProfile.__table__])
    with Session(engine) as session:
        session.add(Memory(id=42, kind="preference", content="旧偏好"))
        session.add(PreferenceProfile(content="手动偏好", revision="manual-revision"))
        session.commit()
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    assert {"preferencesupplement", "preferencelearningstate", "preferenceextraction"} <= set(
        inspect(engine).get_table_names()
    )
    with Session(engine) as session:
        assert session.get(Memory, 42).content == "旧偏好"
        assert session.get(PreferenceProfile, 1).revision == "manual-revision"
        assert not session.exec(select(PreferenceSupplement)).all()
    engine.dispose()


def test_learning_setting_defaults_without_writes_and_requires_revision(setup):
    client, engine = setup
    initial = client.get("/memories/preferences/learning").json()
    assert initial == {"enabled": True, "revision": "initial"}
    assert client.get("/memories/preferences/learning").json() == initial
    with Session(engine) as session:
        assert session.get(PreferenceLearningState, 1) is None
    updated = client.put("/memories/preferences/learning", json={**initial, "enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["revision"] != initial["revision"]
    assert client.put("/memories/preferences/learning", json=initial).status_code == 409
    assert client.put("/memories/preferences/learning", json={"enabled": True}).status_code == 422


def test_concurrent_first_setting_saves_have_one_winner(setup):
    client, engine = setup
    initial = client.get("/memories/preferences/learning").json()
    barrier = Barrier(2)

    def update(enabled):
        barrier.wait(timeout=5)
        return client.put(
            "/memories/preferences/learning", json={**initial, "enabled": enabled}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, [True, False])) == [200, 409]
    with Session(engine) as session:
        assert len(session.exec(select(PreferenceLearningState)).all()) == 1


def test_supplement_listing_includes_scope_evidence_source_and_pagination(setup):
    client, engine = setup
    first = add(engine)
    second = add(engine, scope="topic", tree_id=1, status="pending")
    first_page = client.get("/memories/preferences/supplements?page_size=1").json()
    assert first_page["total"] == 2 and first_page["page"] == 1
    assert len(first_page["items"]) == 1
    row = first_page["items"][0]
    assert row["id"] == second.id
    assert row["scope"] == "topic" and row["status"] == "pending"
    assert row["evidence"] == second.evidence
    assert row["tree_title"] == row["source_tree_title"] == "数学"
    assert row["source_node_id"] == 1 and row["source_tree_id"] == 1
    second_page = client.get("/memories/preferences/supplements?page=2&page_size=1").json()
    assert second_page["items"][0]["id"] == first.id
    assert second_page["items"][0]["tree_title"] is None
    assert client.get("/memories/preferences/supplements?page=0").status_code == 422
    assert client.get("/memories/preferences/supplements?page_size=101").status_code == 422


@pytest.mark.parametrize("status", ["active", "pending"])
def test_edit_locks_content_without_implicitly_activating_suggestions(setup, status):
    client, engine = setup
    row = add(engine, status=status)
    body = {"content": "  我手动修改的偏好  ", "revision": row.revision}
    url = f"/memories/preferences/supplements/{row.id}"
    changed = client.patch(url, json=body)
    assert changed.status_code == 200
    item = changed.json()
    assert item["content"] == "我手动修改的偏好"
    assert item["status"] == status
    assert item["user_edited"] is True
    assert item["revision"] != row.revision
    assert item["evidence"] == row.evidence
    assert client.patch(url, json=body).status_code == 409
    assert client.delete(url, params={"revision": row.revision}).status_code == 409
    assert (
        client.patch(url, json={"content": "  ", "revision": item["revision"]}).status_code == 422
    )
    assert client.delete(url).status_code == 422
    assert client.delete(url, params={"revision": item["revision"]}).json() == {"deleted": row.id}
    assert client.patch(url, json=body).status_code == 404
    assert client.delete(url, params={"revision": item["revision"]}).status_code == 404


def test_supplements_and_profile_remain_independently_editable(setup):
    client, engine = setup
    with Session(engine) as session:
        session.add(Memory(kind="preference", content="旧自动偏好"))
        session.commit()
    profile = client.get("/memories/preferences").json()
    assert profile["content"] == "旧自动偏好"
    row = add(engine)
    assert client.get("/memories/preferences").json() == profile
    saved = client.put(
        "/memories/preferences", json={"content": "手动优先偏好", "revision": profile["revision"]}
    ).json()
    assert saved["managed"]
    assert client.get("/memories/preferences/supplements").json()["items"][0]["id"] == row.id
    client.patch(
        f"/memories/preferences/supplements/{row.id}",
        json={"content": "修改补充", "revision": row.revision},
    )
    assert client.get("/memories/preferences").json() == saved


def test_delete_and_clear_retain_content_free_replay_ledger(setup):
    client, engine = setup
    row = add(engine)
    with Session(engine) as session:
        session.add(PreferenceExtraction(key="a" * 64, node_id=1, tree_id=1))
        session.add(Memory(kind="fact", content="删除的话题事实", tree_id=1, source_node_id=1))
        session.commit()
    assert (
        client.delete(
            f"/memories/preferences/supplements/{row.id}", params={"revision": row.revision}
        ).status_code
        == 200
    )
    add(engine)
    setting = client.put(
        "/memories/preferences/learning", json={"enabled": False, "revision": "initial"}
    ).json()
    profile = client.get("/memories/preferences").json()
    assert client.delete("/memories").json() == {"cleared": 2}
    assert client.get("/memories/preferences/supplements").json()["total"] == 0
    assert client.get("/memories").json() == []
    assert client.get("/memories/preferences").json()["content"] == ""
    after = client.get("/memories/preferences/learning").json()
    assert after["enabled"] is False
    assert after["revision"] != setting["revision"]
    assert client.put("/memories/preferences/learning", json=setting).status_code == 409
    assert (
        client.put(
            "/memories/preferences", json={"content": "过时草稿", "revision": profile["revision"]}
        ).status_code
        == 409
    )
    with Session(engine) as session:
        assert session.get(PreferenceExtraction, "a" * 64)
        assert session.get(PreferenceProfile, 1).reset_revision


def test_eligible_supplements_filter_status_scope_and_source_validity(setup):
    _, engine = setup
    global_row = add(engine)
    topic_row = add(engine, scope="topic", tree_id=1)
    other_topic = add(engine, scope="topic", tree_id=2, source_node_id=2, source_tree_id=2)
    add(engine, status="pending")
    add(engine, source_node_id=3)
    add(engine, source_node_id=999)
    add(engine, source_node_id=None, source_tree_id=None, user_edited=True)
    add(engine, source_tree_id=2)
    add(engine, scope="topic", tree_id=2)
    add(engine, scope="topic", tree_id=None)
    add(engine, tree_id=1)
    with Session(engine) as session:
        assert {row.id for row in eligible_supplements(session, 1)} == {global_row.id, topic_row.id}
        assert {row.id for row in eligible_supplements(session, 2)} == {
            global_row.id,
            other_topic.id,
        }
        assert {row.id for row in eligible_supplements(session, None)} == {global_row.id}
        session.delete(session.get(KnowledgeTree, 1))
        session.commit()
        assert not eligible_supplements(session, 1)


def test_disabled_learning_keeps_previously_accepted_preferences_available(setup):
    client, engine = setup
    row = add(engine)
    client.put("/memories/preferences/learning", json={"enabled": False, "revision": "initial"})
    with Session(engine) as session:
        assert [item.id for item in eligible_supplements(session, 1)] == [row.id]


def test_existing_session_refreshes_changed_supplement_and_source(setup):
    client, engine = setup
    row = add(engine)
    with Session(engine) as session:
        assert eligible_supplements(session, 1)[0].content == row.content
        client.patch(
            f"/memories/preferences/supplements/{row.id}",
            json={"content": "最新手动编辑", "revision": row.revision},
        )
        assert eligible_supplements(session, 1)[0].content == "最新手动编辑"
        with Session(engine) as source_session:
            source = source_session.get(Node, 1)
            source.status = "pending"
            source_session.add(source)
            source_session.commit()
        assert not eligible_supplements(session, 1)
