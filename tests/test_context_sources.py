"""Source rereads use a real temporary SQLite database, never user data or APIs."""

import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.context_sources import SOURCE_TOOL_DEF, SOURCE_TOOL_NAME, make_source_reader
from app.models import KnowledgeTree, Message, Node


@pytest.fixture
def source_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sources.sqlite'}")
    SQLModel.metadata.create_all(engine)
    text = "甲🙂e\u0301\n引用中的空白  不得更改。" * 350
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="当前树"), KnowledgeTree(id=2, title="别树")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="根", learning_note="初始笔记", seed_text="初始引用"),
                Node(id=2, tree_id=1, parent_id=1, title="路径中间节点"),
                Node(id=3, tree_id=1, parent_id=2, title="当前节点", status="pending"),
                Node(id=4, tree_id=1, parent_id=1, title="兄弟节点秘密"),
                Node(id=5, tree_id=2, title="别树秘密"),
            ]
        )
        session.add_all(
            [
                Message(id=11, node_id=1, role="user", content="根节点问题"),
                Message(id=12, node_id=1, role="assistant", content="已被重试替代的旧答案"),
                Message(
                    id=13,
                    node_id=1,
                    role="assistant",
                    content="中断的半截旧答案",
                    status="interrupted",
                ),
                Message(
                    id=14,
                    node_id=1,
                    role="assistant",
                    content=text,
                    images=["data:image/png;base64,PRIVATE_IMAGE"],
                ),
                Message(id=21, node_id=2, role="user", content="中间节点问题"),
                Message(id=22, node_id=2, role="assistant", content="中间节点答案"),
                Message(id=31, node_id=3, role="user", content="当前问题"),
                Message(id=41, node_id=4, role="user", content="兄弟问题秘密"),
                Message(id=42, node_id=4, role="assistant", content="兄弟答案秘密"),
                Message(id=51, node_id=5, role="user", content="别树问题秘密"),
                Message(id=52, node_id=5, role="assistant", content="别树答案秘密"),
            ]
        )
        session.commit()
    allowed = {11, 12, 13, 14, 21, 22, 31, 41, 42, 51, 52}
    yield engine, allowed, text
    engine.dispose()


def _read(reader, node_id=1, message_id=14, **kwargs):
    return json.loads(
        reader({"node_id": node_id, "message_id": message_id, "section": "message", **kwargs})
    )


def test_schema_and_exact_unicode_pagination_are_bounded(source_db):
    engine, allowed, original = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    assert SOURCE_TOOL_DEF["function"]["name"] == SOURCE_TOOL_NAME == "read_learning_source"
    assert SOURCE_TOOL_DEF["function"]["parameters"]["required"] == ["node_id"]
    first = _read(reader)
    assert first["ok"] and first["text"] == original[:1200]
    assert first["start"] == 0 and first["end"] == first["next_start"] == 1200
    assert first["total_chars"] == len(original)
    pieces, start = [], 0
    while start is not None:
        result = _read(reader, start=start, max_chars=200)
        assert result["text"] == original[result["start"] : result["end"]]
        pieces.append(result["text"])
        start = result["next_start"]
    assert "".join(pieces) == original
    assert len(_read(reader, max_chars=100_000)["text"]) == 2000
    assert _read(reader, start=len(original) + 20)["text"] == ""


def test_images_are_counted_but_never_exposed_or_claimed_seen(source_db):
    engine, allowed, _ = source_db
    raw = make_source_reader(engine, 3, 1, allowed)(
        {"node_id": 1, "message_id": 14, "section": "message"}
    )
    result = json.loads(raw)
    assert result["image_count"] == 1 and "图片未附送" in result["image_notice"]
    assert "PRIVATE_IMAGE" not in raw and "data:image" not in raw


def test_only_current_path_complete_snapshot_versions_are_readable(source_db):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    for node_id, message_id in [(1, 12), (1, 13), (4, 42), (5, 52), (1, 22), (900, 14)]:
        result = _read(reader, node_id, message_id)
        assert not result["ok"] and result["error"] == "source_unavailable"
    assert _read(reader, 2, 22)["text"] == "中间节点答案"
    assert _read(reader, 3, 31)["text"] == "当前问题"
    with Session(engine) as session:
        session.add(Message(id=101, node_id=2, role="user", content="本轮开始后出现的秘密问题"))
        session.commit()
    assert not _read(reader, 2, 101)["ok"]
    index = json.loads(reader({"node_id": 2}))
    assert 101 not in [item["message_id"] for item in index["messages"]]
    # The caller cannot expand the captured set after the reader has been made.
    allowed.add(101)
    assert not _read(reader, 2, 101)["ok"]


def test_retries_invalidate_previous_complete_answer_and_hide_new_attempt(source_db):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    assert _read(reader)["ok"]
    with Session(engine) as session:
        session.add(Message(id=102, node_id=1, role="assistant", content="新的重试结果"))
        session.commit()
    assert not _read(reader, message_id=14)["ok"]
    assert not _read(reader, message_id=102)["ok"]
    index = json.loads(reader({"node_id": 1}))
    assert [item["message_id"] for item in index["messages"]] == [11]


