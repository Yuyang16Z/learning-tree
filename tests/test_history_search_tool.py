"""The model's history search is read-only, generation-scoped and bound to the user's settings."""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import search_index
from app.models import KnowledgeTree, Memory, Message, Node, RetrievalSettings
from app.search_index import HISTORY_TOOL_DEF, HISTORY_TOOL_NAME, make_history_search

OFF = {"include_siblings": False, "include_other_trees": False}
WIDE = {"include_siblings": True, "include_other_trees": True}


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'history.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                KnowledgeTree(id=1, title="当前树"),
                KnowledgeTree(id=2, title="其他树"),
                KnowledgeTree(id=3, title="归档树", archived=True),
            ]
        )
        session.add_all(
            [
                Node(id=1, tree_id=1, title="根", status="complete"),
                Node(
                    id=2,
                    tree_id=1,
                    parent_id=1,
                    title="当前路径节点",
                    status="complete",
                    learning_note="路径笔记：quicksort 用 pivot 分区",
                ),
                Node(id=3, tree_id=1, parent_id=1, title="兄弟分支", status="complete"),
                Node(id=4, tree_id=2, title="其他树的根", status="complete"),
                Node(id=5, tree_id=3, title="归档树的根", status="complete"),
            ]
        )
        session.add_all(
            [
                Message(
                    id=12, node_id=1, role="assistant", content="SYNTHETIC_PATH quicksort 说明"
                ),
                Message(id=31, node_id=3, role="assistant", content="SYNTHETIC_SIBLING quicksort"),
                Message(id=41, node_id=4, role="assistant", content="SYNTHETIC_FOREIGN quicksort"),
                Message(id=51, node_id=5, role="assistant", content="SYNTHETIC_ARCHIVED quicksort"),
                Message(
                    id=13,
                    node_id=1,
                    role="assistant",
                    content="SYNTHETIC_PENDING quicksort",
                    status="pending",
                ),
            ]
        )
        session.add_all(
            [
                Memory(
                    id=101, kind="fact", content="quicksort 是分治算法", tree_id=1, source_node_id=1
                ),
                Memory(
                    id=102,
                    kind="fact",
                    content="sibling fact quicksort",
                    tree_id=1,
                    source_node_id=3,
                ),
                Memory(
                    id=103,
                    kind="fact",
                    content="foreign fact quicksort",
                    tree_id=2,
                    source_node_id=4,
                ),
            ]
        )
        session.commit()
    yield engine
    engine.dispose()


def call(search, **args):
    return json.loads(search(args))


def found(result):
    return {(item["kind"], item["node_id"], item["message_id"]) for item in result["results"]}


def test_schema_is_bounded_and_strict():
    function = HISTORY_TOOL_DEF["function"]
    assert function["name"] == HISTORY_TOOL_NAME == "search_learning_history"
    parameters = function["parameters"]
    assert parameters["required"] == ["query"] and parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"query", "scope", "limit"}
    assert parameters["properties"]["scope"]["enum"] == ["path", "tree", "all"]
    assert parameters["properties"]["limit"]["maximum"] == 8
    assert parameters["properties"]["query"]["maxLength"] == 300


def test_path_scope_reads_only_the_current_path(engine):
    search = make_history_search(engine, 1, [1, 2], OFF)
    raw = search({"query": "quicksort"})
    result = json.loads(raw)
    assert result["ok"] and result["scope"] == "path" and "学习材料" in result["notice"]
    assert found(result) == {("message", 1, 12), ("note", 2, None), ("memory", 1, None)}
    for item in result["results"]:
        assert set(item) == {
            "kind",
            "node_id",
            "node_title",
            "message_id",
            "snippet",
            "tree_id",
            "tree_title",
        }
        assert item["tree_id"] == 1 and item["tree_title"] == "当前树"
        assert "quicksort" in item["snippet"]
    assert "SIBLING" not in raw and "FOREIGN" not in raw and "ARCHIVED" not in raw
    assert "PENDING" not in raw
    assert len(call(search, query="quicksort", limit=1)["results"]) == 1


def test_wider_scopes_require_the_snapshot_taken_at_generation_start(engine):
    search = make_history_search(engine, 1, [1, 2], OFF)
    for scope in ("tree", "all"):
        result = call(search, query="quicksort", scope=scope)
        assert not result["ok"] and result["error"] == "scope_not_enabled"
    with Session(engine) as session:
        session.add(RetrievalSettings(include_siblings=True, include_other_trees=True))
        session.commit()
    # Settings changed after the generation started do not widen this generation.
    assert call(search, query="quicksort", scope="tree")["error"] == "scope_not_enabled"

    siblings = make_history_search(engine, 1, [1, 2], {"include_siblings": True})
    tree = call(siblings, query="quicksort", scope="tree")
    assert tree["ok"] and found(tree) >= {("message", 3, 31), ("memory", 3, None)}
    assert all(item["tree_id"] == 1 for item in tree["results"])
    assert "FOREIGN" not in json.dumps(tree, ensure_ascii=False)
    assert call(siblings, query="quicksort", scope="all")["error"] == "scope_not_enabled"

    everything = make_history_search(engine, 1, [1, 2], WIDE)
    result = call(everything, query="quicksort", scope="all", limit=8)
    assert found(result) >= {("message", 4, 41), ("memory", 4, None), ("message", 3, 31)}
    foreign = next(item for item in result["results"] if item["node_id"] == 4)
    assert foreign["tree_title"] == "其他树" and foreign["node_title"] == "其他树的根"
    assert "ARCHIVED" not in json.dumps(result, ensure_ascii=False)


def test_results_are_rechecked_against_current_records(engine):
    search = make_history_search(engine, 1, [1, 2], OFF)
    assert found(call(search, query="quicksort")) == {
        ("message", 1, 12),
        ("note", 2, None),
        ("memory", 1, None),
    }
    with Session(engine) as session:
        session.delete(session.get(Message, 12))
        node = session.get(Node, 2)
        node.learning_note = None
        fact = session.get(Memory, 101)
        fact.superseded_by = 102
        session.add_all([node, fact])
        session.commit()
    assert call(search, query="quicksort")["results"] == []


def test_search_never_writes(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().split()[0].upper())

    search = make_history_search(engine, 1, [1, 2], WIDE)
    event.listen(engine, "before_cursor_execute", record)
    try:
        for scope in ("path", "tree", "all"):
            assert call(search, query="quicksort", scope=scope)["ok"]
        assert call(search, query="快", scope="path")["ok"]
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements and not {"INSERT", "UPDATE", "DELETE", "REPLACE"}.intersection(statements)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 301},
        {"query": 5},
        {"query": "a", "scope": "secret"},
        {"query": "a", "limit": 0},
        {"query": "a", "limit": 9},
        {"query": "a", "limit": True},
        {"query": "a", "limit": "3"},
        {"query": "a", "extra": "SELECT secret"},
        [],
        None,
    ],
)
def test_bad_arguments_return_fixed_errors(engine, args):
    result = json.loads(make_history_search(engine, 1, [1, 2], WIDE)(args))
    assert not result["ok"] and result["error"] == "invalid_arguments"


def test_database_errors_do_not_reveal_sql_or_paths(engine, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("sqlite path=/private/data.db; SELECT secret_key FROM credentials")

    monkeypatch.setattr(search_index, "search_text", fail)
    raw = make_history_search(engine, 1, [1, 2], OFF)({"query": "quicksort"})
    assert json.loads(raw)["error"] == "search_failed"
    assert "private" not in raw and "secret_key" not in raw and "SELECT" not in raw
