"""A topic's default learning style: set by PATCH, listed, synced, kept out of exports."""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
import app.main as main
import app.service as service
from app import learning_presets as presets
from app.models import KnowledgeTree, Message, ModelConfig, Node
from app.routers import nodes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'topic-styles.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)
    nodes._GENERATIONS.clear()

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    with Session(engine) as session:
        session.add(
            ModelConfig(
                label="演示", base_url="mock", llm_model="mock", api_key="mock", is_default=True
            )
        )
        session.commit()
    yield TestClient(main.app), engine
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    main.app.dependency_overrides.clear()
    engine.dispose()


def ask(client, node_id, question, **body):
    response = client.post(f"/nodes/{node_id}/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[-1].get("done"), events
    return events[-1]["node_id"]


def snapshot(engine):
    """Everything a topic style change must leave alone."""
    with Session(engine) as session:
        return [
            row.model_dump()
            for model in (Node, Message)
            for row in session.exec(select(model).order_by(model.id)).all()
        ]


def custom_style(name="analogy"):
    directory = presets.CUSTOM_DIRECTORY
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        f"---\ntitle_zh: 自定义 {name}\ntitle_en: Custom {name}\n---\n合成教学说明。\n",
        encoding="utf-8",
    )
    return path


def test_the_topic_default_is_patched_listed_and_cleared(setup):
    client, engine = setup
    tree = client.post("/trees", json={"title": "学习方式"}).json()
    other = client.post("/trees", json={"title": "另一个主题"}).json()
    assert tree["learning_preset"] is None and other["learning_preset"] is None
    ask(client, tree["root_node_id"], "根问题", learning_preset="feynman")
    before = snapshot(engine)

    chosen = client.patch(f"/trees/{tree['id']}", json={"learning_preset": "socratic"})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json() == {**tree, "learning_preset": "socratic"}
    assert client.get("/trees").json() == [chosen.json(), other]
    assert client.get("/trees?include_archived=true").json() == [chosen.json(), other]
    assert snapshot(engine) == before, "earlier questions keep the style they were asked with"

    both = client.patch(
        f"/trees/{tree['id']}", json={"title": "新名称", "learning_preset": "compare"}
    )
    assert (both.json()["title"], both.json()["learning_preset"]) == ("新名称", "compare")
    archived = client.patch(f"/trees/{tree['id']}", json={"archived": True})
    assert archived.json()["learning_preset"] == "compare", "other fields keep the style"
    cleared = client.patch(f"/trees/{tree['id']}", json={"learning_preset": None})
    assert cleared.status_code == 200 and cleared.json()["learning_preset"] is None
    with Session(engine) as session:
        assert session.get(KnowledgeTree, tree["id"]).learning_preset is None
        assert session.get(KnowledgeTree, other["id"]).learning_preset is None


@pytest.mark.parametrize(
    "payload",
    [
        {"learning_preset": "no-such-style"},
        {"learning_preset": "custom:missing"},
        {"learning_preset": 7},
        {"learning_preset": "x" * 81},
        {"learning_preset": None, "title": None},
        {"learning_preset": "socratic", "unsupported": True},
    ],
)
def test_invalid_topic_style_updates_change_nothing(setup, payload):
    client, engine = setup
    tree = client.post("/trees", json={"title": "保持原样"}).json()
    client.patch(f"/trees/{tree['id']}", json={"learning_preset": "self-test"})
    response = client.patch(f"/trees/{tree['id']}", json=payload)
    assert response.status_code == 422
    if payload.get("learning_preset") in ("no-such-style", "custom:missing") and len(payload) == 1:
        assert response.json()["detail"] == presets.UNKNOWN_PRESET
    assert client.get("/trees").json() == [{**tree, "learning_preset": "self-test"}]
    assert client.patch("/trees/999", json={"learning_preset": "socratic"}).status_code == 404


