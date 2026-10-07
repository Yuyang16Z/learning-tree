import logging
import sqlite3
from collections.abc import Iterator

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from .config import settings

logger = logging.getLogger(__name__)

# Writers wait this long for a lock instead of failing at once with "database is locked".
BUSY_TIMEOUT_MS = 5000


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """Apply per-connection SQLite settings to every engine, including test engines.

    SQLite leaves foreign keys unenforced unless each connection turns them on. WAL lets
    readers continue while one writer commits; in-memory databases keep their own mode.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


@event.listens_for(Engine, "begin")
def _defer_foreign_keys(connection) -> None:
    """Check foreign keys at COMMIT rather than per statement.

    The ORM does not order inserts and deletes between tables without relationships, so a
    child row can be written before its parent inside one flush. Deferral keeps enforcement
    (a dangling reference still fails the commit) without depending on statement order.
    SQLite clears this setting at every COMMIT or ROLLBACK, so it is set per transaction.
    """
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")


@event.listens_for(Engine, "handle_error")
def _end_failed_commit(context) -> None:
    """A COMMIT rejected by a deferred foreign key leaves SQLite's transaction open.

    SQLAlchemy does not roll back the DBAPI connection after a failed commit, so the pooled
    connection would carry the rejected writes into its next transaction. End it here.
    """
    if context.statement is not None or context.connection is None:
        return  # Only commit failures: statement errors are rolled back normally.
    dbapi_connection = context.connection.connection.dbapi_connection
    if isinstance(dbapi_connection, sqlite3.Connection) and dbapi_connection.in_transaction:
        dbapi_connection.rollback()


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # Streaming runs in worker threads.
)

# Idempotently add missing columns; create_all creates tables but cannot ALTER existing ones.
_ADDED_COLUMNS = {
    "knowledgetree": {
        "archived": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "modelconfig": {
        "protocol": "TEXT NOT NULL DEFAULT 'openai'",
        "max_tokens": "INTEGER NOT NULL DEFAULT 4096",
        "context_window": "INTEGER NOT NULL DEFAULT 32768",
    },
    "node": {
        "kind": "TEXT NOT NULL DEFAULT 'followup'",
        "status": "TEXT NOT NULL DEFAULT 'idle'",
        "error": "TEXT",
        "source_node_id": "INTEGER",
        "source_message_id": "INTEGER",
        "source_start": "INTEGER",
        "source_end": "INTEGER",
        "learning_note": "TEXT",
        "revision_of": "INTEGER",
        "request_id": "TEXT",
        "title_state": "TEXT NOT NULL DEFAULT 'legacy'",
    },
    "message": {
        "images": "JSON",
        "document_ids": "JSON",
        "reasoning": "TEXT",
        "steps": "JSON",
        "status": "TEXT NOT NULL DEFAULT 'complete'",
    },
}


def _ensure_columns() -> None:
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # create_all will include the new columns when creating this table.
            for name, sqltype in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}"))
                    if table == "modelconfig" and name == "context_window":
                        # Existing output limits can exceed the new default.
                        # Backfill only once; never overwrite a user's later choice.
                        conn.execute(
                            text(
                                "UPDATE modelconfig SET context_window = "
                                "MAX(32768, COALESCE(max_tokens, 4096) * 2 + 4096)"
                            )
                        )


def _ensure_indexes() -> None:
    """Back request-ID uniqueness with the database, not only the request lock."""
    with engine.begin() as conn:
        duplicate = conn.execute(
            text(
                "SELECT tree_id, request_id FROM node WHERE request_id IS NOT NULL "
                "GROUP BY tree_id, request_id HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            # Never fail startup over legacy data; the request lock still serializes asks.
            logger.warning(
                "Skipping unique request index: tree %s reuses request %s",
                duplicate[0],
                duplicate[1],
            )
            return
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_node_tree_request "
                "ON node (tree_id, request_id) WHERE request_id IS NOT NULL"
            )
        )


def _report_foreign_key_violations() -> None:
    """Older databases ran without enforcement; report orphans instead of failing startup."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    if rows:
        tables = sorted({row[0] for row in rows})
        logger.warning("%d row(s) reference missing parents in: %s", len(rows), ", ".join(tables))


def init_db() -> None:
    from . import documents  # noqa: F401 -- register the additive document table

    # Only additive migrations. Legacy multi-turn nodes keep their original IDs,
    # messages and branch attachments; the thread API presents every Q/A pair.
    SQLModel.metadata.create_all(engine)
    _ensure_columns()
    _ensure_indexes()
    _report_foreign_key_violations()
    from .models import Message, Node
    from .title_generation import fallback_title

    with Session(engine) as session:
        for node in session.exec(select(Node).where(Node.status == "pending")).all():
            node.status = "interrupted"
            node.error = "上次生成因服务重启中断，可重试。"
            session.add(node)
        for node in session.exec(
            select(Node).where(Node.title_state.in_(["legacy", "pending"]))
        ).all():
            # Repair only the recognizable old auto-label, not custom titles.
            old_source_title = len(node.seed_text or "") > 24 and node.title == node.seed_text[:24]
            if node.title_state == "pending" or old_source_title:
                question = session.exec(
                    select(Message)
                    .where(Message.node_id == node.id, Message.role == "user")
                    .order_by(Message.id)
                ).first()
                node.title = fallback_title(question.content) if question else ""
                node.title_state = "fallback" if question else "empty"
                session.add(node)
            elif node.kind == "branch" or node.seed_text:
                node.title_state = "manual" if node.title.strip() else "empty"
                session.add(node)
        session.commit()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
