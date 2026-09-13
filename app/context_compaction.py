"""Source-grounded extractive summaries; originals remain the authority.

No generative model is called. Categories select complete source sentences,
including conditions/corrections in the middle of answers, rather than making
new factual claims. A bounded hash-keyed cache is disposable and process-local.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

from .context_budget import text_tokens
from .models import Message, Node

SUMMARY_VERSION = "extractive-v1"
_CACHE: OrderedDict[str, tuple] = OrderedDict()
_LOCK = threading.Lock()
_SENTENCES = re.compile(r".+?(?:[。！？]|(?<=[.!?])\s+|\n+|$)", re.DOTALL)
_CORRECTION = re.compile(
    r"纠正|更正|修正|此前.*错|应改为|实际上|\b(?:correction|instead|incorrect)\b", re.I
)
_CONDITION = re.compile(
    r"只有|仅当|除非|前提|条件|必须|不能|不要|不应|不等于|并非|禁止|但|\b(?:if|unless|must|never|not|except|only)\b",
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

    def render(self) -> str:
        source = f"node={self.node_id}, section={self.section}"
        if self.message_id is not None:
            source += f", message={self.message_id}"
        return (
            f"- [{self.category}；{self.role}；{source}, chars={self.start}:{self.end}] {self.text}"
        )


def _category(text: str, role: str) -> tuple[str, int]:
    if _CORRECTION.search(text):
        return "纠正记录", 50
    if _CONDITION.search(text):
        return "适用条件", 40
    if _UNCERTAIN.search(text):
        return "待解决疑问", 30
    if role == "用户笔记":
        return "用户理解（未经验证）", 25
    if role == "user":
        return "学习目标与问题", 20
    if _DEFINITION.search(text):
        return "概念定义", 15
    return "历史解释（未经验证）", 5


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
    key = source_fingerprint(node, messages)
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
                )
            )
    ranked = sorted(enumerate(candidates), key=lambda item: (-item[1].priority, item[0]))[:128]
    result = tuple(excerpt for _, excerpt in sorted(ranked))
    with _LOCK:
        _CACHE[key] = result
        _CACHE.move_to_end(key)
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return result


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms


def summarize_sources(
    sources: list[tuple[Node, list[Message]]], query: str, byte_budget: int
) -> str:
    """Select cited whole sentences under a budget, with explicit omission markers."""
    query_terms = _terms(query)
    ranked = []
    for order, (node, messages) in enumerate(sources):
        for excerpt in extract(node, messages):
            overlap = len(query_terms & _terms(excerpt.text))
            ranked.append((excerpt.priority + min(30, overlap * 5), order, excerpt))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen = []
    seen = set()
    used = 0
    for _, order, excerpt in ranked:
        if excerpt.text in seen:
            continue
        line = excerpt.render()
        cost = text_tokens(line) + 1
        if used + cost <= byte_budget:
            chosen.append((order, excerpt))
            seen.add(excerpt.text)
            used += cost
    chosen.sort(key=lambda item: (item[0], item[1].message_id or 0, item[1].start))
    return "\n".join(excerpt.render() for _, excerpt in chosen)
