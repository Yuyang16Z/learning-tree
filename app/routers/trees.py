"""Learning trees and portable, credential-free JSON backups."""

import base64
import binascii
import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete
from sqlmodel import Session, select

from ..context_compaction import forget_tree
from ..db import get_session
from ..documents import (
    MAX_FILE_BYTES,
    MEDIA_TYPES,
    DocumentAttachment,
    DocumentParseError,
    parse_document_limited,
    safe_document_name,
)
from ..learning_summaries import invalidate_summaries
from ..models import (
    KnowledgeTree,
    Memory,
    MemoryEmbedding,
    Message,
    Node,
    PreferenceExtraction,
    PreferenceSupplement,
)
from ..schemas import NodeOut, TreeIn, TreeOut, TreePatch
from ..service import get_messages
from ..tree_identity import record_node_id, record_tree_id
from .nodes import _CANCELLED_REQUESTS, _REQUEST_LOCK, discard_node_work, node_metadata

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
    tree = KnowledgeTree(
        id=record_tree_id(session, allocate=True), title=body.title.strip() or "新的学习"
    )
    session.add(tree)
    session.flush()
    root = Node(
        id=record_node_id(session, allocate=True),
        tree_id=tree.id,
        parent_id=None,
        title=body.root_question or tree.title,
        kind="root",
    )
    session.add(root)
    session.commit()
    return TreeOut(id=tree.id, title=tree.title, root_node_id=root.id)


@router.get("", response_model=list[TreeOut])
def list_trees(
    include_archived: bool = False, session: Session = Depends(get_session)
) -> list[TreeOut]:
    out = []
    query = select(KnowledgeTree).order_by(KnowledgeTree.id)
    if not include_archived:
        query = query.where(KnowledgeTree.archived.is_(False))
    for tree in session.exec(query):
        root = session.exec(
            select(Node).where(Node.tree_id == tree.id, Node.parent_id.is_(None)).order_by(Node.id)
        ).first()
        out.append(
            TreeOut(
                id=tree.id,
                title=tree.title,
                root_node_id=root.id if root else 0,
                archived=tree.archived,
            )
        )
    return out


@router.patch("/{tree_id}", response_model=TreeOut)
def update_tree(tree_id: int, body: TreePatch, session: Session = Depends(get_session)) -> TreeOut:
    tree = session.get(KnowledgeTree, tree_id)
    if not tree:
        raise HTTPException(404, "知识树不存在")
    # Sidebar metadata never rewrites root questions, branches, messages or memories.
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(tree, field, value)
    session.add(tree)
    session.commit()
    root = session.exec(
        select(Node).where(Node.tree_id == tree.id, Node.parent_id.is_(None)).order_by(Node.id)
    ).first()
    return TreeOut(
        id=tree.id,
        title=tree.title,
        root_node_id=root.id if root else 0,
        archived=tree.archived,
    )


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
    document_ids: list[str] = Field(default_factory=list, max_length=4)
    reasoning: str | None = Field(default=None, max_length=1000000)
    steps: list[dict] | None = None


class PortableDocument(Portable):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(max_length=14 * 1024 * 1024)
    sha256: str = Field(min_length=64, max_length=64)


class PortableBackup(Portable):
    format: Literal["branch-learning"]
    version: Literal[1, 2]
    tree: PortableTree
    nodes: list[PortableNode] = Field(min_length=1, max_length=2000)
    messages: list[PortableMessage] = Field(max_length=12000)
    documents: list[PortableDocument] = Field(default_factory=list, max_length=100)


