"""Persistent, active-path summaries with bounded model work and local fallback.

Blocks stop at node boundaries so extending a path leaves earlier blocks intact.
The original records are authoritative, including when a slow summary finishes
after a source was edited or deleted. Provider calls never hold a mutation lock.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .context_budget import ContextPolicy, text_tokens
from .context_compaction import summarize_sources
from .models import ContextSummary, KnowledgeTree, Message, Node
from .summary_generation import SUMMARY_PROMPT_VERSION, summarize_learning_context

BLOCK_TARGET = 8000
BLOCK_MAX = 24000
MIN_INPUT_BYTES = 1200
MAX_OUTPUT_BYTES = 1600
FAILURE_COOLDOWN = 60.0
_LOCK = threading.RLock()
_INFLIGHT: set[tuple[int, str]] = set()
_FAILURES: OrderedDict[tuple[int, str], float] = OrderedDict()
_EPOCHS: dict[tuple[int, int, int | None], int] = {}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _snapshot(record) -> dict:
    return record.model_dump(mode="json")


def _namespace(bind) -> int:
    return id(getattr(bind, "engine", bind))


def _epoch(namespace: int, tree_id: int, node_id: int) -> tuple[int, int]:
    return (
        _EPOCHS.get((namespace, tree_id, None), 0),
        _EPOCHS.get((namespace, tree_id, node_id), 0),
    )


def _stopped(stop) -> bool:
    return bool(stop and (stop.is_set() if hasattr(stop, "is_set") else stop()))


@dataclass
class _Block:
    node: Node
    messages: list[Message]
    records: list[dict]
    fingerprint: str
    key: str
    input_bytes: int

    @property
    def output_limit(self) -> int:
        return min(MAX_OUTPUT_BYTES, max(384, self.input_bytes // 3))

    @property
    def heading(self) -> str:
        return f"模型摘要（node={self.node.id}；未经核实，请按引用回查原文）：\n"


def _blocks(sources, model_fingerprint: str, hard_limit: int) -> list[_Block]:
    blocks = []
    target = min(BLOCK_TARGET, hard_limit)
    for node, messages in sources:
        records = [
            ({"source_id": f"node={node.id},message={m.id}", "role": m.role, "text": m.content}, m)
            for m in messages
            if m.content
        ]
        if node.learning_note:
            records.append(
                (
                    {
                        "source_id": f"node={node.id},section=note",
                        "role": "用户笔记",
                        "text": node.learning_note,
                    },
                    None,
                )
            )
        if node.seed_text:
            records.append(
                (
                    {
                        "source_id": f"node={node.id},section=quote",
                        "role": "引用原文",
                        "text": node.seed_text,
                    },
                    None,
                )
            )
        group = []

        def flush():
            if not group:
                return
            payload = [record for record, _ in group]
            selected = [message for _, message in group if message is not None]
            fingerprint = _hash(
                {
                    "node": _snapshot(node),
                    "messages": [_snapshot(m) for m in selected],
                    "records": payload,
                }
            )
            key = _hash([node.tree_id, fingerprint, model_fingerprint, SUMMARY_PROMPT_VERSION])
            blocks.append(
                _Block(node, selected, payload, fingerprint, key, text_tokens(_json(payload)))
            )
            group.clear()

        for record in records:
            # A long single message stays whole. It may exceed the target but
            # must still fit the hard model allowance before a call is attempted.
            if group and text_tokens(_json([r for r, _ in group] + [record[0]])) > target:
                flush()
            group.append(record)
            if text_tokens(_json([r for r, _ in group])) >= target:
                flush()
        flush()
    return blocks


def _valid_sources(session: Session, sources) -> bool:
    if not sources or not session.get(KnowledgeTree, sources[0][0].tree_id):
        return False
    previous_id = None
    for expected, messages in sources:
        node = session.get(Node, expected.id)
        if node is None or _snapshot(node) != _snapshot(expected):
            return False
        if previous_id is not None and node.id != previous_id:
            parent_id, seen = node.parent_id, {node.id}
            while parent_id is not None and parent_id != previous_id:
                if parent_id in seen:
                    return False
                seen.add(parent_id)
                parent = session.get(Node, parent_id)
                if parent is None or parent.tree_id != node.tree_id:
                    return False
                parent_id = parent.parent_id
            if parent_id != previous_id:
                return False  # Supplied nodes must follow one path, not siblings.
        previous_id = node.id
        actual = session.exec(
            select(Message).where(Message.node_id == node.id).order_by(Message.id)
        ).all()
        active = []
        for message in actual:
            if message.role == "assistant" and active and active[-1].role == "assistant":
                active[-1] = message
            else:
                active.append(message)
        live = {message.id: message for message in active if message.status == "complete"}
        for message in messages:
            if message.id not in live or _snapshot(live[message.id]) != _snapshot(message):
                return False
    return True


def invalidate_summaries(session: Session, *, tree_id: int | None = None, node_ids=None) -> None:
    """Delete disposable summaries inside the caller's source-mutation transaction."""
    if tree_id is None and node_ids is None:
        return  # Require an explicit scope; never accidentally clear other topics.
    ids = None if node_ids is None else set(node_ids)
    if ids == set():
        return
    namespace = _namespace(session.get_bind())
    query = select(ContextSummary)
    if tree_id is not None:
        query = query.where(ContextSummary.tree_id == tree_id)
    with _LOCK:
        rows = session.exec(query).all()
        trees = {tree_id} if tree_id is not None else {row.tree_id for row in rows}
        if ids is not None:
            trees.update(session.exec(select(Node.tree_id).where(Node.id.in_(ids))).all())
        # Also invalidate pending work whose result is not yet in the table.
        if ids is not None:
            for ns, known_tree, known_node in list(_EPOCHS):
                if ns == namespace and known_node in ids:
                    trees.add(known_tree)
        for tid in trees:
            for nid in ids if ids is not None else (None,):
                token = (namespace, tid, nid)
                _EPOCHS[token] = _EPOCHS.get(token, 0) + 1
        for row in rows:
            if ids is None or ids.intersection(row.source_node_ids):
                session.delete(row)


