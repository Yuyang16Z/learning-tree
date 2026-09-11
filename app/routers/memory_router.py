"""长期记忆：列 / 删一条 / 清空。自动提炼在 nodes.ask 里后台完成，这里只做管理。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db import get_session
from ..models import Memory
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
    session.delete(m)
    session.commit()
    return {"deleted": memory_id}


@router.delete("")
def clear_memories(session: Session = Depends(get_session)) -> dict:
    n = 0
    for m in session.exec(select(Memory)).all():
        session.delete(m)
        n += 1
    session.commit()
    return {"cleared": n}