def test_a_custom_topic_default_is_kept_after_its_file_is_deleted(setup):
    client, _ = setup
    tree = client.post("/trees", json={"title": "自定义"}).json()
    path = custom_style()
    assert (
        client.patch(f"/trees/{tree['id']}", json={"learning_preset": "custom:analogy"}).json()[
            "learning_preset"
        ]
        == "custom:analogy"
    )
    path.unlink()
    assert client.get("/trees").json()[0]["learning_preset"] == "custom:analogy"
    again = client.patch(f"/trees/{tree['id']}", json={"learning_preset": "custom:analogy"})
    assert again.status_code == 422 and again.json()["detail"] == presets.UNKNOWN_PRESET
    # The next question answers in the default way and says so.
    node_id = ask(client, tree["root_node_id"], "删除后的问题")
    assert client.get(f"/nodes/{node_id}").json()["learning_preset"] is None


def test_topic_style_changes_reach_other_windows_through_the_topic_feed(setup):
    client, _ = setup
    tree = client.post("/trees", json={"title": "多窗口"}).json()
    cursor = client.get("/changes").json()["cursor"]
    client.patch(f"/trees/{tree['id']}", json={"learning_preset": "socratic"})
    notice = client.get("/changes", params={"since": cursor}).json()
    assert notice["topics"] is True and notice["reload"] is False and notice["trees"] == {}
    cursor = notice["cursor"]
    client.patch(f"/trees/{tree['id']}", json={"learning_preset": "socratic"})  # Unchanged.
    assert client.get("/changes", params={"since": cursor}).json()["topics"] is False


def test_exports_carry_question_styles_only_when_set_and_imports_restore_them(setup):
    client, engine = setup
    tree = client.post("/trees", json={"title": "导出学习方式"}).json()
    root = tree["root_node_id"]
    plain = client.get(f"/trees/{tree['id']}/export").json()
    ask(client, root, "根问题", learning_preset="worked-examples")
    follow_up = ask(client, root, "追问", learning_preset=None)
    path = custom_style("gone")
    custom = ask(client, follow_up, "自定义追问", learning_preset="custom:gone")
    path.unlink()  # A style deleted since the question stays as history.
    client.patch(f"/trees/{tree['id']}", json={"learning_preset": "socratic"})

    backup = client.get(f"/trees/{tree['id']}/export").json()
    assert backup["tree"] == {"title": "导出学习方式"}, "the topic default is not exported"
    exported = {node["id"]: node for node in backup["nodes"]}
    assert exported[root]["learning_preset"] == "worked-examples"
    assert "learning_preset" not in exported[follow_up]
    assert exported[custom]["learning_preset"] == "custom:gone"

    imported = client.post("/trees/import", json=backup)
    assert imported.status_code == 200, imported.text
    assert imported.json()["learning_preset"] is None
    restored = sorted(
        (node["title"], node["learning_preset"])
        for node in client.get(f"/trees/{imported.json()['id']}").json()
    )
    assert restored == [
        ("根问题", "worked-examples"),
        ("自定义追问", "custom:gone"),
        ("追问", None),
    ]

    # A backup from before styles existed (no field anywhere) imports unchanged.
    assert all("learning_preset" not in node for node in plain["nodes"])
    legacy = client.post("/trees/import", json=plain)
    assert legacy.status_code == 200
    assert {n["learning_preset"] for n in client.get(f"/trees/{legacy.json()['id']}").json()} == {
        None
    }

    with Session(engine) as session:
        topics = len(session.exec(select(KnowledgeTree)).all())
    for malformed in ("Not An Id", "custom:", "x" * 81):
        broken = json.loads(json.dumps(backup))
        broken["nodes"][0]["learning_preset"] = malformed
        assert client.post("/trees/import", json=broken).status_code == 422
    with Session(engine) as session:
        assert len(session.exec(select(KnowledgeTree)).all()) == topics


def test_a_malformed_stored_style_is_left_out_of_exports(setup):
    client, engine = setup
    tree = client.post("/trees", json={"title": "损坏的值"}).json()
    ask(client, tree["root_node_id"], "根问题", learning_preset="socratic")
    with Session(engine) as session:
        node = session.get(Node, tree["root_node_id"])
        node.learning_preset = "Not A Stored Id"
        session.add(node)
        session.commit()
    backup = client.get(f"/trees/{tree['id']}/export").json()
    assert all("learning_preset" not in node for node in backup["nodes"])
    assert client.post("/trees/import", json=backup).status_code == 200
