"""Memory management: list, delete one or clear all.

Automatic extraction runs in the background through nodes.ask; this module only manages records."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete
from sqlmodel import Session, select

from ..db import get_session
from ..models import Memory, MemoryEmbedding
from ..schemas import MemoryOut

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("", response_model=list[MemoryOut])
def list_memories(session: Session = Depends(get_session)) -> list[MemoryOut]:
    rows = session.exec(select(Memory).order_by(Memory.id.desc())).all()
    return [MemoryOut(id=m.id, kind=m.kind, content=m.content, tree_id=m.tree_id) for m in rows]


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, session: Session = Depends(get_session)) -> dict:
    m = session.get(Memory, memory_id)
    if not m:
        raise HTTPException(404, "记忆不存在")
    session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id))
    session.delete(m)
    session.commit()
    return {"deleted": memory_id}


@router.delete("")
def clear_memories(session: Session = Depends(get_session)) -> dict:
    session.execute(delete(MemoryEmbedding))
    n = 0
    for m in session.exec(select(Memory)).all():
        session.delete(m)
        n += 1
    session.commit()
    return {"cleared": n}


@router.get("/retrieval/status")
def retrieval_status() -> dict:
    from ..semantic_models import get_status

    return get_status()


@router.post("/retrieval/prepare")
def prepare_retrieval() -> dict:
    from ..semantic_models import start_prepare

    return start_prepare()
