"""The return card is a local extract of the branch being left: no model call, no writes."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

import app.db as database
import app.llm as llm
import app.main as main
import app.service as service
from app.models import KnowledgeTree, Message, Node
from app.return_summary import MAX_EXCERPT_CHARS, plain_sentence
from app.routers import nodes

ROOT_ANSWER = "递归把问题拆成更小的同类问题。实际上，根节点的这句纠正不属于分支。"
BRANCH_ANSWER = (
    "基例让递归停下来，这是最常见的说法。\n\n"
    "实际上，基例必须写在递归调用之前检查。\n\n"
    "基例是指函数直接给出答案的输入。\n\n"
    "只有当参数逐步靠近基例时，递归才会结束。"
)
FOLLOW_UP_ANSWER = (
    "## 小结\n\n"
    "需要注意以下几点：\n\n"
    "- **注意**：只有基例正确，尾递归改写才安全。\n\n"
    "> 尾递归是指递归调用是函数的最后一步。\n\n"
    "| 写法 | 是否尾递归 |\n\n"
    "我也不确定你的语言是否优化尾递归。"
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'return.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)

    def session_override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[database.get_session] = session_override
    yield TestClient(main.app), engine
    main.app.dependency_overrides.clear()
    engine.dispose()


def seed(engine):
    """root → branch (quotes the root answer) → follow-up, plus an unrelated sibling branch."""
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="递归"))
        session.add(Node(id=1, tree_id=1, title="什么是递归", kind="root", status="complete"))
        session.add(
            Node(
                id=2,
                tree_id=1,
                parent_id=1,
                title="基例是什么",
                kind="branch",
                seed_text="更小的同类问题",
                source_node_id=1,
                source_message_id=2,
                source_start=4,
                source_end=11,
                learning_note="  基例让递归停下。 ",
                understanding="open",
            )
        )
        session.add(Node(id=3, tree_id=1, parent_id=2, title="尾递归呢", status="complete"))
        session.add(
            Node(id=4, tree_id=1, parent_id=1, title="旁支", kind="branch", seed_text="同类")
        )
        rows = [
            (1, 1, "user", "什么是递归？", "complete"),
            (2, 1, "assistant", ROOT_ANSWER, "complete"),
            (3, 2, "user", "基例是什么？我还不明白它为什么能让递归停下。", "complete"),
            (4, 2, "assistant", "实际上，失败的回答不该出现。", "error"),
            (5, 2, "assistant", BRANCH_ANSWER, "complete"),
            (6, 3, "user", "那尾递归呢？", "complete"),
            (7, 3, "assistant", FOLLOW_UP_ANSWER, "complete"),
            (8, 4, "user", "旁支问题", "complete"),
            (9, 4, "assistant", "实际上，旁支的纠正也不属于这个分支。", "complete"),
        ]
        for message_id, node_id, role, content, status in rows:
            session.add(
                Message(id=message_id, node_id=node_id, role=role, content=content, status=status)
            )
        session.commit()


def test_excerpts_rank_categories_and_carry_note_mark_and_source(setup):
    client, engine = setup
    seed(engine)
    response = client.get("/nodes/3/return-summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {key: body[key] for key in ("branch_node_id", "branch_title", "note")} == {
        "branch_node_id": 2,
        "branch_title": "基例是什么",
        "note": "基例让递归停下。",
    }
    assert body["understanding"] == "open", "the follow-up is unmarked, so the branch mark counts"
    assert body["source"] == {
        "node_id": 1,
        "message_id": 2,
        "start": 4,
        "end": 11,
        "text": "更小的同类问题",
    }
    assert [(item["category"], item["text"]) for item in body["excerpts"]] == [
        ("correction", "实际上，基例必须写在递归调用之前检查。"),
        ("condition", "注意：只有基例正确，尾递归改写才安全。"),
        ("condition", "只有当参数逐步靠近基例时，递归才会结束。"),
        ("definition", "尾递归是指递归调用是函数的最后一步。"),
    ]
    assert [(item["node_id"], item["message_id"]) for item in body["excerpts"]] == [
        (2, 5),
        (3, 7),
        (2, 5),
        (3, 7),
    ]


def test_only_the_branch_path_and_open_questions_from_the_learner_count(setup):
    client, engine = setup
    seed(engine)
    body = client.get("/nodes/2/return-summary").json()
    texts = [item["text"] for item in body["excerpts"]]
    joined = "\n".join(texts)
    assert "根节点" not in joined and "旁支" not in joined, "ancestors and siblings stay out"
    assert "失败的回答" not in joined, "incomplete answers are not sources"
    assert "那尾递归呢" not in joined and "尾递归" not in joined, (
        "deeper nodes are not on this path"
    )
    assert [item["category"] for item in body["excerpts"]] == [
        "correction",
        "condition",
        "definition",
        "question",
    ]
    # The learner's own sentence appears only as an open question, never their plain question.
    assert texts[-1] == "我还不明白它为什么能让递归停下。"
    assert body["excerpts"][-1]["message_id"] == 3 and "基例是什么？" not in texts
    assert all(item["node_id"] == 2 for item in body["excerpts"])


def test_headings_tables_and_lead_ins_are_skipped_and_long_sentences_shortened(setup):
    client, engine = setup
    seed(engine)
    long_sentence = "实际上" + "很长的纠正" * 60 + "。"
    with Session(engine) as session:
        message = session.get(Message, 7)
        message.content = FOLLOW_UP_ANSWER + "\n\n" + long_sentence
        session.add(message)
        session.commit()
    excerpts = client.get("/nodes/3/return-summary").json()["excerpts"]
    texts = [item["text"] for item in excerpts]
    assert texts[0].startswith("实际上很长的纠正") and texts[0].endswith("…")
    assert len(texts[0]) == MAX_EXCERPT_CHARS
    assert not any(text.startswith(("#", "|", "小结", "需要注意")) for text in texts)
    assert not any("**" in text or text.startswith(("-", ">")) for text in texts)


def test_mark_comes_from_the_node_being_left_first(setup):
    client, engine = setup
    seed(engine)
    client.patch("/nodes/3", json={"understanding": "clear"})
    assert client.get("/nodes/3/return-summary").json()["understanding"] == "clear"
    assert client.get("/nodes/2/return-summary").json()["understanding"] == "open"
    client.patch("/nodes/2", json={"understanding": None})
    client.patch("/nodes/3", json={"understanding": None})
    assert client.get("/nodes/3/return-summary").json()["understanding"] is None


def test_nearest_branch_wins_and_empty_branches_have_no_excerpts(setup):
    client, engine = setup
    seed(engine)
    with Session(engine) as session:
        session.add(
            Node(
                id=5,
                tree_id=1,
                parent_id=3,
                title="",
                kind="branch",
                seed_text="尾递归",
                source_node_id=3,
                source_message_id=7,
            )
        )
        session.commit()
    body = client.get("/nodes/5/return-summary").json()
    assert body["branch_node_id"] == 5 and body["branch_title"] == ""
    assert body["excerpts"] == [] and body["note"] is None and body["understanding"] is None
    assert body["source"]["node_id"] == 3 and body["source"]["message_id"] == 7


def test_nodes_outside_a_branch_or_missing_are_not_found(setup):
    client, engine = setup
    seed(engine)
    with Session(engine) as session:
        session.add(Node(id=6, tree_id=1, parent_id=1, title="主线追问", status="complete"))
        # An orphaned quote with nowhere to return to is not a branch either.
        session.add(Node(id=7, tree_id=1, title="孤立引用", seed_text="引用"))
        session.commit()
    for node_id in (1, 6, 7):
        response = client.get(f"/nodes/{node_id}/return-summary")
        assert response.status_code == 404
        assert response.json()["detail"] == "这个节点不在分支中"
    missing = client.get("/nodes/999/return-summary")
    assert missing.status_code == 404 and missing.json()["detail"] == "节点不存在"
    assert client.get("/api/nodes/3/return-summary").status_code == 200


def test_summary_calls_no_model_and_writes_nothing(setup, monkeypatch):
    client, engine = setup
    seed(engine)

    def forbidden(*args, **kwargs):
        pytest.fail("the return summary must stay local")

    for module, name in (
        (nodes, "stream_chat"),
        (nodes, "run_agent"),
        (nodes, "complete"),
        (nodes, "summarize_history"),
        (llm, "stream_chat"),
        (llm, "complete"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.lstrip().split()[0].upper())

    cursor = client.get("/changes").json()["cursor"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        for node_id in (2, 3):
            assert client.get(f"/nodes/{node_id}/return-summary").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) <= {"SELECT", "PRAGMA"}, statements
    notice = client.get("/changes", params={"since": cursor}).json()
    assert notice["trees"] == {} and not notice["reload"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("> - **基例**是指 [直接返回](https://example.com/a) 的输入", "基例是指 直接返回 的输入"),
        ("1. `n == 0` 时返回 1", "n == 0 时返回 1"),
        ("### Base case  means   stop", "Base case means stop"),
        ("__Only__ when n shrinks", "Only when n shrinks"),
        ("plain sentence", "plain sentence"),
    ],
)
def test_plain_sentence_drops_markdown_markers(raw, expected):
    assert plain_sentence(raw) == expected