def _render(sources, blocks, summaries, query: str, budget: int) -> str | None:
    chosen = []
    covered = set()
    used = 0
    # Favor later path context when all cached summaries do not fit together.
    for block in reversed(blocks):
        content = summaries.get(block.key)
        if not content:
            continue
        text = block.heading + content
        cost = text_tokens(text) + (2 if chosen else 0)
        if used + cost > budget:
            continue
        chosen.insert(0, text)
        covered.update(record["source_id"] for record in block.records)
        used += cost
    if not chosen:
        return None
    remaining = []
    for node, messages in sources:
        local_node = Node(**node.model_dump())
        if f"node={node.id},section=note" in covered:
            local_node.learning_note = None
        if f"node={node.id},section=quote" in covered:
            local_node.seed_text = None
        local_messages = [m for m in messages if f"node={node.id},message={m.id}" not in covered]
        if local_messages or local_node.learning_note or local_node.seed_text:
            remaining.append((local_node, local_messages))
    heading = "\n\n原文摘录（其余历史可能省略，请回查来源）：\n"
    room = budget - used - text_tokens(heading)
    if remaining and room > 0:
        excerpts = summarize_sources(remaining, query, room)
        if excerpts:
            chosen.append(heading[2:] + excerpts)
    result = "\n\n".join(chosen)
    return result if text_tokens(result) <= budget else None


