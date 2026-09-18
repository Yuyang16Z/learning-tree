"""Manage a user-owned preference profile and searchable, source-linked topic facts."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from ..db import get_session
from ..memory_preferences import begin_memory_write, read_preferences
from ..models import (
    KnowledgeTree,
    Memory,
    MemoryEmbedding,
    PreferenceLearningState,
    PreferenceProfile,
    PreferenceSupplement,
)
from ..preference_supplements import read_learning_state, supplement_out
from ..schemas import (
    FactDeleteIn,
    FactOut,
    FactPageOut,
    FactPatch,
    FactTopicOut,
    MemoryOut,
    PreferenceLearningIn,
    PreferenceLearningOut,
    PreferenceProfileIn,
    PreferenceProfileOut,
    PreferenceSupplementOut,
    PreferenceSupplementPageOut,
    PreferenceSupplementPatch,
)

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("", response_model=list[MemoryOut])
def list_memories(session: Session = Depends(get_session)) -> list[MemoryOut]:
    rows = session.exec(select(Memory).order_by(Memory.id.desc())).all()
    return [MemoryOut(id=m.id, kind=m.kind, content=m.content, tree_id=m.tree_id) for m in rows]


@router.get("/preferences", response_model=PreferenceProfileOut)
def get_preferences(session: Session = Depends(get_session)) -> PreferenceProfileOut:
    return read_preferences(session)


@router.get("/preferences/learning", response_model=PreferenceLearningOut)
def get_preference_learning(session: Session = Depends(get_session)) -> PreferenceLearningOut:
    return read_learning_state(session)


@router.put("/preferences/learning", response_model=PreferenceLearningOut)
def save_preference_learning(
    body: PreferenceLearningIn, session: Session = Depends(get_session)
) -> PreferenceLearningOut:
    begin_memory_write(session)
    current = read_learning_state(session)
    if body.revision != current.revision:
        raise HTTPException(409, "偏好学习设置已在其他窗口更新，请重新载入。")
    state = session.get(PreferenceLearningState, 1) or PreferenceLearningState()
    state.enabled = body.enabled
    state.revision = uuid4().hex
    session.add(state)
    session.commit()
    return read_learning_state(session)


@router.get("/preferences/supplements", response_model=PreferenceSupplementPageOut)
def list_preference_supplements(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> PreferenceSupplementPageOut:
    total = session.exec(select(func.count()).select_from(PreferenceSupplement)).one()
    rows = session.exec(
        select(PreferenceSupplement)
        .order_by(PreferenceSupplement.updated_at.desc(), PreferenceSupplement.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PreferenceSupplementPageOut(
        items=[supplement_out(session, row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.patch("/preferences/supplements/{supplement_id}", response_model=PreferenceSupplementOut)
def edit_preference_supplement(
    supplement_id: int,
    body: PreferenceSupplementPatch,
    session: Session = Depends(get_session),
) -> PreferenceSupplementOut:
    content = body.content.strip()
    if not content:
        raise HTTPException(422, "偏好内容不能为空。")
    begin_memory_write(session)
    supplement = session.get(PreferenceSupplement, supplement_id)
    if supplement is None:
        raise HTTPException(404, "偏好补充不存在。")
    if supplement.revision != body.revision:
        raise HTTPException(409, "偏好补充已在其他窗口更新，请重新载入后再保存。")
    supplement.content = content
    supplement.user_edited = True
    supplement.revision = uuid4().hex
    supplement.updated_at = datetime.now(timezone.utc)
    # Editing a conflicting suggestion does not silently make it an active preference.
    session.add(supplement)
    session.commit()
    session.refresh(supplement)
    return supplement_out(session, supplement)


@router.delete("/preferences/supplements/{supplement_id}")
def delete_preference_supplement(
    supplement_id: int,
    revision: str = Query(min_length=1, max_length=100),
    session: Session = Depends(get_session),
) -> dict:
    begin_memory_write(session)
    supplement = session.get(PreferenceSupplement, supplement_id)
    if supplement is None:
        raise HTTPException(404, "偏好补充不存在。")
    if supplement.revision != revision:
        raise HTTPException(409, "偏好补充已在其他窗口更新，请重新载入后再删除。")
    # The content-free extraction ledger remains, so old inputs cannot recreate this row.
    session.delete(supplement)
    session.commit()
    return {"deleted": supplement_id}


@router.put("/preferences", response_model=PreferenceProfileOut)
def save_preferences(
    body: PreferenceProfileIn, session: Session = Depends(get_session)
) -> PreferenceProfileOut:
    begin_memory_write(session)
    current = read_preferences(session)
    if body.revision != current.revision:
        raise HTTPException(409, "偏好已在其他窗口更新，请重新载入后再保存。")
    profile = session.get(PreferenceProfile, 1) or PreferenceProfile()
    profile.content = body.content.strip()
    profile.revision = uuid4().hex
    profile.updated_at = datetime.now(timezone.utc)
    session.add(profile)
    session.commit()
    return read_preferences(session)


def _fact_out(memory: Memory, title: str | None) -> FactOut:
    return FactOut(
        id=memory.id,
        kind=memory.kind,
        content=memory.content,
        tree_id=memory.tree_id,
        source_node_id=memory.source_node_id,
        tree_title=title,
        created_at=memory.created_at,
    )


@router.get("/facts", response_model=FactPageOut)
def list_facts(
    q: str = Query(default="", max_length=300),
    tree_id: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> FactPageOut:
    conditions = [Memory.kind == "fact"]
    if q.strip():
        # Treat percent/underscore literally, not as user-provided SQL wildcards.
        conditions.append(Memory.content.icontains(q.strip(), autoescape=True))
    if tree_id is not None:
        conditions.append(Memory.tree_id == tree_id)
    total = session.exec(select(func.count()).select_from(Memory).where(*conditions)).one()
    rows = session.exec(
        select(Memory, KnowledgeTree.title)
        .outerjoin(KnowledgeTree, Memory.tree_id == KnowledgeTree.id)
        .where(*conditions)
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    # Topic navigation stays stable while searching or paging through results.
    topics = session.exec(
        select(Memory.tree_id, KnowledgeTree.title, func.count(Memory.id))
        .outerjoin(KnowledgeTree, Memory.tree_id == KnowledgeTree.id)
        .where(Memory.kind == "fact")
        .group_by(Memory.tree_id, KnowledgeTree.title)
        .order_by(func.lower(KnowledgeTree.title), Memory.tree_id)
    ).all()
    return FactPageOut(
        items=[_fact_out(memory, title) for memory, title in rows],
        total=total,
        page=page,
        page_size=page_size,
        topics=[
            FactTopicOut(tree_id=topic_id, title=title or "", count=count)
            for topic_id, title, count in topics
        ],
    )


@router.post("/delete")
def delete_facts(body: FactDeleteIn, session: Session = Depends(get_session)) -> dict:
    begin_memory_write(session)
    ids = list(
        session.exec(
            select(Memory.id).where(Memory.id.in_(set(body.ids)), Memory.kind == "fact")
        ).all()
    )
    session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id.in_(ids)))
    session.execute(delete(Memory).where(Memory.id.in_(ids), Memory.kind == "fact"))
    session.commit()
    return {"deleted": len(ids)}


@router.patch("/{memory_id}", response_model=FactOut)
def edit_fact(memory_id: int, body: FactPatch, session: Session = Depends(get_session)) -> FactOut:
    content = body.content.strip()
    if not content:
        raise HTTPException(422, "记忆内容不能为空。")
    begin_memory_write(session)
    memory = session.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(404, "记忆不存在")
    if memory.kind != "fact":
        raise HTTPException(422, "请在偏好编辑框中修改用户偏好。")
    changed = session.execute(
        update(Memory)
        .where(
            Memory.id == memory_id,
            Memory.kind == "fact",
            Memory.content == body.expected_content,
        )
        .values(content=content)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "记忆已在其他窗口更新，请重新载入后再保存。")
    session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id))
    session.commit()
    session.refresh(memory)
    tree = session.get(KnowledgeTree, memory.tree_id) if memory.tree_id is not None else None
    return _fact_out(memory, tree.title if tree else None)


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, session: Session = Depends(get_session)) -> dict:
    begin_memory_write(session)
    m = session.get(Memory, memory_id)
    if not m:
        raise HTTPException(404, "记忆不存在")
    session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id))
    session.delete(m)
    session.commit()
    return {"deleted": memory_id}


@router.delete("")
def clear_memories(session: Session = Depends(get_session)) -> dict:
    begin_memory_write(session)
    session.execute(delete(MemoryEmbedding))
    n = session.exec(select(func.count()).select_from(Memory)).one()
    session.execute(delete(Memory))
    n += session.exec(select(func.count()).select_from(PreferenceSupplement)).one()
    session.execute(delete(PreferenceSupplement))
    # Keep the replay ledger and invalidate extractions started before clear-all.
    learning = session.get(PreferenceLearningState, 1) or PreferenceLearningState()
    learning.revision = uuid4().hex
    session.add(learning)
    # Retain an empty override, not a missing row: otherwise automatic extraction
    # and stale editors could revive preferences that the user explicitly cleared.
    profile = session.get(PreferenceProfile, 1) or PreferenceProfile()
    profile.content = ""
    profile.revision = uuid4().hex
    profile.reset_revision = uuid4().hex
    profile.updated_at = datetime.now(timezone.utc)
    session.add(profile)
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
