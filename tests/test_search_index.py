"""The full-text index is derived data over synthetic records; triggers keep it current."""

import os
import re
import sqlite3

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
from app import search_index
from app.models import KnowledgeTree, Memory, Message, Node
from app.routers import nodes, trees
from app.search_index import ensure_search_index, search_text


def _has_fts5() -> bool:
    return sqlite3.connect(":memory:").execute("PRAGMA module_list").fetchall() is not None and any(
        row[0] == "fts5" for row in sqlite3.connect(":memory:").execute("PRAGMA module_list")
    )


requires_fts5 = pytest.mark.skipif(not _has_fts5(), reason="this SQLite has no FTS5")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'search.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    for module in (database, nodes):
        monkeypatch.setattr(module, "engine", engine)
    with Session(engine) as session:
        session.add_all(
            [
                KnowledgeTree(id=1, title="当前树"),
                KnowledgeTree(id=2, title="其他树"),
                KnowledgeTree(id=3, title="已归档", archived=True),
            ]
        )
        session.add_all(
            [
                Node(id=1, tree_id=1, title="根", status="complete"),
                Node(
                    id=2,
                    tree_id=1,
                    parent_id=1,
                    title="当前路径",
                    status="complete",
                    learning_note="我的理解：quicksort 用 pivot 分区",
                ),
                Node(id=3, tree_id=1, parent_id=1, title="兄弟分支", status="complete"),
                Node(id=4, tree_id=2, title="其他树的根", status="complete"),
                Node(id=5, tree_id=3, title="归档树的根", status="complete"),
            ]
        )
        session.add_all(
            [
                Message(id=11, node_id=1, role="user", content="快速排序的平均时间复杂度是多少？"),
                Message(
                    id=12,
                    node_id=1,
                    role="assistant",
                    content="快速排序的平均时间复杂度是 O(n log n)，最坏情况是 O(n^2)。",
                ),
                Message(
                    id=21, node_id=2, role="assistant", content="SYNTHETIC_PATH quicksort pivots"
                ),
                Message(id=31, node_id=3, role="assistant", content="SYNTHETIC_SIBLING quicksort"),
                Message(id=41, node_id=4, role="assistant", content="SYNTHETIC_FOREIGN quicksort"),
                Message(id=51, node_id=5, role="assistant", content="SYNTHETIC_ARCHIVED quicksort"),
            ]
        )
        session.add_all(
            [
                Memory(
                    id=101, kind="fact", content="快速排序是分治算法。", tree_id=1, source_node_id=1
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
                Memory(id=104, kind="preference", content="preference quicksort"),
            ]
        )
        session.commit()
    yield engine
    engine.dispose()


def rows(engine, kind=None):
    with engine.connect() as connection:
        statement = "SELECT kind, ref_id, tree_id, node_id FROM search_index"
        if kind:
            statement += f" WHERE kind = '{kind}'"
        return sorted(tuple(row) for row in connection.exec_driver_sql(statement))


@requires_fts5
def test_create_all_builds_the_index_and_triggers_keep_it_current(engine):
    # The user's question is its own kind: search results say what matched.
    assert rows(engine, "question") == [("question", 11, 1, 1)]
    assert rows(engine, "message") == [
        ("message", 12, 1, 1),
        ("message", 21, 1, 2),
        ("message", 31, 1, 3),
        ("message", 41, 2, 4),
        ("message", 51, 3, 5),
    ]
    assert rows(engine, "memory") == [
        ("memory", 101, 1, 1),
        ("memory", 102, 1, 3),
        ("memory", 103, 2, 4),
    ]
    assert rows(engine, "note") == [("note", 2, 1, 2)]
    with Session(engine) as session:
        # A streaming answer is checkpointed as pending and only indexed once complete.
        pending = Message(id=60, node_id=2, role="assistant", content="partial", status="pending")
        session.add(pending)
        session.commit()
        assert ("message", 60, 1, 2) not in rows(engine)
        pending.content, pending.status = "SYNTHETIC_FINAL answer", "complete"
        session.add(pending)
        session.commit()
        assert ("message", 60, 1, 2) in rows(engine)
        assert search_text(session, "SYNTHETIC_FINAL", tree_id=1)[0].ref_id == 60
        pending.content = "SYNTHETIC_EDITED answer"
        session.add(pending)
        session.commit()
        assert not search_text(session, "SYNTHETIC_FINAL", tree_id=1)
        assert search_text(session, "SYNTHETIC_EDITED", tree_id=1)[0].ref_id == 60
        session.delete(pending)
        session.commit()
        assert ("message", 60, 1, 2) not in rows(engine)
        # Notes follow edits; facts leave the index when superseded and return when restored.
        node = session.get(Node, 2)
        node.learning_note = ""
        session.add(node)
        session.commit()
        assert rows(engine, "note") == []
        node.learning_note = "SYNTHETIC_NOTE again"
        session.add(node)
        session.commit()
        assert rows(engine, "note") == [("note", 2, 1, 2)]
        fact = session.get(Memory, 101)
        fact.superseded_by = 102
        session.add(fact)
        session.commit()
        assert ("memory", 101, 1, 1) not in rows(engine)
        fact.superseded_by = None
        session.add(fact)
        session.commit()
        assert ("memory", 101, 1, 1) in rows(engine)
        session.add(Memory(kind="preference", content="never indexed"))
        session.commit()
        assert len(rows(engine, "memory")) == 3


@requires_fts5
def test_init_db_rebuilds_a_missing_or_stale_index_and_is_idempotent(engine):
    with engine.begin() as connection:
        for name in (
            "search_message_ai",
            "search_message_au",
            "search_message_ad",
            "search_memory_ai",
            "search_memory_au",
            "search_memory_ad",
            "search_note_ai",
            "search_note_au",
            "search_node_messages_ai",
            "search_node_tree_au",
            "search_node_ad",
        ):
            connection.exec_driver_sql(f"DROP TRIGGER {name}")
        connection.exec_driver_sql("DROP TABLE search_index")
    with Session(engine) as session:
        session.add(Message(id=70, node_id=2, role="assistant", content="SYNTHETIC_LEGACY answer"))
        session.commit()
    database.init_db()
    database.init_db()
    with engine.connect() as connection:
        assert search_index.index_count(connection) == search_index.source_count(connection) == 11
    with Session(engine) as session:
        assert search_text(session, "SYNTHETIC_LEGACY", tree_id=1)[0].ref_id == 70
    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM search_index WHERE ref_id = 70")
    assert ensure_search_index(engine)
    assert ("message", 70, 1, 2) in rows(engine)


def _trigger_statements(connection) -> dict[str, list[str]]:
    """The statements inside each stored search trigger body."""
    stored = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'search%'"
    )
    return {
        name: [
            part.strip()
            for part in sql[sql.index(" BEGIN ") + len(" BEGIN ") : sql.rindex(" END")].split(";")
            if part.strip()
        ]
        for name, sql in stored
    }


