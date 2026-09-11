"""Learning trees and portable, credential-free JSON backups."""

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, select

from ..db import get_session
from ..models import KnowledgeTree, Memory, Message, Node
from ..schemas import NodeOut, TreeIn, TreeOut
from ..service import get_messages
from .nodes import _GENERATIONS, _REQUEST_LOCK, node_metadata

router = APIRouter(prefix="/trees", tags=["trees"])


def _node_out(node: Node, session: Session) -> NodeOut:
    return NodeOut(
        id=node.id,
        tree_id=node.tree_id,
        parent_id=node.parent_id,
        title=node.title,
        seed_text=node.seed_text,
        has_summary=node.summary is not None,
        **node_metadata(node, get_messages(session, node.id)),
    )


@router.post("", response_model=TreeOut)
def create_tree(body: TreeIn, session: Session = Depends(get_session)) -> TreeOut:
    tree = KnowledgeTree(title=body.title.strip() or "新的学习")
    session.add(tree)
    session.flush()
    root = Node(
        tree_id=tree.id, parent_id=None, title=body.root_question or tree.title, kind="root"
    )
    session.add(root)
    session.commit()
    return TreeOut(id=tree.id, title=tree.title, root_node_id=root.id)


@router.get("", response_model=list[TreeOut])
def list_trees(session: Session = Depends(get_session)) -> list[TreeOut]:
    out = []
    for tree in session.exec(select(KnowledgeTree).order_by(KnowledgeTree.id)):
        root = session.exec(
            select(Node).where(Node.tree_id == tree.id, Node.parent_id.is_(None)).order_by(Node.id)
        ).first()
        out.append(TreeOut(id=tree.id, title=tree.title, root_node_id=root.id if root else 0))
    return out