@router.post("/import", response_model=TreeOut)
def import_tree(body: dict, session: Session = Depends(get_session)) -> TreeOut:
    if len(json.dumps(body, ensure_ascii=False).encode()) > 25 * 1024 * 1024:
        raise HTTPException(413, "备份超过 25 MB，请分成较小的主题")
    try:
        backup = PortableBackup.model_validate(body)
    except ValidationError:
        raise HTTPException(422, "备份格式无效：需要学习树版本 1 或 2 的 JSON") from None
    nodes = {node.id: node for node in backup.nodes}
    messages = {message.id: message for message in backup.messages}
    if len(nodes) != len(backup.nodes) or len(messages) != len(backup.messages):
        raise HTTPException(422, "备份包含重复 ID")
    roots = [node for node in backup.nodes if node.parent_id is None]
    if not roots:
        raise HTTPException(422, "备份缺少根节点")
    documents = {doc.id: doc for doc in backup.documents}
    if len(documents) != len(backup.documents):
        raise HTTPException(422, "备份包含重复文档 ID")
    if backup.version == 1 and (documents or any(m.document_ids for m in backup.messages)):
        raise HTTPException(422, "文档附件需要版本 2 的备份")
    for message in backup.messages:
        if message.node_id not in nodes:
            raise HTTPException(422, "消息引用了不存在的节点")
        if len(set(message.document_ids)) != len(message.document_ids) or any(
            identifier not in documents for identifier in message.document_ids
        ):
            raise HTTPException(422, "消息引用了不存在或重复的文档")
        if message.document_ids and message.role != "user":
            raise HTTPException(422, "文档只能关联用户问题")
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
    parsed_documents = []
    referenced_ids = {identifier for m in backup.messages for identifier in m.document_ids}
    if set(documents) != referenced_ids:
        raise HTTPException(422, "备份包含未关联问题的文档")
    for old in backup.documents:
        try:
            content = base64.b64decode(old.content_base64, validate=True)
            if len(content) > MAX_FILE_BYTES or hashlib.sha256(content).hexdigest() != old.sha256:
                raise ValueError()
            name = safe_document_name(old.name)
            sections, warnings = parse_document_limited(content, name)
        except DocumentParseError as exc:
            raise HTTPException(422, str(exc)) from None
        except (ValueError, binascii.Error):
            raise HTTPException(422, "备份文档内容或校验摘要无效") from None
        parsed_documents.append((old, name, content, sections, warnings))
    # All validation happens before this transaction. Imported IDs are remapped,
    # never overwritten; request IDs and model credentials are not portable.
    # Archive status is local organization, not portable learning content.
    # Imports always appear as active topics; keep versions 1 and 2 compatible.
    tree = KnowledgeTree(id=record_tree_id(session, allocate=True), title=backup.tree.title)
    session.add(tree)
    session.flush()
    document_map = {}
    for old, name, content, sections, warnings in parsed_documents:
        identifier = str(uuid4())
        document_map[old.id] = identifier
        session.add(
            DocumentAttachment(
                id=identifier,
                tree_id=tree.id,
                name=name,
                media_type=MEDIA_TYPES[PurePosixPath(name).suffix.lower()],
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content=content,
                sections=sections,
                warnings=warnings,
            )
        )
    node_map = {}
    for old in backup.nodes:
        values = old.model_dump(
            exclude={"id", "parent_id", "source_node_id", "source_message_id", "revision_of"}
        )
        if values["status"] == "pending":
            values.update(status="interrupted", error="导入了未完成的回答，可重试。")
        # Imported labels are explicit user data; do not trigger model calls or
        # overwrite them when opening the tree in a newer version.
        node = Node(
            id=record_node_id(session, allocate=True),
            tree_id=tree.id,
            title_state="manual" if values["title"] else "empty",
            **values,
        )
        session.add(node)
        session.flush()
        node_map[old.id] = node
    message_map = {}
    for old in sorted(backup.messages, key=lambda m: m.id):
        values = old.model_dump(exclude={"id", "node_id"})
        values["document_ids"] = [document_map[identifier] for identifier in old.document_ids]
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
    document_ids = {
        identifier for message in messages for identifier in (message.document_ids or [])
    }
    documents = [session.get(DocumentAttachment, identifier) for identifier in sorted(document_ids)]
    if any(doc is None or doc.tree_id != tree_id for doc in documents):
        raise HTTPException(409, "文档来源缺失，请先检查附件，未导出不完整备份")
    backup = {
        "format": "branch-learning",
        "version": 2 if documents else 1,
        "tree": {"title": tree.title},
        "nodes": [
            {
                **node.model_dump(exclude={"tree_id", "created_at", "request_id", "title_state"}),
                "kind": node_metadata(node)["kind"],
            }
            for node in nodes
        ],
        "messages": [
            {
                **message.model_dump(exclude={"created_at", "document_ids"}),
                **({"document_ids": message.document_ids or []} if documents else {}),
            }
            for message in messages
        ],
    }
    if documents:
        backup["documents"] = [
            {
                "id": doc.id,
                "name": doc.name,
                "content_base64": base64.b64encode(doc.content).decode("ascii"),
                "sha256": doc.sha256,
            }
            for doc in documents
        ]
    if len(json.dumps(backup, ensure_ascii=False).encode()) > 25 * 1024 * 1024:
        raise HTTPException(413, "备份含附件后超过 25 MB，请分成较小的主题")
    return backup


@router.delete("/{tree_id}")
def delete_tree(tree_id: int, session: Session = Depends(get_session)) -> dict:
    with _REQUEST_LOCK:
        tree = session.get(KnowledgeTree, tree_id)
        if not tree:
            raise HTTPException(404, "知识树不存在")
        record_tree_id(session)
        record_node_id(session)
        nodes = session.exec(select(Node).where(Node.tree_id == tree_id)).all()
        ids = [node.id for node in nodes]
        discard_node_work(ids)
        invalidate_summaries(session, tree_id=tree_id)
        session.execute(
            delete(PreferenceSupplement).where(
                (PreferenceSupplement.tree_id == tree_id)
                | (PreferenceSupplement.source_tree_id == tree_id)
                | PreferenceSupplement.source_node_id.in_(ids)
            )
        )
        session.execute(
            delete(PreferenceExtraction).where(
                (PreferenceExtraction.tree_id == tree_id) | PreferenceExtraction.node_id.in_(ids)
            )
        )
        for key in list(_CANCELLED_REQUESTS):
            if key[0] == tree_id:
                _CANCELLED_REQUESTS.pop(key, None)
        for message in session.exec(select(Message).where(Message.node_id.in_(ids))).all():
            session.delete(message)
        for memory in session.exec(
            select(Memory).where((Memory.tree_id == tree_id) | Memory.source_node_id.in_(ids))
        ).all():
            session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory.id))
            session.delete(memory)
        for node in nodes:
            session.delete(node)
        session.execute(delete(DocumentAttachment).where(DocumentAttachment.tree_id == tree_id))
        session.delete(tree)
        session.commit()
        forget_tree(tree_id)
    return {"deleted": tree_id}


@router.get("/{tree_id}", response_model=list[NodeOut])
def get_tree(tree_id: int, session: Session = Depends(get_session)) -> list[NodeOut]:
    if not session.get(KnowledgeTree, tree_id):
        raise HTTPException(404, "知识树不存在")
    nodes = session.exec(select(Node).where(Node.tree_id == tree_id).order_by(Node.id)).all()
    return [_node_out(node, session) for node in nodes]