@requires_fts5
def test_trigger_statements_find_index_rows_by_rowid(engine):
    # FTS5 can only look rows up by rowid (or MATCH): a filter on an UNINDEXED column
    # scans the whole index for every changed source row.
    with engine.connect() as connection:
        triggers = _trigger_statements(connection)
        assert len(triggers) == 11
        for name, statements in triggers.items():
            if name == "search_node_tree_au":
                continue  # Nodes never move between topics; this repair may scan.
            for statement in statements:
                probe = re.sub(r"\b(?:OLD|NEW)\.\w+", "?", statement)
                plan = connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + probe, (None,) * probe.count("?")
                ).fetchall()
                for row in plan:
                    if "search_index VIRTUAL TABLE" in row[-1] or " s VIRTUAL TABLE" in row[-1]:
                        assert re.search(r"INDEX \d+:=", row[-1]), (name, statement, row[-1])


@requires_fts5
def test_index_rows_carry_rowids_derived_from_their_sources(engine):
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT rowid, kind, ref_id FROM search_index").all()
    offsets = search_index.ROWID_OFFSETS
    assert offsets == {"message": 1, "question": 1, "memory": 2, "note": 3}
    assert rows and all(rowid == ref_id * 4 + offsets[kind] for rowid, kind, ref_id in rows)


