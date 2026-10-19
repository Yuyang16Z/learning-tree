"""Source-grounded extractive summaries; originals remain the authority.

No generative model is called. Categories select complete source sentences,
including conditions/corrections in the middle of answers, rather than making
new factual claims. A bounded hash-keyed cache is disposable and process-local.

Selection order: corrections first, then every other sentence by its category's
base weight plus its relevance to the question (BM25 within the candidate set,
normalised to 0..1, see text_relevance). A first pass caps each category's share of
the excerpt budget so conditions cannot crowd out relevant explanations; a second
pass fills whatever budget is left in the same order. The result is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from .context_budget import text_tokens
from .models import Message, Node
from .text_relevance import relevance, tokens

SUMMARY_VERSION = "extractive-v2"
_CACHE: OrderedDict[tuple[int, str], tuple] = OrderedDict()
_DELETED_TREES: set[int] = set()
_LOCK = threading.Lock()
_SENTENCES = re.compile(r".+?(?:[。！？]|(?<=[.!?])\s+|\n+|$)", re.DOTALL)
_CORRECTION = re.compile(
    r"纠正|更正|修正|此前.*错|应改为|实际上|\b(?:correction|instead|incorrect)\b", re.I
)
_CONDITION = re.compile(
    r"只有|仅当|除非|前提|条件|必须|不能|不要|不应|不等于|并非|禁止|\b(?:if|unless|must|never|not|except|only)\b",
    re.I,
)
# A contrast alone ("但它很直观") is ordinary explanation. It marks a condition only
# together with a negation or a limiting word in the same sentence.
_CONTRAST = re.compile(r"但|不过|然而|可是|\b(?:but|however)\b", re.I)
_QUALIFIER = re.compile(
    r"不(?!过|错|仅|但|管|久|少|断)|没|无(?!论|穷)|非(?!常)|未|别|勿|仅|只|才|须|需要|限于|除了"
    r"|\b(?:no|cannot|can't|don't|doesn't|isn't|aren't|won't|unless|except|must|only|never|not)\b",
    re.I,
)
_UNCERTAIN = re.compile(
    r"还不明白|仍不理解|尚未解决|待确认|不确定|\b(?:unresolved|uncertain|not sure)\b", re.I
)
_DEFINITION = re.compile(r"是指|定义|意味着|指的是|\b(?:means|defined|refers to)\b", re.I)


@dataclass(frozen=True)
class Excerpt:
    node_id: int | None
    message_id: int | None
    section: str
    start: int
    end: int
    category: str
    role: str
    text: str
    priority: int
    # Lexical terms of the sentence, computed once with the cached extraction.
    terms: tuple[str, ...] = field(default=(), compare=False, repr=False)

    def render(self) -> str:
        source = f"node={self.node_id}, section={self.section}"
        if self.message_id is not None:
            source += f", message={self.message_id}"
        return (
            f"- [{self.category}；{self.role}；{source}, chars={self.start}:{self.end}] {self.text}"
        )


CORRECTION = "纠正记录"
CONDITION = "适用条件"
# Base weight per category; corrections additionally form a tier of their own.
CATEGORY_WEIGHT = {
    CORRECTION: 50,
    CONDITION: 40,
    "待解决疑问": 30,
    "用户理解（未经验证）": 25,
    "学习目标与问题": 20,
    "概念定义": 15,
    "历史解释（未经验证）": 5,
}
# Relevance adds up to this much: a highly relevant explanation (5 + 60) outranks an
# unrelated condition (40), while equally relevant sentences keep the category order.
RELEVANCE_WEIGHT = 60
# First-pass share of the excerpt budget per category; the second pass fills the rest.
CATEGORY_SHARE = {
    CORRECTION: 0.5,
    CONDITION: 0.4,
    "待解决疑问": 0.3,
    "用户理解（未经验证）": 0.3,
    "学习目标与问题": 0.3,
    "概念定义": 0.3,
    "历史解释（未经验证）": 0.5,
}
# Candidate sentences kept per source version, by base weight and then position. Large
# enough that relevance, not the category alone, decides what a long answer keeps.
MAX_CANDIDATES = 512


def _is_condition(text: str) -> bool:
    return bool(_CONDITION.search(text)) or bool(_CONTRAST.search(text) and _QUALIFIER.search(text))


def _category(text: str, role: str) -> tuple[str, int]:
    if _CORRECTION.search(text):
        label = CORRECTION
    elif _is_condition(text):
        label = CONDITION
    elif _UNCERTAIN.search(text):
        label = "待解决疑问"
    elif role == "用户笔记":
        label = "用户理解（未经验证）"
    elif role == "user":
        label = "学习目标与问题"
    elif _DEFINITION.search(text):
        label = "概念定义"
    else:
        label = "历史解释（未经验证）"
    return label, CATEGORY_WEIGHT[label]


def source_fingerprint(node: Node, messages: list[Message]) -> str:
    data = {
        "version": SUMMARY_VERSION,
        "node": [
            node.id,
            node.tree_id,
            node.title,
            node.status,
            node.learning_note,
            node.seed_text,
        ],
        "messages": [
            [
                m.id,
                m.role,
                m.status,
                m.content,
                [hashlib.sha256(i.encode()).hexdigest() for i in (m.images or [])],
            ]
            for m in messages
        ],
    }
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def extract(node: Node, messages: list[Message]) -> tuple[Excerpt, ...]:
    key = (node.tree_id, source_fingerprint(node, messages))
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    sources = [(m.id, "message", m.role, m.content) for m in messages if m.status == "complete"]
    if node.learning_note:
        sources.append((None, "note", "用户笔记", node.learning_note))
    if node.seed_text:
        sources.append((None, "quote", "引用原文", node.seed_text))
    candidates = []
    for message_id, section, role, content in sources:
        for match in _SENTENCES.finditer(content):
            raw = match.group()
            text = raw.strip()
            if not text or text_tokens(text) > 1000:
                continue  # Never cut through a long sentence and lose its qualification.
            start = match.start() + len(raw) - len(raw.lstrip())
            category, priority = _category(text, role)
            candidates.append(
                Excerpt(
                    node.id,
                    message_id,
                    section,
                    start,
                    start + len(text),
                    category,
                    role,
                    text,
                    priority,
                    tuple(tokens(text)),
                )
            )
    ranked = sorted(enumerate(candidates), key=lambda item: (-item[1].priority, item[0]))
    ranked = ranked[:MAX_CANDIDATES]
    result = tuple(excerpt for _, excerpt in sorted(ranked))
    with _LOCK:
        # A worker holding old snapshots can finish after deletion. Never recache them.
        if node.tree_id not in _DELETED_TREES:
            _CACHE[key] = result
            _CACHE.move_to_end(key)
            while len(_CACHE) > 64:
                _CACHE.popitem(last=False)
    return result


def forget_tree(tree_id: int) -> None:
    """Discard content caches, retaining only a content-free ID until process exit."""
    with _LOCK:
        _DELETED_TREES.add(tree_id)
        for key in list(_CACHE):
            if key[0] == tree_id:
                del _CACHE[key]


def summarize_sources(
    sources: list[tuple[Node, list[Message]]], query: str, byte_budget: int
) -> str:
    """Select cited whole sentences under a budget, with explicit omission markers."""
    candidates = [
        (order, excerpt)
        for order, (node, messages) in enumerate(sources)
        for excerpt in extract(node, messages)
    ]
    scores = relevance(tokens(query), [Counter(excerpt.terms) for _, excerpt in candidates])
    ranked = sorted(
        (
            (
                excerpt.category == CORRECTION,
                excerpt.priority + RELEVANCE_WEIGHT * score,
                order,
                excerpt,
            )
            for (order, excerpt), score in zip(candidates, scores, strict=True)
        ),
        key=lambda item: (item[0], item[1], item[2]),
        reverse=True,
    )
    chosen = []
    seen = set()
    used = 0
    spent: Counter = Counter()
    deferred = []
    for _, _, order, excerpt in ranked:
        if excerpt.text in seen:
            continue
        cost = text_tokens(excerpt.render()) + 1
        if used + cost > byte_budget:
            continue
        # A share caps how much a category accumulates; its best sentence always gets a
        # place, even when that one sentence is longer than the share itself.
        share = CATEGORY_SHARE[excerpt.category] * byte_budget
        if spent[excerpt.category] and spent[excerpt.category] + cost > share:
            deferred.append((order, excerpt, cost))
            continue
        chosen.append((order, excerpt))
        seen.add(excerpt.text)
        spent[excerpt.category] += cost
        used += cost
    # Every category has had its turn; leftover budget goes to deferred sentences in
    # the same order, so the shares prevent crowding out without wasting budget.
    for order, excerpt, cost in deferred:
        if excerpt.text not in seen and used + cost <= byte_budget:
            chosen.append((order, excerpt))
            seen.add(excerpt.text)
            used += cost
    chosen.sort(key=lambda item: (item[0], item[1].message_id or 0, item[1].start))
    return "\n".join(excerpt.render() for _, excerpt in chosen)
