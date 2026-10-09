"""Trigger-maintained SQLite FTS5 index over completed answers, current facts and notes.

The index is derived data: SQLite triggers keep it in step with the source rows,
start-up rebuilds it when its row count disagrees with the sources, and every
reader degrades to a LIKE scan when FTS5 is unavailable. Nothing in this module
writes to the source tables.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, select

from .models import Memory, Message, Node

logger = logging.getLogger(__name__)

INDEX_TABLE = "search_index"
KINDS = ("message", "memory", "note")
# Trigram tokens need three characters; shorter queries use LIKE instead.
MIN_FTS_QUERY_CHARS = 3
_COLUMNS = "kind, ref_id, tree_id, node_id, content"
# Trigger bodies are only resolved when they fire, so a trigger created against a
# legacy table that still lacks a column would break every later write.
_REQUIRED_COLUMNS = {
    "message": {"id", "node_id", "status", "content"},
    "memory": {"id", "kind", "tree_id", "source_node_id", "content", "superseded_by"},
    "node": {"id", "tree_id", "learning_note"},
}

_CREATE_TABLE = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {INDEX_TABLE} USING fts5("
    "kind UNINDEXED, ref_id UNINDEXED, tree_id UNINDEXED, node_id UNINDEXED, content, "
    "tokenize='trigram remove_diacritics 1')"
)
_MESSAGE_ROW = (
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'message', NEW.id, node.tree_id, NEW.node_id, NEW.content "
    "FROM node WHERE node.id = NEW.node_id"
)
_MEMORY_ROW = (
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'memory', NEW.id, NEW.tree_id, NEW.source_node_id, NEW.content "
    "WHERE NEW.kind = 'fact' AND NEW.superseded_by IS NULL"
)
_NOTE_ROW = (
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'note', NEW.id, NEW.tree_id, NEW.id, NEW.learning_note "
    "WHERE NEW.learning_note IS NOT NULL AND NEW.learning_note <> ''"
)
_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS search_message_ai AFTER INSERT ON message "
    f"WHEN NEW.status = 'complete' BEGIN {_MESSAGE_ROW}; END",
    "CREATE TRIGGER IF NOT EXISTS search_message_au "
    "AFTER UPDATE OF content, status, node_id ON message BEGIN "
    f"DELETE FROM {INDEX_TABLE} WHERE kind = 'message' AND ref_id = OLD.id; "
    f"{_MESSAGE_ROW} AND NEW.status = 'complete'; END",
    "CREATE TRIGGER IF NOT EXISTS search_message_ad AFTER DELETE ON message BEGIN "
    f"DELETE FROM {INDEX_TABLE} WHERE kind = 'message' AND ref_id = OLD.id; END",
    "CREATE TRIGGER IF NOT EXISTS search_memory_ai AFTER INSERT ON memory "
    f"WHEN NEW.kind = 'fact' AND NEW.superseded_by IS NULL BEGIN {_MEMORY_ROW}; END",
    "CREATE TRIGGER IF NOT EXISTS search_memory_au "
    "AFTER UPDATE OF kind, content, tree_id, source_node_id, superseded_by ON memory BEGIN "
    f"DELETE FROM {INDEX_TABLE} WHERE kind = 'memory' AND ref_id = OLD.id; {_MEMORY_ROW}; END",
    "CREATE TRIGGER IF NOT EXISTS search_memory_ad AFTER DELETE ON memory BEGIN "
    f"DELETE FROM {INDEX_TABLE} WHERE kind = 'memory' AND ref_id = OLD.id; END",
    "CREATE TRIGGER IF NOT EXISTS search_note_ai AFTER INSERT ON node "
    f"WHEN NEW.learning_note IS NOT NULL AND NEW.learning_note <> '' BEGIN {_NOTE_ROW}; END",
    # One flush may write a child message before its node; index those messages now.
    "CREATE TRIGGER IF NOT EXISTS search_node_messages_ai AFTER INSERT ON node BEGIN "
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'message', m.id, NEW.tree_id, m.node_id, m.content FROM message m "
    "WHERE m.node_id = NEW.id AND m.status = 'complete' AND NOT EXISTS "
    f"(SELECT 1 FROM {INDEX_TABLE} s WHERE s.kind = 'message' AND s.ref_id = m.id); END",
    "CREATE TRIGGER IF NOT EXISTS search_note_au AFTER UPDATE OF learning_note ON node BEGIN "
    f"DELETE FROM {INDEX_TABLE} WHERE kind = 'note' AND ref_id = OLD.id; {_NOTE_ROW}; END",
    "CREATE TRIGGER IF NOT EXISTS search_node_tree_au AFTER UPDATE OF tree_id ON node BEGIN "
    f"UPDATE {INDEX_TABLE} SET tree_id = NEW.tree_id WHERE node_id = NEW.id; END",
    "CREATE TRIGGER IF NOT EXISTS search_node_ad AFTER DELETE ON node BEGIN "
    f"DELETE FROM {INDEX_TABLE} WHERE node_id = OLD.id OR (kind = 'note' AND ref_id = OLD.id); "
    "END",
)
_SOURCE_COUNT = (
    "SELECT (SELECT COUNT(*) FROM message m JOIN node n ON n.id = m.node_id "
    "WHERE m.status = 'complete')"
    " + (SELECT COUNT(*) FROM memory WHERE kind = 'fact' AND superseded_by IS NULL)"
    " + (SELECT COUNT(*) FROM node WHERE learning_note IS NOT NULL AND learning_note <> '')"
)
_REBUILD = (
    f"DELETE FROM {INDEX_TABLE}",
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'message', m.id, n.tree_id, m.node_id, m.content "
    "FROM message m JOIN node n ON n.id = m.node_id WHERE m.status = 'complete'",
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'memory', id, tree_id, source_node_id, content "
    "FROM memory WHERE kind = 'fact' AND superseded_by IS NULL",
    f"INSERT INTO {INDEX_TABLE}({_COLUMNS}) "
    "SELECT 'note', id, tree_id, id, learning_note "
    "FROM node WHERE learning_note IS NOT NULL AND learning_note <> ''",
)


def fts5_available(connection) -> bool:
    """FTS5 is a build-time property of the SQLite library linked into Python."""
    row = connection.exec_driver_sql("SELECT 1 FROM pragma_module_list WHERE name = 'fts5'")
    return row.first() is not None


def _index_exists(connection) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (INDEX_TABLE,)
    )
    return row.first() is not None


def _schema_ready(connection) -> bool:
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
        if not required <= columns:
            return False
    return True


def _create(connection) -> None:
    connection.exec_driver_sql(_CREATE_TABLE)
    for statement in _TRIGGERS:
        connection.exec_driver_sql(statement)


def _rebuild(connection) -> None:
    for statement in _REBUILD:
        connection.exec_driver_sql(statement)


def index_count(connection) -> int:
    return int(connection.exec_driver_sql(f"SELECT COUNT(*) FROM {INDEX_TABLE}").scalar())


def source_count(connection) -> int:
    return int(connection.exec_driver_sql(_SOURCE_COUNT).scalar())


def _ensure(connection, *, rebuild: bool) -> bool:
    if not fts5_available(connection):
        logger.warning("SQLite FTS5 is unavailable; search falls back to LIKE scans")
        return False
    if not _schema_ready(connection):
        # Legacy tables gain their columns in init_db before the index is retried.
        return False
    _create(connection)
    if rebuild and index_count(connection) != source_count(connection):
        logger.info("Rebuilding the search index from stored records")
        _rebuild(connection)
    return True


@event.listens_for(SQLModel.metadata, "after_create")
def _after_create(target, connection, **kw) -> None:
    """Give schemas built with create_all alone (tests) the same index as init_db."""
    try:
        _ensure(connection, rebuild=False)
    except OperationalError as exc:
        logger.warning("Search index not created: %s", exc)


def ensure_search_index(engine) -> bool:
    """Create the index and triggers if possible and rebuild a stale index. Never raises."""
    try:
        with engine.begin() as connection:
            return _ensure(connection, rebuild=True)
    except OperationalError as exc:
        logger.warning("Search index unavailable: %s", exc)
        return False


@dataclass(frozen=True)
class SearchHit:
    kind: str  # "message" | "memory" | "note"
    ref_id: int  # Message id, memory id or node id depending on kind.
    tree_id: int | None
    node_id: int | None
    snippet: str  # A verbatim excerpt; FTS5 marks omitted text with an ellipsis.
    rank: int  # 1-based position in the returned ordering.


def _phrase(query: str) -> str:
    # User text is a phrase, never FTS5 query syntax.
    return '"' + query.replace('"', '""') + '"'


def _fts_ready(session: Session) -> bool:
    connection = session.connection()
    return fts5_available(connection) and _index_exists(connection)


def _in_clause(column: str, values: Sequence, params: dict) -> str:
    names = []
    for value in values:
        name = f"p{len(params)}"
        params[name] = value
        names.append(f":{name}")
    return f"{column} IN ({', '.join(names)})" if names else "0"


def _excerpt(content: str, needle: str, fallback: str = "") -> str:
    """A verbatim window around the first match, so the UI can highlight that passage.

    FTS5's own snippet() places its window by token position, which with trigram
    phrases can land on the start of the text instead of the match; it is only
    used when a case-folded search cannot locate the match (diacritics).
    """
    position = content.casefold().find(needle.casefold())
    if position < 0:
        return fallback or content[:40] + ("…" if len(content) > 40 else "")
    start = max(0, position - 12)
    end = min(len(content), position + len(needle) + 28)
    return ("…" if start > 0 else "") + content[start:end] + ("…" if end < len(content) else "")


def _fts_rows(session, needle, tree_id, tree_ids, node_ids, kinds, limit) -> list[tuple]:
    from sqlalchemy import text

    params: dict = {"phrase": _phrase(needle), "limit": limit}
    clauses = [f"{INDEX_TABLE} MATCH :phrase", _in_clause("kind", kinds, params)]
    if tree_id is not None:
        params["tree_id"] = tree_id
        clauses.append("tree_id = :tree_id")
    if tree_ids is not None:
        clauses.append(_in_clause("tree_id", list(tree_ids), params))
    if node_ids is not None:
        clauses.append(_in_clause("node_id", list(node_ids), params))
    statement = text(
        "SELECT kind, ref_id, tree_id, node_id, content, "
        f"snippet({INDEX_TABLE}, 4, '', '', '…', 12) "
        f"FROM {INDEX_TABLE} WHERE {' AND '.join(clauses)} "
        f"ORDER BY bm25({INDEX_TABLE}, 0, 0, 0, 0, 1.0), ref_id DESC LIMIT :limit"
    )
    return [
        (kind, ref_id, hit_tree, node_id, _excerpt(content or "", needle, snippet))
        for kind, ref_id, hit_tree, node_id, content, snippet in session.execute(statement, params)
    ]


def _like_rows(session, needle, tree_id, tree_ids, node_ids, kinds, limit) -> list[tuple]:
    rows: list[tuple] = []
    for kind in kinds:
        if kind == "message":
            statement = (
                select(Message.id, Node.tree_id, Message.node_id, Message.content)
                .join(Node, Node.id == Message.node_id)
                .where(
                    Message.status == "complete",
                    Message.content.icontains(needle, autoescape=True),
                )
            )
            tree_column, node_column, order = Node.tree_id, Message.node_id, Message.id
        elif kind == "memory":
            statement = select(
                Memory.id, Memory.tree_id, Memory.source_node_id, Memory.content
            ).where(
                Memory.kind == "fact",
                Memory.superseded_by.is_(None),
                Memory.content.icontains(needle, autoescape=True),
            )
            tree_column, node_column, order = Memory.tree_id, Memory.source_node_id, Memory.id
        else:
            statement = select(Node.id, Node.tree_id, Node.id, Node.learning_note).where(
                Node.learning_note.icontains(needle, autoescape=True)
            )
            tree_column, node_column, order = Node.tree_id, Node.id, Node.id
        if tree_id is not None:
            statement = statement.where(tree_column == tree_id)
        if tree_ids is not None:
            statement = statement.where(tree_column.in_(list(tree_ids)))
        if node_ids is not None:
            statement = statement.where(node_column.in_(list(node_ids)))
        for ref_id, hit_tree, node_id, content in session.exec(
            statement.order_by(order.desc()).limit(limit)
        ):
            rows.append((kind, ref_id, hit_tree, node_id, _excerpt(content or "", needle)))
    return rows[:limit]


def search_text(
    session: Session,
    query: str,
    *,
    tree_id: int | None = None,
    tree_ids: Sequence[int] | None = None,
    node_ids: Sequence[int] | None = None,
    kinds: Sequence[str] | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    """Find completed answers, current facts and notes containing the user's text.

    FTS5 ranks by BM25 when it is available and the query has at least three
    characters; otherwise a case-insensitive LIKE scan runs. Database errors
    yield an empty result instead of reaching the caller.
    """
    needle = query.strip()
    selected = tuple(kind for kind in (kinds or KINDS) if kind in KINDS)
    if not needle or limit <= 0 or not selected:
        return []
    try:
        if len(needle) >= MIN_FTS_QUERY_CHARS and _fts_ready(session):
            rows = _fts_rows(session, needle, tree_id, tree_ids, node_ids, selected, limit)
        else:
            rows = _like_rows(session, needle, tree_id, tree_ids, node_ids, selected, limit)
    except OperationalError:
        return []
    return [
        SearchHit(kind, ref_id, hit_tree, node_id, snippet, position)
        for position, (kind, ref_id, hit_tree, node_id, snippet) in enumerate(rows, 1)
    ]