@requires_fts5
def test_an_index_from_an_earlier_trigger_layout_is_replaced_and_rebuilt(engine):
    with engine.begin() as connection:
        names = list(_trigger_statements(connection))
        for name in names:
            connection.exec_driver_sql(f"DROP TRIGGER {name}")
        # The earlier layout: the same rows with arbitrary rowids, found by kind and ref_id.
        connection.exec_driver_sql("DELETE FROM search_index")
        connection.exec_driver_sql(
            "INSERT INTO search_index(kind, ref_id, tree_id, node_id, content) "
            "SELECT 'message', m.id, n.tree_id, m.node_id, m.content "
            "FROM message m JOIN node n ON n.id = m.node_id WHERE m.status = 'complete' "
            "UNION ALL SELECT 'memory', id, tree_id, source_node_id, content FROM memory "
            "WHERE kind = 'fact' AND superseded_by IS NULL "
            "UNION ALL SELECT 'note', id, tree_id, id, learning_note FROM node "
            "WHERE learning_note IS NOT NULL AND learning_note <> ''"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER search_message_ad AFTER DELETE ON message BEGIN "
            "DELETE FROM search_index WHERE kind = 'message' AND ref_id = OLD.id; END"
        )
    with engine.connect() as connection:
        # Equal counts: only the trigger layout tells that the rows must be rebuilt.
        assert search_index.index_count(connection) == search_index.source_count(connection)
    database.init_db()
    with engine.connect() as connection:
        triggers = _trigger_statements(connection)
        assert sorted(triggers) == sorted(names)
        assert "rowid = OLD.id * 4 + 1" in triggers["search_message_ad"][0]
        assert search_index.index_count(connection) == search_index.source_count(connection)
    test_index_rows_carry_rowids_derived_from_their_sources(engine)
    with Session(engine) as session:
        session.delete(session.get(Message, 31))
        session.commit()
        assert ("message", 31, 1, 3) not in rows(engine)
        assert [hit.ref_id for hit in search_text(session, "SYNTHETIC_FOREIGN")] == [41]


def _set_tokenizer(engine, old, new):
    """Rewrite the stored table definition, as a database moved between SQLite builds."""
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA writable_schema = ON")
        connection.exec_driver_sql(
            "UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name = 'search_index'",
            (old, new),
        )
        connection.exec_driver_sql("PRAGMA writable_schema = OFF")
    engine.dispose()


def _search_triggers(engine) -> list[str]:
    with engine.connect() as connection:
        return list(_trigger_statements(connection))


@requires_fts5
def test_an_index_this_sqlite_cannot_open_never_blocks_writes(engine):
    _set_tokenizer(engine, "remove_diacritics 1", "remove_diacritics 9")
    assert ensure_search_index(engine) is False
    assert _search_triggers(engine) == []
    with Session(engine) as session:
        session.add(Message(node_id=2, role="assistant", content="partial", status="pending"))
        session.add(Message(node_id=2, role="assistant", content="SYNTHETIC_UNINDEXED answer"))
        session.add(Node(id=6, tree_id=1, parent_id=1, title="新节点", learning_note="新笔记"))
        fact = session.get(Memory, 101)
        fact.content = "快速排序在离线期间改写过。"
        session.add(fact)
        session.commit()
        # Search falls back to a LIKE scan instead of returning nothing.
        hits = search_text(session, "SYNTHETIC_UNINDEXED", tree_id=1)
        assert [(hit.kind, hit.node_id) for hit in hits] == [("message", 2)]

    # Back on an SQLite that opens the table: triggers return and the index is rebuilt,
    # including the edit made meanwhile, although the row counts happen to agree.
    _set_tokenizer(engine, "remove_diacritics 9", "remove_diacritics 1")
    assert ensure_search_index(engine)
    assert len(_search_triggers(engine)) == 11
    with engine.connect() as connection:
        assert search_index.index_count(connection) == search_index.source_count(connection)
    with Session(engine) as session:
        assert [hit.ref_id for hit in search_text(session, "离线期间改写", kinds=["memory"])] == [
            101
        ]
        assert search_text(session, "SYNTHETIC_UNINDEXED", tree_id=1)[0].kind == "message"


