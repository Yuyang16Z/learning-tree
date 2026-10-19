"""What a branch contributed, extracted locally for the card shown back at its source.

Returning from a branch to the passage it quoted, the learner sees a few
sentences from the branch's stored answers, picked by the categories context
compaction already uses (corrections, conditions, definitions, open questions)
together with their own note and understanding mark. Only the answers the
conversation shows count: the latest attempt of each question, when complete.
Nothing is generated or written: no model call, no database write, and the
excerpts are whole sentences of stored answers with Markdown markers removed
(long ones are shortened).
"""

from __future__ import annotations

import re

from sqlmodel import Session, select

from .context import context_messages
from .context_compaction import extract
from .models import Message, Node
from .schemas import UNDERSTANDING_LEVELS

MAX_EXCERPTS = 4
MAX_EXCERPT_CHARS = 180
MIN_EXCERPT_CHARS = 6
# First pass: at most this many sentences per answer, so a long branch shows more
# than its first reply; a second pass fills any remaining places in rank order.
PER_MESSAGE = 2
# context_compaction category labels -> stable keys, most useful first. Learning
# goals (the learner's own questions) are left out: the learner asked them.
CATEGORIES = {
    "纠正记录": "correction",
    "适用条件": "condition",
    "概念定义": "definition",
    "待解决疑问": "question",
    "历史解释（未经验证）": "explanation",
}
_RANK = {key: rank for rank, key in enumerate(CATEGORIES.values())}
_LEADING_MARKER = re.compile(r"^(?:#{1,6}\s+|>\s?|[-*+]\s+|\d{1,3}[.)]\s+)")
_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\s]*\)")
_EMPHASIS = re.compile(r"\*\*|__|`")


def branch_path(session: Session, node: Node) -> list[Node]:
    """Nodes from the nearest ancestor-or-self branch down to `node`; empty outside a branch.

    A branch node quotes a passage (seed_text) and has a source node to return to,
    matching the conversation view's "Back to source" target."""
    chain, seen, current = [node], {node.id}, node
    while not (current.seed_text and (current.source_node_id or current.parent_id) is not None):
        parent_id = current.parent_id
        if parent_id is None or parent_id in seen:
            return []
        parent = session.get(Node, parent_id)
        if parent is None or parent.tree_id != node.tree_id:
            return []
        seen.add(parent_id)
        chain.append(parent)
        current = parent
    return chain[::-1]


def plain_sentence(text: str) -> str:
    """Drop Markdown markers that read as noise outside a rendered answer."""
    text = text.strip()
    for _ in range(3):  # Nested markers such as "> - item".
        stripped = _LEADING_MARKER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _EMPHASIS.sub("", _LINK.sub(r"\1", text))
    return " ".join(text.split())


def _shorten(text: str) -> str:
    return text if len(text) <= MAX_EXCERPT_CHARS else text[: MAX_EXCERPT_CHARS - 1] + "…"


def _candidates(path: list[Node], messages: dict[int, list[Message]]) -> list[tuple]:
    # Like the conversation and the context builder, the latest answer attempt is the
    # answer even when incomplete; a complete attempt it superseded is not quoted.
    current = [(node, context_messages(messages.get(node.id, []))) for node in path]
    order = {
        message.id: index
        for index, message in enumerate(message for _, rows in current for message in rows)
    }
    found = []
    for node, rows in current:
        for excerpt in extract(node, rows):
            category = CATEGORIES.get(excerpt.category)
            if excerpt.section != "message" or category is None:
                continue  # Notes and the quoted passage are shown separately.
            if excerpt.role == "user" and category != "question":
                continue  # The learner's own words only when they flag an open question.
            raw = excerpt.text.strip()
            if raw.startswith(("#", "|", "```")) or "```" in raw:
                continue  # Headings, table rows and code do not read as sentences.
            text = plain_sentence(raw)
            if len(text) < MIN_EXCERPT_CHARS or text.endswith((":", "：")):
                continue  # Lead-ins such as "Note the following:" carry no content.
            # Most useful category first; within one, the latest answer first.
            key = (_RANK[category], -order.get(excerpt.message_id, 0), excerpt.start)
            found.append((key, category, _shorten(text), excerpt.node_id, excerpt.message_id))
    found.sort(key=lambda item: item[0])
    return found


def _pick(candidates: list[tuple]) -> list[dict]:
    chosen, seen, per_message = [], set(), {}
    for limited in (True, False):
        for key, category, text, node_id, message_id in candidates:
            if len(chosen) >= MAX_EXCERPTS:
                break
            if text in seen or (limited and per_message.get(message_id, 0) >= PER_MESSAGE):
                continue
            seen.add(text)
            per_message[message_id] = per_message.get(message_id, 0) + 1
            chosen.append((key, category, text, node_id, message_id))
    # Shown in rank order, whichever pass picked them.
    return [
        {"category": category, "text": text, "node_id": node_id, "message_id": message_id}
        for _, category, text, node_id, message_id in sorted(chosen, key=lambda item: item[0])
    ]


def build_return_summary(session: Session, node: Node) -> dict | None:
    """The card's content for leaving `node`, or None when `node` is not inside a branch."""
    path = branch_path(session, node)
    if not path:
        return None
    branch = path[0]
    rows = session.exec(
        select(Message).where(Message.node_id.in_([item.id for item in path])).order_by(Message.id)
    ).all()
    messages: dict[int, list[Message]] = {}
    for message in rows:
        messages.setdefault(message.node_id, []).append(message)
    # The latest judgement along the way back: the node being left first. Marks belong
    # to answers; listings call a node without messages "idle" and the map ignores a
    # mark the API or an import put there, so the recap skips it too.
    understanding = next(
        (
            item.understanding
            for item in reversed(path)
            if item.understanding in UNDERSTANDING_LEVELS
            and (item.status != "idle" or item.id in messages)
        ),
        None,
    )
    return {
        "branch_node_id": branch.id,
        "branch_title": branch.title,
        "understanding": understanding,
        "note": (branch.learning_note or "").strip() or None,
        "excerpts": _pick(_candidates(path, messages)),
        "source": {
            "node_id": branch.source_node_id or branch.parent_id,
            "message_id": branch.source_message_id,
            "start": branch.source_start,
            "end": branch.source_end,
            "text": branch.seed_text,
        },
    }
