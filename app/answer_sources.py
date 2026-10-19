"""Memory provenance stored with answers (Message.sources): ids at rest, titles on read.

An answer records which memory records reached its prompt as
`{memory_id, source_node_id, tree_id, scope}`. The provenance is derived data: it is
shown with topic titles read from the live topic, entries whose memory, source node or
topic no longer exists are dropped when read, and every deletion path removes the
entries that point at what it deleted. Titles are never stored, so provenance cannot
keep a deleted topic's title. Text an answer or its tool steps already quoted (such as a
history-search result naming another topic) is the answer's own record and is not
rewritten here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from sqlalchemy import text, update
from sqlmodel import Session, select

from .models import KnowledgeTree, Memory, Message, Node

SCOPES = ("path", "tree", "other")


def _title(value: str | None) -> str:
    value = value or ""
    return value if len(value) <= 60 else value[:60] + "…"


def _entries(value) -> list[dict]:
    """Well-formed entries reduced to their ids and scope; anything else is ignored."""
    if not isinstance(value, list):
        return []
    entries = []
    for item in value:
        if not isinstance(item, dict):
            continue
        memory_id, node_id, tree_id = (
            item.get(key) for key in ("memory_id", "source_node_id", "tree_id")
        )
        if (
            type(memory_id) is int
            and type(tree_id) is int
            and (node_id is None or type(node_id) is int)
            and item.get("scope") in SCOPES
        ):
            entries.append(
                {
                    "memory_id": memory_id,
                    "source_node_id": node_id,
                    "tree_id": tree_id,
                    "scope": item["scope"],
                }
            )
    return entries


def resolve_sources(session: Session, stored: Sequence) -> list[list[dict] | None]:
    """The current provenance of several stored lists, with three queries in total.

    An entry survives only while its memory still comes from the same source node and
    topic (memory ids can be reused), its source node is still in that topic and the
    topic exists; `tree_title` is the topic's current title."""
    lists = [_entries(value) for value in stored]
    entries = [item for items in lists for item in items]
    if not entries:
        return [None for _ in lists]
    memory_ids = {item["memory_id"] for item in entries}
    node_ids = {item["source_node_id"] for item in entries if item["source_node_id"] is not None}
    tree_ids = {item["tree_id"] for item in entries}
    memories = {
        memory_id: (node_id, tree_id)
        for memory_id, node_id, tree_id in session.exec(
            select(Memory.id, Memory.source_node_id, Memory.tree_id).where(
                Memory.id.in_(memory_ids)
            )
        )
    }
    nodes = (
        dict(session.exec(select(Node.id, Node.tree_id).where(Node.id.in_(node_ids))).all())
        if node_ids
        else {}
    )
    titles = dict(
        session.exec(
            select(KnowledgeTree.id, KnowledgeTree.title).where(KnowledgeTree.id.in_(tree_ids))
        ).all()
    )

    def live(item: dict) -> bool:
        node_id, tree_id = item["source_node_id"], item["tree_id"]
        return (
            memories.get(item["memory_id"]) == (node_id, tree_id)
            and (node_id is None or nodes.get(node_id) == tree_id)
            and tree_id in titles
        )

    return [
        [{**item, "tree_title": _title(titles[item["tree_id"]])} for item in items if live(item)]
        or None
        for items in lists
    ]


def sources_for_storage(session: Session, sources) -> list[dict] | None:
    """What an answer stores: the entries whose records still exist, without titles."""
    (current,) = resolve_sources(session, [sources])
    return [
        {key: item[key] for key in item if key != "tree_title"} for item in current or []
    ] or None


def scrub_sources(
    session: Session,
    *,
    tree_ids: Iterable[int] = (),
    node_ids: Iterable[int] = (),
    memory_ids: Iterable[int] = (),
    every_memory: bool = False,
) -> None:
    """Remove stored entries that point at deleted topics, nodes or memories.

    Runs in the caller's transaction. Rows are rewritten through the ORM, so the change
    feed tells open windows which answers to reload; clearing every memory touches all
    answers with provenance and reports a full reload instead."""
    trees, nodes, memories = (sorted(set(ids)) for ids in (tree_ids, node_ids, memory_ids))
    # The JSON column stores an absent list as the JSON text 'null'.
    if every_memory:
        stored = text("sources IS NOT NULL AND sources <> 'null'")
        # A bulk update makes every window reload, so skip it when nothing is stored.
        if session.exec(select(Message.id).where(stored).limit(1)).first() is not None:
            session.execute(
                update(Message)
                .where(stored)
                .values(sources=None)
                .execution_options(synchronize_session=False)
            )
        return
    if not (trees or nodes or memories):
        return
    # json_each and json_extract raise "malformed JSON" on a value the app never writes
    # (not JSON, not an array, an element that is not an object), which would fail the
    # whole delete; reads skip such values (_entries), so the scrub does too. The guards
    # sit in CASE, which evaluates only the branch it picks: SQLite may evaluate both
    # operands of AND, and WHERE terms in an order of its choosing.
    candidates = session.execute(
        text(
            "SELECT m.id FROM message m WHERE m.sources IS NOT NULL AND m.sources <> 'null' "
            "AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(m.sources) "
            "AND json_type(m.sources) = 'array' THEN m.sources ELSE '[]' END) e "
            "WHERE CASE WHEN e.type = 'object' THEN "
            "json_extract(e.value, '$.tree_id') IN (SELECT value FROM json_each(:trees)) "
            "OR json_extract(e.value, '$.source_node_id') IN "
            "(SELECT value FROM json_each(:nodes)) "
            "OR json_extract(e.value, '$.memory_id') IN (SELECT value FROM json_each(:memories)) "
            "ELSE 0 END)"
        ),
        {"trees": json.dumps(trees), "nodes": json.dumps(nodes), "memories": json.dumps(memories)},
    ).scalars()
    dead_trees, dead_nodes, dead_memories = set(trees), set(nodes), set(memories)
    for message_id in list(candidates):
        message = session.get(Message, message_id)
        if message is None:
            continue
        kept = [
            item
            for item in _entries(message.sources)
            if item["tree_id"] not in dead_trees
            and item["source_node_id"] not in dead_nodes
            and item["memory_id"] not in dead_memories
        ]
        message.sources = kept or None
        session.add(message)