@requires_fts5
def test_without_fts5_the_triggers_are_removed_and_restored_later(engine, monkeypatch):
    monkeypatch.setattr(search_index, "fts5_available", lambda connection: False)
    assert ensure_search_index(engine) is False
    assert _search_triggers(engine) == []
    with Session(engine) as session:
        fact = session.get(Memory, 103)
        fact.content = "foreign fact rewritten offline"
        session.add(fact)
        session.commit()
    monkeypatch.undo()
    assert ensure_search_index(engine)
    assert len(_search_triggers(engine)) == 11
    with Session(engine) as session:
        assert [hit.ref_id for hit in search_text(session, "rewritten offline")] == [103]


@requires_fts5
def test_fts_queries_are_phrases_and_filters_bound_tree_and_nodes(engine):
    with Session(engine) as session:
        hits = search_text(session, "quicksort", tree_id=1)
        assert {(hit.kind, hit.ref_id) for hit in hits} == {
            ("message", 21),
            ("message", 31),
            ("memory", 102),
            ("note", 2),
        }
        assert [hit.rank for hit in hits] == [1, 2, 3, 4]
        path_only = search_text(session, "quicksort", tree_id=1, node_ids=[1, 2])
        assert {(hit.kind, hit.ref_id) for hit in path_only} == {("message", 21), ("note", 2)}
        assert {hit.tree_id for hit in search_text(session, "quicksort", tree_ids=[1, 2])} == {1, 2}
        assert search_text(session, "quicksort", tree_id=1, kinds=["memory"])[0].ref_id == 102
        assert search_text(session, "quicksort", tree_id=1, kinds=["secret"]) == []
        assert search_text(session, "quicksort", tree_id=1, limit=1) == [
            search_text(session, "quicksort", tree_id=1)[0]
        ]
        complexity = search_text(session, "复杂度是 O(n log n)", tree_id=1)
        assert [hit.ref_id for hit in complexity] == [12]
        assert "O(n log n)" in complexity[0].snippet
        # Operators, quotes and column syntax are literal text, never FTS5 syntax.
        for raw in ['O(n log n) AND "quote" OR NOT -x:y', 'kind:"message"', "***", "("]:
            assert isinstance(search_text(session, raw, tree_id=1), list)


def test_short_queries_and_missing_fts5_use_like_with_literal_wildcards(engine, monkeypatch):
    with Session(engine) as session:
        short = search_text(session, "最坏", tree_id=1)
        assert [(hit.kind, hit.ref_id) for hit in short] == [("message", 12)]
        assert "最坏" in short[0].snippet and short[0].rank == 1
        assert search_text(session, "%_", tree_id=1) == []
        assert search_text(session, "   ", tree_id=1) == []
        monkeypatch.setattr(search_index, "fts5_available", lambda connection: False)
        fallback = search_text(session, "quicksort", tree_id=1, node_ids=[1, 2])
        assert {(hit.kind, hit.ref_id) for hit in fallback} == {("message", 21), ("note", 2)}
        assert all("quicksort" in hit.snippet for hit in fallback)
        assert search_text(session, "QUICKSORT", tree_id=2, kinds=["memory"])[0].ref_id == 103


