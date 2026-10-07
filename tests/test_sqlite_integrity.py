"""SQLite connection settings: enforced foreign keys, WAL, lock waits and request uniqueness."""

import logging
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as database
from app.models import KnowledgeTree, Message, Node


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'integrity.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_every_connection_enforces_keys_waits_on_locks_and_uses_wal(engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == database.BUSY_TIMEOUT_MS
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_children_may_be_written_before_parents_within_one_transaction(engine):
    with Session(engine) as session:
        # No ORM relationships order these inserts; the check runs at COMMIT.
        session.add(Message(id=1, node_id=1, role="user", content="question"))
        session.add(Node(id=1, tree_id=1, title="root"))
        session.add(KnowledgeTree(id=1, title="topic"))
        session.commit()
    with Session(engine) as session:
        assert session.get(Message, 1).node_id == 1


def test_dangling_reference_fails_commit_and_leaves_connection_clean(engine):
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="topic"))
        session.add(Node(id=1, tree_id=1, title="root"))
        session.commit()
    with Session(engine) as session:
        session.add(Message(id=2, node_id=999, role="user", content="orphan"))
        with pytest.raises(IntegrityError):
            session.commit()
    # The rejected write must not leak into the pooled connection's next transaction.
    with Session(engine) as session:
        session.add(Message(id=3, node_id=1, role="user", content="valid"))
        session.commit()
        assert [m.id for m in session.exec(select(Message))] == [3]


def test_deleting_a_parent_with_children_is_rejected(engine):
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="topic"))
        session.add(Node(id=1, tree_id=1, title="root"))
        session.add(Node(id=2, tree_id=1, parent_id=1, title="child"))
        session.commit()
    with Session(engine) as session:
        session.delete(session.get(Node, 1))
        with pytest.raises(IntegrityError):
            session.commit()
    with Session(engine) as session:
        assert session.get(Node, 2).parent_id == 1


def test_init_db_adds_unique_request_index(engine, monkeypatch):
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    with Session(engine) as session:
        session.add(KnowledgeTree(id=1, title="topic"))
        session.add(Node(id=1, tree_id=1, title="a", request_id="same"))
        session.add(Node(id=2, tree_id=1, title="b"))
        session.add(Node(id=3, tree_id=1, title="c"))  # Several nodes may have no request.
        session.commit()
    with Session(engine) as session:
        session.add(Node(id=4, tree_id=1, title="duplicate", request_id="same"))
        with pytest.raises(IntegrityError):
            session.commit()


def _legacy_database(path):
    """An older database created without foreign-key enforcement."""
    seed = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(seed)
    seed.dispose()
    raw = sqlite3.connect(path)  # Plain connection: foreign keys off, as before.
    raw.execute(
        "INSERT INTO knowledgetree (id, title, archived, created_at) VALUES (1, 't', 0, '')"
    )
    for node_id in (1, 2):
        raw.execute(
            "INSERT INTO node (id, tree_id, title, kind, status, title_state, request_id, "
            "created_at) VALUES (?, 1, 'n', 'followup', 'idle', 'manual', 'dup', '')",
            (node_id,),
        )
    raw.execute(
        "INSERT INTO message (id, node_id, role, status, content, created_at) "
        "VALUES (1, 404, 'user', 'complete', 'orphan', '')"
    )
    raw.commit()
    raw.close()


def test_legacy_duplicates_and_orphans_are_reported_not_fatal(tmp_path, monkeypatch, caplog):
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", engine)
    with caplog.at_level(logging.WARNING, logger="app.db"):
        database.init_db()
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Skipping unique request index" in messages
    assert "reference missing parents in: message" in messages
    with engine.connect() as conn:
        indexes = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(node)")}
    assert "ux_node_tree_request" not in indexes
    engine.dispose()
