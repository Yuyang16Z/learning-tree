from collections.abc import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # SQLite + 多线程（流式在线程池里跑）
)

# 幂等小迁移：给已存在的库补上后加的列（create_all 只建新表、不会 ALTER 旧表）。
_ADDED_COLUMNS = {
    "modelconfig": {
        "protocol": "TEXT NOT NULL DEFAULT 'openai'",
        "max_tokens": "INTEGER NOT NULL DEFAULT 4096",
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
                continue  # 表还没建，create_all 会带上新列
            for name, sqltype in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}"))


def init_db() -> None:
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
