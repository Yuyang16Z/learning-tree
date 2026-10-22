"""Persistent, active-path summaries with bounded model work and local fallback.

Blocks stop at node boundaries so extending a path leaves earlier blocks intact.
The original records are authoritative, including when a slow summary finishes
after a source was edited or deleted. Provider calls never hold a mutation lock.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter, OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import semantic_models
from .context_budget import ContextPolicy, text_tokens
from .context_compaction import summarize_sources
from .models import ContextSummary, KnowledgeTree, Message, Node
from .summary_generation import (
    SUMMARY_PROMPT_VERSION,
    fits_input_budget,
    summarize_learning_context,
)
from .text_relevance import relevance, tokens

BLOCK_TARGET = 8000
BLOCK_MAX = 24000
MIN_INPUT_BYTES = 1200
MAX_OUTPUT_BYTES = 1600
FAILURE_COOLDOWN = 60.0
_LOCK = threading.RLock()
_INFLIGHT: set[tuple[int, str]] = set()
_FAILURES: OrderedDict[tuple[int, str], float] = OrderedDict()
_EPOCHS: dict[tuple[int, int, int | None], int] = {}
# Block vectors from the local embedding model, keyed by model and content hash.
_SEMANTIC_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_SEMANTIC_CACHE_SIZE = 256
_SEMANTIC_LOCK = threading.Lock()
# Reciprocal-rank fusion constant, as in memory recall.
_RRF_K = 60


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


# Learner marks are not source content. Leaving them out keeps summary keys stable,
# so marking a node never forces a new (billable) summary or rejects one in flight.
_NODE_MARKS = frozenset({"understanding"})


def _snapshot(record) -> dict:
    if isinstance(record, Node):
        return record.model_dump(mode="json", exclude=set(_NODE_MARKS))
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


def _render(
    sources, blocks, summaries, query: str, budget: int, relevance_by_key=None
) -> str | None:
    chosen = []
    covered = set()
    used = 0
    if relevance_by_key is None:
        relevance_by_key = _block_relevance(blocks, query, semantic=False)
    ranked = sorted(
        enumerate(blocks),
        key=lambda item: (relevance_by_key[item[1].key], item[0]),
        reverse=True,
    )
    # Leave room for original sentences from blocks that have no valid summary.
    # A cached summary must never crowd all of that evidence out.
    has_uncached = any(block.key not in summaries for block in blocks)
    excerpt_reserve = min(budget // 3, 1200) if has_uncached else 0
    for index, block in ranked:
        content = summaries.get(block.key)
        if not content:
            continue
        text = block.heading + content
        cost = text_tokens(text) + (2 if chosen else 0)
        if used + cost > budget - excerpt_reserve:
            continue
        chosen.append((index, text))
        covered.update(record["source_id"] for record in block.records)
        used += cost
    if not chosen:
        return None
    chosen = [text for _, text in sorted(chosen)]
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


def _block_text(block: _Block) -> str:
    return "\n".join(record["text"] for record in block.records)


def _unit(vector) -> list[float]:
    values = [float(value) for value in vector]
    length = math.sqrt(sum(value * value for value in values))
    if not values or not math.isfinite(length) or length <= 0:
        raise ValueError("invalid embedding")
    return [value / length for value in values]


def _semantic_scores(query: str, texts: list[str]) -> list[float] | None:
    """Cosine similarity of each block to the question with the local E5 model, or None.

    Used only when the locally cached model is already loaded: inference never
    downloads, and any failure leaves block ranking lexical. Block vectors are cached
    by model and content hash, so a later question only embeds the question itself.
    Sentence scoring never calls this.
    """
    if len(texts) < 2 or not query.strip():
        return None
    try:
        backend = semantic_models.get_backend()
        question = _unit(backend.embed_query(query[:2000]))
        model = str(backend.model_key)
        keys = [(model, hashlib.sha256(text.encode("utf-8")).hexdigest()) for text in texts]
        vectors: dict[tuple[str, str], list[float]] = {}
        with _SEMANTIC_LOCK:
            for key in keys:
                if key in _SEMANTIC_CACHE:
                    _SEMANTIC_CACHE.move_to_end(key)
                    vectors[key] = _SEMANTIC_CACHE[key]
        missing = list(dict.fromkeys(key for key in keys if key not in vectors))
        if missing:
            by_key = dict(zip(keys, texts, strict=True))
            computed = list(backend.embed_documents([by_key[key] for key in missing]))
            if len(computed) != len(missing):
                return None
            with _SEMANTIC_LOCK:
                for key, vector in zip(missing, computed, strict=True):
                    vectors[key] = _SEMANTIC_CACHE[key] = _unit(vector)
                    _SEMANTIC_CACHE.move_to_end(key)
                while len(_SEMANTIC_CACHE) > _SEMANTIC_CACHE_SIZE:
                    _SEMANTIC_CACHE.popitem(last=False)
        if any(len(vectors[key]) != len(question) for key in keys):
            return None
        return [sum(a * b for a, b in zip(question, vectors[key], strict=True)) for key in keys]
    except Exception:
        return None  # The optional model is never required for ranking.


def _dense_ranks(values: list[float]) -> list[int]:
    """1 for the highest value; equal values share a rank."""
    order = {value: rank for rank, value in enumerate(sorted(set(values), reverse=True), 1)}
    return [order[value] for value in values]


def _block_relevance(blocks: list[_Block], query: str, *, semantic: bool = True) -> dict:
    """Relevance of each immutable source block to the question; cached summaries stay
    query-independent. Lexical BM25 within the blocks, fused by rank with semantic
    similarity when the local embedding model is available."""
    texts = [_block_text(block) for block in blocks]
    lexical = relevance(tokens(query), [Counter(tokens(text)) for text in texts])
    similarity = _semantic_scores(query, texts) if semantic else None
    if similarity is None:
        return {block.key: score for block, score in zip(blocks, lexical, strict=True)}
    lexical_rank, semantic_rank = _dense_ranks(lexical), _dense_ranks(similarity)
    return {
        block.key: 1 / (_RRF_K + lexical_rank[index]) + 1 / (_RRF_K + semantic_rank[index])
        for index, block in enumerate(blocks)
    }


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
    # Block partition and eligibility are UTF-8 byte limits. They come from the
    # uncalibrated budget, read as one byte per estimate unit: conservative, since
    # ordinary text costs at most about 0.6 unit per byte, and stable, so a learned
    # calibration ratio never re-keys cached blocks or switches summaries off. The
    # calibrated budget gates each summary request itself (fits_input_budget).
    hard_limit = min(BLOCK_MAX, max(0, ContextPolicy.from_spec(spec).raw_input_budget - 4000))
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
    # Lexical (and, when loaded, semantic) block relevance; computed outside every lock.
    relevance_by_key = _block_relevance(blocks, query)
    # Score and size-check outside the shared claim lock; only the claim is atomic.
    # A block whose request the calibrated budget would refuse never takes this
    # question's one model call, nor starts a failure cooldown.
    ranked_missing = sorted(
        (
            (index, block)
            for index, block in enumerate(blocks)
            if block.key not in summaries
            and MIN_INPUT_BYTES <= block.input_bytes <= hard_limit
            and budget >= block.output_limit + text_tokens(block.heading)
            and fits_input_budget(spec, block.records, block.output_limit)
        ),
        key=lambda item: (-relevance_by_key[item[1].key], item[0]),
    )
    with _LOCK:
        # Spend the one optional model call on the old block most relevant to
        # this question. Equal-scoring blocks keep chronological cache fill.
        for _, block in ranked_missing:
            state_key = (namespace, block.key)
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
    return _render(sources, blocks, summaries, query, budget, relevance_by_key)