def summarize_history(
    engine, spec, sources, query, budget, *, stop=None, mutation_lock=None
) -> str | None:
    """Reuse exact current-path blocks; generate at most one new model summary.

    None asks the context builder to use its ordinary local-only compaction.
    Keys contain hashes of provider identity and exact source versions, never keys.
    """
    if budget < 512 or not sources or _stopped(stop):
        return None
    if spec.api_key == "mock" or spec.base_url.startswith("mock"):
        return None
    sources = [
        (Node(**node.model_dump()), [Message(**m.model_dump()) for m in messages])
        for node, messages in sources
    ]
    tree_id = sources[0][0].tree_id
    if any(node.tree_id != tree_id or node.id is None for node, _ in sources):
        return None
    if any(
        m.id is None
        or m.node_id != node.id
        or m.status != "complete"
        or m.role not in {"user", "assistant"}
        for node, ms in sources
        for m in ms
    ):
        return None
    model_fingerprint = _hash([spec.protocol, spec.base_url, spec.llm_model])
    hard_limit = min(BLOCK_MAX, max(0, ContextPolicy.from_spec(spec).input_budget - 4000))
    blocks = _blocks(sources, model_fingerprint, hard_limit)
    namespace = _namespace(engine)
    summaries = {}
    guard = mutation_lock if mutation_lock is not None else nullcontext()
    with guard, Session(engine) as session:
        if _stopped(stop) or not _valid_sources(session, sources):
            return None
        for block in blocks:
            row = session.get(ContextSummary, block.key)
            if row:
                summaries[block.key] = row.content
    candidate = None
    with _LOCK:
        for block in blocks:
            state_key = (namespace, block.key)
            if block.key in summaries or not MIN_INPUT_BYTES <= block.input_bytes <= hard_limit:
                continue
            if budget < block.output_limit + text_tokens(block.heading):
                continue
            if state_key in _INFLIGHT or time.monotonic() < _FAILURES.get(state_key, 0):
                continue
            _INFLIGHT.add(state_key)
            _EPOCHS.setdefault((namespace, tree_id, block.node.id), 0)
            epoch = _epoch(namespace, tree_id, block.node.id)
            candidate = block
            break
    if candidate is not None:
        state_key = (namespace, candidate.key)
        failed = False
        try:
            # A previous caller can commit after our initial cache read and
            # release its claim before ours. Re-read after claiming, before
            # spending another provider call on that already completed block.
            with guard, Session(engine) as session:
                with _LOCK:
                    unchanged = _epoch(namespace, tree_id, candidate.node.id) == epoch
                if not unchanged or _stopped(stop) or not _valid_sources(session, sources):
                    return None
                cached = session.get(ContextSummary, candidate.key)
                if cached is not None:
                    summaries[candidate.key] = cached.content
            if candidate.key not in summaries and not _stopped(stop):
                content = summarize_learning_context(
                    spec, candidate.records, candidate.output_limit
                )
                if (
                    not isinstance(content, str)
                    or not content.strip()
                    or text_tokens(content) > candidate.output_limit
                ):
                    failed = True
                elif not _stopped(stop):
                    with guard, Session(engine) as session:
                        with _LOCK:
                            unchanged = _epoch(namespace, tree_id, candidate.node.id) == epoch
                        if unchanged and not _stopped(stop) and _valid_sources(session, sources):
                            if session.get(ContextSummary, candidate.key) is None:
                                session.add(
                                    ContextSummary(
                                        key=candidate.key,
                                        tree_id=tree_id,
                                        source_node_ids=[candidate.node.id],
                                        source_message_ids=[m.id for m in candidate.messages],
                                        fingerprint=candidate.fingerprint,
                                        model_fingerprint=model_fingerprint,
                                        prompt_version=SUMMARY_PROMPT_VERSION,
                                        content=content,
                                    )
                                )
                                try:
                                    session.commit()
                                except IntegrityError:
                                    session.rollback()  # Another process may have cached the same version.
                            summaries[candidate.key] = content
        except Exception:
            failed = True  # Optional compaction never prevents the original question.
        finally:
            with _LOCK:
                _INFLIGHT.discard(state_key)
                if failed:
                    _FAILURES[state_key] = time.monotonic() + FAILURE_COOLDOWN
                    _FAILURES.move_to_end(state_key)
                    while len(_FAILURES) > 256:
                        _FAILURES.popitem(last=False)
    with guard, Session(engine) as session:
        if _stopped(stop) or not _valid_sources(session, sources):
            return None
    return _render(sources, blocks, summaries, query, budget)
