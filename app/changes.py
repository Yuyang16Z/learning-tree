"""Change notices that let other open windows and devices refresh what they show.

Committed ORM changes are recorded by scope and revision. Notices carry record
IDs only; clients reload through the normal API, so a notice never copies
conversation text, keys or tool configuration. State lives in this process: a
restart or a client that falls too far behind is told to reload everything."""

import threading
from collections import deque
from uuid import uuid4

from fastapi import APIRouter
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from .models import (
    KnowledgeTree,
    McpServer,
    Memory,
    Message,
    ModelConfig,
    Node,
    PreferenceLearningState,
    PreferenceProfile,
    PreferenceSupplement,
)

router = APIRouter(prefix="/changes", tags=["changes"])
_PENDING = "learning_tree_changes"
# Topic nodes and messages are reported per node; these tables by name.
_SCOPES = {
    KnowledgeTree: "topics",
    ModelConfig: "models",
    McpServer: "mcp",
    Memory: "memory",
    PreferenceProfile: "memory",
    PreferenceSupplement: "memory",
    PreferenceLearningState: "memory",
}


class ChangeFeed:
    def __init__(self, history: int = 512) -> None:
        self.instance = uuid4().hex[:12]
        self._lock = threading.Lock()
        self._revision = 0
        self._recent: deque[tuple[int, frozenset[tuple]]] = deque(maxlen=history)

    def publish(self, scopes) -> None:
        if not scopes:
            return
        with self._lock:
            self._revision += 1
            self._recent.append((self._revision, frozenset(scopes)))

    def since(self, cursor: str) -> dict:
        """Describe changes after `cursor`; an unknown or expired cursor requires a reload."""
        instance, _, value = cursor.partition(":")
        changed: set[tuple] = set()
        with self._lock:
            current = self._revision
            known = instance == self.instance and value.isdigit() and int(value) <= current
            if known and int(value) < current:
                # Every revision after the cursor must still be retained.
                known = bool(self._recent) and self._recent[0][0] <= int(value) + 1
                for revision, scopes in self._recent if known else ():
                    if revision > int(value):
                        changed |= scopes
        notice = {
            "cursor": f"{self.instance}:{current}",
            "reload": not known or ("reload",) in changed,
            "topics": False,
            "models": False,
            "mcp": False,
            "memory": False,
            "trees": {},
        }
        for scope in changed:
            if scope[0] == "node":
                notice["trees"].setdefault(str(scope[1]), []).append(scope[2])
            elif scope[0] != "reload":
                notice[scope[0]] = True
        for node_ids in notice["trees"].values():
            node_ids.sort()
        return notice


feed = ChangeFeed()


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _context) -> None:
    scopes = session.info.setdefault(_PENDING, set())
    message_nodes = set()
    dirty = (item for item in session.dirty if session.is_modified(item))
    for item in (*session.new, *session.deleted, *dirty):
        if isinstance(item, Node):
            scopes.add(("node", item.tree_id, item.id))
        elif isinstance(item, Message):
            message_nodes.add(item.node_id)
        elif type(item) in _SCOPES:
            scopes.add((_SCOPES[type(item)],))
    if message_nodes:
        # Nodes deleted in this flush are already reported above.
        rows = session.connection().execute(
            select(Node.id, Node.tree_id).where(Node.id.in_(message_nodes))
        )
        scopes.update(("node", tree_id, node_id) for node_id, tree_id in rows)


@event.listens_for(Session, "do_orm_execute")
def _collect_bulk(state) -> None:
    if not (state.is_update or state.is_delete) or state.bind_mapper is None:
        return
    entity = state.bind_mapper.class_
    # Bulk statements do not say which topic rows they touched.
    scope = ("reload",) if entity in (Node, Message) else (_SCOPES.get(entity),)
    if scope[0]:
        state.session.info.setdefault(_PENDING, set()).add(scope)


@event.listens_for(Session, "after_commit")
def _publish(session: Session) -> None:
    feed.publish(session.info.pop(_PENDING, None))


@event.listens_for(Session, "after_transaction_end")
def _discard(session: Session, transaction) -> None:
    # Commits have already published; rolled-back or abandoned work never does.
    if transaction.parent is None:
        session.info.pop(_PENDING, None)


@router.get("")
def changes(since: str = "") -> dict:
    return feed.since(since)