class Portable(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortableTree(Portable):
    title: str = Field(min_length=1, max_length=300)


class PortableNode(Portable):
    id: int = Field(gt=0)
    parent_id: int | None = None
    title: str = Field(max_length=100000)
    seed_text: str | None = Field(default=None, max_length=100000)
    summary: str | None = Field(default=None, max_length=100000)
    kind: Literal["root", "followup", "branch", "revision"] = "followup"
    status: Literal["idle", "pending", "complete", "error", "interrupted"] = "idle"
    error: str | None = Field(default=None, max_length=10000)
    source_node_id: int | None = None
    source_message_id: int | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    learning_note: str | None = Field(default=None, max_length=10000)
    revision_of: int | None = None


class PortableMessage(Portable):
    id: int = Field(gt=0)
    node_id: int
    role: Literal["user", "assistant"]
    content: str = Field(max_length=1000000)
    answered_by: str | None = Field(default=None, max_length=200)
    status: Literal["pending", "complete", "error", "interrupted"] = "complete"
    images: list[str] | None = None
    reasoning: str | None = Field(default=None, max_length=1000000)
    steps: list[dict] | None = None


class PortableBackup(Portable):
    format: Literal["branch-learning"]
    version: Literal[1]
    tree: PortableTree
    nodes: list[PortableNode] = Field(min_length=1, max_length=2000)
    messages: list[PortableMessage] = Field(max_length=12000)


@router.post("/import", response_model=TreeOut)
def import_tree(body: dict, session: Session = Depends(get_session)) -> TreeOut:
    if len(json.dumps(body, ensure_ascii=False).encode()) > 25 * 1024 * 1024:
        raise HTTPException(413, "备份超过 25 MB，请分成较小的主题")
    try:
        backup = PortableBackup.model_validate(body)
    except ValidationError:
        raise HTTPException(422, "备份格式无效：需要学习树版本 1 的学习树 JSON") from None
    nodes = {node.id: node for node in backup.nodes}
    messages = {message.id: message for message in backup.messages}
    if len(nodes) != len(backup.nodes) or len(messages) != len(backup.messages):
        raise HTTPException(422, "备份包含重复 ID")
    roots = [node for node in backup.nodes if node.parent_id is None]
    if not roots:
        raise HTTPException(422, "备份缺少根节点")
    for message in backup.messages:
        if message.node_id not in nodes:
            raise HTTPException(422, "消息引用了不存在的节点")
        if message.images and (
            len(message.images) > 8 or any(not x.startswith("data:image/") for x in message.images)
        ):
            raise HTTPException(422, "备份图片必须是内嵌图片，每条消息最多 8 张")
    for node in backup.nodes:
        seen = {node.id}
        parent = node.parent_id
        while parent is not None:
            if parent not in nodes or parent in seen:
                raise HTTPException(422, "备份的父子关系不存在或形成循环")
            seen.add(parent)
            parent = nodes[parent].parent_id
        for ref in (node.source_node_id, node.revision_of):
            if ref is not None and ref not in nodes:
                raise HTTPException(422, "备份引用了不存在的来源节点")
        if node.source_message_id is not None:
            source = messages.get(node.source_message_id)
            if not source or source.node_id != node.source_node_id or source.role != "assistant":
                raise HTTPException(422, "备份的来源消息不匹配")
        if (node.source_start is None) != (node.source_end is None) or (
            node.source_start is not None and node.source_end <= node.source_start
        ):
            raise HTTPException(422, "备份的原文位置无效")
    # All validation happens before this transaction. Imported IDs are remapped,
    # never overwritten; request IDs and model credentials are not portable.
    tree = KnowledgeTree(title=backup.tree.title)
    session.add(tree)
    session.flush()
    node_map = {}
    for old in backup.nodes:
        values = old.model_dump(
            exclude={"id", "parent_id", "source_node_id", "source_message_id", "revision_of"}
        )
        if values["status"] == "pending":
            values.update(status="interrupted", error="导入了未完成的回答，可重试。")
        node = Node(tree_id=tree.id, **values)
        session.add(node)
        session.flush()
        node_map[old.id] = node
    message_map = {}
    for old in sorted(backup.messages, key=lambda m: m.id):
        values = old.model_dump(exclude={"id", "node_id"})
        if values["status"] == "pending":
            values["status"] = "interrupted"
        message = Message(node_id=node_map[old.node_id].id, **values)
        session.add(message)
        session.flush()
        message_map[old.id] = message.id
    for old in backup.nodes:
        node = node_map[old.id]
        node.parent_id = node_map[old.parent_id].id if old.parent_id else None
        node.source_node_id = node_map[old.source_node_id].id if old.source_node_id else None
        node.source_message_id = message_map.get(old.source_message_id)
        node.revision_of = node_map[old.revision_of].id if old.revision_of else None
        session.add(node)
    session.commit()
    return TreeOut(id=tree.id, title=tree.title, root_node_id=node_map[roots[0].id].id)


@router.get("/{tree_id}/export")
def export_tree(tree_id: int, session: Session = Depends(get_session)) -> dict:
    tree = session.get(KnowledgeTree, tree_id)
    if not tree:
        raise HTTPException(404, "知识树不存在")
    nodes = session.exec(select(Node).where(Node.tree_id == tree_id).order_by(Node.id)).all()
    node_ids = [node.id for node in nodes]
    messages = session.exec(
        select(Message).where(Message.node_id.in_(node_ids)).order_by(Message.id)
    ).all()
    return {
        "format": "branch-learning",
        "version": 1,
        "tree": {"title": tree.title},
        "nodes": [
            {
                **node.model_dump(exclude={"tree_id", "created_at", "request_id"}),
                "kind": node_metadata(node)["kind"],
            }
            for node in nodes
        ],
        "messages": [message.model_dump(exclude={"created_at"}) for message in messages],
    }


@router.delete("/{tree_id}")
def delete_tree(tree_id: int, session: Session = Depends(get_session)) -> dict:
    tree = session.get(KnowledgeTree, tree_id)
    if not tree:
        raise HTTPException(404, "知识树不存在")
    nodes = session.exec(select(Node).where(Node.tree_id == tree_id)).all()
    ids = [node.id for node in nodes]
    with _REQUEST_LOCK:
        for nid in ids:
            if nid in _GENERATIONS:
                _GENERATIONS[nid].stop.set()
        for message in session.exec(select(Message).where(Message.node_id.in_(ids))).all():
            session.delete(message)
        for memory in session.exec(
            select(Memory).where((Memory.tree_id == tree_id) | Memory.source_node_id.in_(ids))
        ).all():
            session.delete(memory)
        for node in nodes:
            session.delete(node)
        session.delete(tree)
        session.commit()
    return {"deleted": tree_id}


@router.get("/{tree_id}", response_model=list[NodeOut])
def get_tree(tree_id: int, session: Session = Depends(get_session)) -> list[NodeOut]:
    if not session.get(KnowledgeTree, tree_id):
        raise HTTPException(404, "知识树不存在")
    nodes = session.exec(select(Node).where(Node.tree_id == tree_id).order_by(Node.id)).all()
    return [_node_out(node, session) for node in nodes]