def test_excerpts_keep_the_match_when_case_folding_changes_lengths(engine):
    from app.search_index import excerpt

    german = "Das heißt: " * 40 + "Quicksort teilt das Array in zwei Teile und sortiert sie."
    snippet = excerpt(german, "quicksort")
    assert "Quicksort teilt" in snippet and snippet.strip("…") in german
    ligatures = "ﬁﬂ" * 30 + " QuickSort teilt"
    assert "QuickSort" in excerpt(ligatures, "QUICKSORT")
    # Matches that only diacritic folding finds keep the FTS snippet.
    assert excerpt("Ein café am Markt", "cafe", "…Ein café…") == "…Ein café…"
    with Session(engine) as session:
        session.add(Message(id=70, node_id=2, role="assistant", content=german))
        session.commit()
        hits = search_text(session, "Quicksort teilt", tree_id=1)
        assert [hit.ref_id for hit in hits] == [70] and "Quicksort teilt" in hits[0].snippet


def test_database_errors_become_empty_results(engine, monkeypatch):
    def broken(*args, **kwargs):
        raise OperationalError("SELECT secret", {}, Exception("no such table"))

    monkeypatch.setattr(search_index, "_fts_rows", broken)
    monkeypatch.setattr(search_index, "_like_rows", broken)
    with Session(engine) as session:
        assert search_text(session, "quicksort", tree_id=1) == []
        assert search_text(session, "快", tree_id=1) == []


@requires_fts5
def test_searching_never_writes(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        with Session(engine) as session:
            search_text(session, "quicksort", tree_id=1)
            search_text(session, "最坏", tree_id=1)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements and not {"INSERT", "UPDATE", "DELETE", "REPLACE"}.intersection(statements)


@requires_fts5
@pytest.mark.parametrize("deletion", ["node", "tree"])
def test_deleting_a_subtree_or_tree_removes_its_index_rows(engine, deletion):
    with Session(engine) as session:
        if deletion == "node":
            nodes.delete_node(1, session=session)
        else:
            trees.delete_tree(1, session=session)
    remaining = rows(engine)
    assert all(row[2] != 1 for row in remaining), remaining
    assert ("message", 41, 2, 4) in remaining and ("memory", 103, 2, 4) in remaining


def test_legacy_schema_gets_the_index_only_after_its_columns_exist(tmp_path, monkeypatch):
    from sqlalchemy import text

    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}", connect_args={"check_same_thread": False}
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledgetree (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE node (id INTEGER PRIMARY KEY, tree_id INTEGER NOT NULL, "
                "parent_id INTEGER, title TEXT NOT NULL, seed_text TEXT, summary TEXT, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE message (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, answered_by TEXT, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE memory (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
                "content TEXT NOT NULL, tree_id INTEGER, source_node_id INTEGER, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO knowledgetree VALUES (1, '旧主题', '2026-01-01')"))
        connection.execute(
            text("INSERT INTO node VALUES (1, 1, NULL, '旧根', NULL, NULL, '2026-01-01')")
        )
        connection.execute(
            text(
                "INSERT INTO message VALUES (1, 1, 'assistant', 'SYNTHETIC_OLD', NULL, '2026-01-01')"
            )
        )
        connection.execute(
            text("INSERT INTO memory VALUES (1, 'fact', 'old fact', 1, 1, '2026-01-01')")
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    if not _has_fts5():
        assert not triggers
        return
    assert "search_message_ai" in triggers and "search_memory_au" in triggers
    with Session(engine) as session:
        assert [hit.ref_id for hit in search_text(session, "SYNTHETIC_OLD", tree_id=1)] == [1]
        assert [hit.ref_id for hit in search_text(session, "old fact", tree_id=1)] == [1]
        # Writes through the new triggers work on the migrated table.
        session.add(Message(node_id=1, role="assistant", content="SYNTHETIC_NEW"))
        session.commit()
        assert len(search_text(session, "SYNTHETIC_NEW", tree_id=1)) == 1
        assert len(session.exec(select(Message)).all()) == 2
    engine.dispose()
