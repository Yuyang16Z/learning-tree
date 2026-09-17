from collections.abc import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import settings

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


def init_db() -> None:
    from . import documents  # noqa: F401 -- register the additive document table

    # Only additive migrations. Legacy multi-turn nodes keep their original IDs,
    # messages and branch attachments; the thread API presents every Q/A pair.
    SQLModel.metadata.create_all(engine)
    _ensure_columns()
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
