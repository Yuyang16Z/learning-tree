"""Allocate stable topic/node IDs without rebuilding legacy SQLite tables."""

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from .models import KnowledgeTree, Node, TreeIdSequence


def _record_id(session: Session, model, sequence_id: int, *, allocate: bool) -> int:
    """Atomically remember the largest existing/deleted ID, optionally reserving the next."""
    current = select(func.coalesce(func.max(model.id), 0)).scalar_subquery()
    increment = int(allocate)
    statement = insert(TreeIdSequence).values(id=sequence_id, last_id=current + increment)
    statement = statement.on_conflict_do_update(
        index_elements=["id"],
        set_={"last_id": func.max(TreeIdSequence.last_id, current) + increment},
    ).returning(TreeIdSequence.last_id)
    return session.execute(statement).scalar_one()


def record_tree_id(session: Session, *, allocate: bool = False) -> int:
    return _record_id(session, KnowledgeTree, 1, allocate=allocate)


def record_node_id(session: Session, *, allocate: bool = False) -> int:
    return _record_id(session, Node, 2, allocate=allocate)