@pytest.mark.parametrize("status", ["pending", "interrupted", "error"])
def test_current_incomplete_messages_are_not_available(source_db, status):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    with Session(engine) as session:
        message = session.get(Message, 14)
        message.status = status
        session.add(message)
        session.commit()
    assert not _read(reader)["ok"]


def test_fresh_session_returns_current_edited_text_and_notes_without_writes(source_db):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    assert json.loads(reader({"node_id": 1, "section": "note"}))["text"] == "初始笔记"
    with Session(engine) as session:
        node = session.get(Node, 1)
        node.learning_note, node.seed_text = "当前修改后的笔记" * 400, "当前引用\n 保留空白"
        message = session.get(Message, 14)
        message.content = "当前修改后的原文，不可返回缓存旧值。"
        session.add_all([node, message])
        session.commit()
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert _read(reader)["text"] == "当前修改后的原文，不可返回缓存旧值。"
        note = json.loads(reader({"node_id": 1, "section": "note", "start": 3, "max_chars": 7}))
        assert note["text"] == ("当前修改后的笔记" * 400)[3:10]
        quote = json.loads(reader({"node_id": 1, "section": "quote"}))
        assert quote["text"] == "当前引用\n 保留空白"
        assert quote["source_version"] == "current" and "当前原文" in quote["notice"]
        assert statements and not {"INSERT", "UPDATE", "DELETE", "REPLACE"}.intersection(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("change", ["delete_source", "delete_target", "reparent", "move_tree"])
def test_source_membership_is_rechecked_after_each_change(source_db, change):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    assert _read(reader, 2, 22)["ok"]
    with Session(engine) as session:
        if change == "delete_source":
            session.delete(session.get(Node, 2))
        elif change == "delete_target":
            session.delete(session.get(Node, 3))
        else:
            target = session.get(Node, 3)
            if change == "reparent":
                target.parent_id = 4
            else:
                target.tree_id = 2
            session.add(target)
        session.commit()
    result = _read(reader, 2, 22)
    assert not result["ok"] and result["error"] == "source_unavailable"
    assert not json.loads(reader({"node_id": 2}))["ok"]


def test_deleted_message_is_not_available_even_if_its_node_remains(source_db):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)
    with Session(engine) as session:
        session.delete(session.get(Message, 14))
        session.commit()
    assert not _read(reader)["ok"]
    assert 14 not in [item["message_id"] for item in json.loads(reader({"node_id": 1}))["messages"]]


def test_index_paginates_both_directories_and_excludes_other_branches(source_db):
    engine, allowed, _ = source_db
    with Session(engine) as session:
        for offset in range(45):
            message_id = 200 + offset
            session.add(Message(id=message_id, node_id=1, role="user", content=f"历史问题{offset}"))
            allowed.add(message_id)
        parent = 3
        for node_id in range(1000, 1025):
            session.add(Node(id=node_id, tree_id=1, parent_id=parent, title="路径标题" * 100))
            parent = node_id
        session.commit()
    reader = make_source_reader(engine, parent, 1, allowed)
    index = json.loads(reader({"node_id": 1, "limit": 1000}))
    assert len(index["messages"]) == len(index["path"]) == 20
    assert index["next_start"] == index["next_path_start"] == 20
    path_ids = [item["node_id"] for item in index["path"]]
    message_ids = [item["message_id"] for item in index["messages"]]
    next_index = json.loads(reader({"node_id": 1, "start": 20, "path_start": 20, "limit": 20}))
    path_ids.extend(item["node_id"] for item in next_index["path"])
    message_ids.extend(item["message_id"] for item in next_index["messages"])
    assert path_ids == [1, 2, 3, *range(1000, 1025)]
    assert 4 not in path_ids and 5 not in path_ids
    assert len(set(message_ids)) == len(message_ids)
    assert all(len(item["title"]) <= 121 for item in index["path"])
    assert not any(item["message_id"] in (12, 13, 41, 42, 51, 52) for item in index["messages"])
    fallback = json.loads(reader({"node_id": 1, "section": "message"}))
    assert fallback["section"] == "index"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"node_id": True},
        {"node_id": "1"},
        {"node_id": 1, "start": -1},
        {"node_id": 1, "max_chars": 0},
        {"node_id": 1, "message_id": None},
        {"node_id": 1, "section": "secret"},
        {"node_id": 1, "extra": "SELECT secret"},
        {"node_id": 1, "path_start": 1.5},
        {"node_id": 1, "limit": False},
        [],
    ],
)
def test_bad_arguments_return_fixed_errors(source_db, args):
    engine, allowed, _ = source_db
    result = json.loads(make_source_reader(engine, 3, 1, allowed)(args))
    assert not result["ok"] and result["error"] == "invalid_arguments"


def test_database_error_does_not_reveal_sql_or_paths(source_db, monkeypatch):
    engine, allowed, _ = source_db
    reader = make_source_reader(engine, 3, 1, allowed)

    def fail(*args, **kwargs):
        raise RuntimeError("sqlite path=/private/data.db; SELECT secret_key FROM credentials")

    monkeypatch.setattr("app.context_sources.get_ancestors", fail)
    raw = reader({"node_id": 1})
    assert json.loads(raw)["error"] == "source_read_failed"
    assert "private" not in raw and "secret_key" not in raw and "SELECT" not in raw
