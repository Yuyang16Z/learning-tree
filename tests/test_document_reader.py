"""Read-only document tools preserve sources, pagination and bounded JSON responses."""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""
os.environ["MEMORY_RETRIEVAL_MODE"] = "lexical"

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import document_context
from app.document_context import make_document_reader
from app.documents import DocumentAttachment
from app.models import KnowledgeTree, Message, Node


@pytest.fixture
def sources(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reader.db'}")
    with Session(engine) as session:
        SQLModel.metadata.create_all(engine)
        tree, foreign_tree = KnowledgeTree(title="Learning"), KnowledgeTree(title="Unrelated")
        session.add_all([tree, foreign_tree])
        session.flush()
        root = Node(tree_id=tree.id, title="Root")
        foreign_node = Node(tree_id=foreign_tree.id, title="Other")
        session.add_all([root, foreign_node])
        session.flush()
        target = Node(tree_id=tree.id, parent_id=root.id, title="Current")
        sibling = Node(tree_id=tree.id, parent_id=root.id, title="Sibling")
        session.add_all([target, sibling])
        session.flush()
        text = ('验证条件 "source"\\line\n' + "复杂内容😀" * 10) * 14
        docs = []
        for index in range(8):
            doc = DocumentAttachment(
                tree_id=foreign_tree.id if index == 6 else tree.id,
                name="学" * 236 + ".pdf",
                media_type="application/pdf",
                size=10,
                sha256=str(index),
                content=b"synthetic",
                sections=[{"label": f"Page {page}", "text": text} for page in range(1, 8)],
                warnings=["部分页面可能是扫描图片，本工具未进行 OCR。" * 20],
            )
            session.add(doc)
            docs.append(doc)
        session.flush()
        session.add_all(
            [
                Message(
                    node_id=root.id,
                    role="user",
                    content="Ancestor source",
                    document_ids=[doc.id for doc in docs[:3]],
                ),
                Message(
                    node_id=target.id,
                    role="user",
                    content="Current source",
                    document_ids=[doc.id for doc in docs[3:5]],
                ),
                Message(
                    node_id=sibling.id,
                    role="user",
                    content="Sibling source",
                    document_ids=[docs[5].id],
                ),
                Message(
                    node_id=foreign_node.id,
                    role="user",
                    content="Foreign source",
                    document_ids=[docs[6].id],
                ),
            ]
        )
        session.commit()
        state = {
            "tree": tree.id,
            "foreign_tree": foreign_tree.id,
            "root": root.id,
            "target": target.id,
            "sibling": sibling.id,
            "foreign_node": foreign_node.id,
            "docs": [doc.id for doc in docs],
            "text": text,
        }
    yield engine, state
    engine.dispose()


def make_reader(sources, limit=32, allowed=None):
    engine, state = sources
    return make_document_reader(
        engine,
        state["target"],
        state["tree"],
        set(state["docs"] if allowed is None else allowed),
        max_chars=limit,
    )


def call(reader, args, limit=32):
    raw = reader(args)
    assert len(raw.encode("utf-8")) <= 512 + 4 * limit
    result = json.loads(raw)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("limit", [1, 32, 256, 768])
def test_document_index_pages_long_names_within_budget(sources, limit):
    _, state = sources
    reader = make_reader(sources, limit)
    start, ids = 0, []
    while start is not None:
        result = call(reader, {"action": "index", "start": start}, limit)
        assert result["ok"]
        assert 1 <= len(result["documents"]) <= 2
        for document in result["documents"]:
            assert document["name_truncated"] and document["name"].endswith("…")
            assert document["warning_count"] == 1
            ids.append(document["id"])
        following = result["next_start"]
        assert following is None or following > start
        start = following
    assert ids == sorted(state["docs"][:5])


@pytest.mark.parametrize("limit", [1, 32, 768])
def test_section_index_preserves_all_page_offsets(sources, limit):
    _, state = sources
    reader = make_reader(sources, limit)
    start, sections = 0, []
    while start is not None:
        result = call(
            reader,
            {
                "action": "index",
                "document_id": state["docs"][0],
                "start": start,
            },
            limit,
        )
        assert result["ok"] and result["name_truncated"]
        assert 1 <= len(result["sections"]) <= 2
        sections.extend(result["sections"])
        following = result["next_start"]
        assert following is None or following > start
        start = following
    assert [section["section_index"] for section in sections] == list(range(7))
    assert [section["source"] for section in sections] == [f"Page {i}" for i in range(1, 8)]


def test_read_pagination_reassembles_exact_unicode_and_escaped_text(sources):
    _, state = sources
    reader = make_reader(sources)
    start, parts = 0, []
    while start is not None:
        result = call(
            reader,
            {
                "action": "read",
                "document_id": state["docs"][0],
                "section_index": 0,
                "start": start,
            },
        )
        assert result["ok"] and 0 < len(result["text"]) <= 32
        assert result["start"] == start
        assert result["text"] == state["text"][result["start"] : result["end"]]
        assert result["next_start"] is None or result["next_start"] > start
        parts.append(result["text"])
        start = result["next_start"]
    assert "".join(parts) == state["text"]


@pytest.mark.parametrize("limit", [1, 32, 768])
def test_search_results_keep_match_coordinates_and_progress(sources, limit):
    _, state = sources
    reader = make_reader(sources, limit)
    result = call(
        reader,
        {
            "action": "search",
            "document_id": state["docs"][0],
            "query": "验证",
        },
        limit,
    )
    assert result["ok"] and result["matches"]
    assert 1 <= len(result["matches"]) <= 2
    assert sum(len(match["text"]) for match in result["matches"]) <= limit
    for match in result["matches"]:
        assert match["text"] == state["text"][match["start"] : match["end"]]
        assert match["end"] <= match["match_end"]
        assert "name" not in match and "document_id" not in match
        # Search provides the exact continuation offset for the read action.
        if match["end"] < match["match_end"]:
            continuation = call(
                reader,
                {
                    "action": "read",
                    "document_id": state["docs"][0],
                    "section_index": match["section_index"],
                    "start": match["end"],
                },
                limit,
            )
            assert continuation["ok"] and continuation["text"]
    assert result["next_start"] == len(result["matches"])
    next_page = call(
        reader,
        {
            "action": "search",
            "document_id": state["docs"][0],
            "query": "验证",
            "start": result["next_start"],
        },
        limit,
    )
    assert next_page["ok"] and next_page["matches"]


@pytest.mark.parametrize("index", [5, 6, 7])
def test_reader_rejects_siblings_foreign_and_unreferenced_sources(sources, index):
    _, state = sources
    result = call(
        make_reader(sources),
        {
            "action": "read",
            "document_id": state["docs"][index],
        },
    )
    assert result == {"ok": False, "error": "source_unavailable"}


def test_newly_attached_document_not_in_generation_snapshot_is_unavailable(sources):
    engine, state = sources
    reader = make_reader(sources, allowed=state["docs"][:5])
    with Session(engine) as session:
        session.add(
            Message(
                node_id=state["target"],
                role="user",
                content="Added later",
                document_ids=[state["docs"][7]],
            )
        )
        session.commit()
    result = call(reader, {"action": "read", "document_id": state["docs"][7]})
    assert result == {"ok": False, "error": "source_unavailable"}


def test_reparenting_invalidates_old_ancestor_sources(sources):
    engine, state = sources
    reader = make_reader(sources)
    assert call(reader, {"action": "read", "document_id": state["docs"][0]})["ok"]
    with Session(engine) as session:
        target = session.get(Node, state["target"])
        target.parent_id = None
        session.add(target)
        session.commit()
    assert call(reader, {"action": "read", "document_id": state["docs"][0]}) == {
        "ok": False,
        "error": "source_unavailable",
    }
    assert call(reader, {"action": "read", "document_id": state["docs"][3]})["ok"]


def test_deleted_document_and_deleted_target_invalidate_reader(sources):
    engine, state = sources
    reader = make_reader(sources)
    with Session(engine) as session:
        session.delete(session.get(DocumentAttachment, state["docs"][0]))
        session.commit()
    assert call(reader, {"action": "read", "document_id": state["docs"][0]}) == {
        "ok": False,
        "error": "source_unavailable",
    }
    # Missing IDs are skipped without losing directory pagination progress.
    assert call(reader, {"action": "index"})["ok"]
    with Session(engine) as session:
        session.delete(session.get(Node, state["target"]))
        session.commit()
    assert call(reader, {"action": "index"}) == {
        "ok": False,
        "error": "source_unavailable",
    }


def test_incomplete_message_is_not_a_readable_document_reference(sources):
    engine, state = sources
    with Session(engine) as session:
        session.add(
            Message(
                node_id=state["target"],
                role="user",
                content="Incomplete",
                status="error",
                document_ids=[state["docs"][7]],
            )
        )
        session.commit()
    assert call(
        make_reader(sources),
        {
            "action": "read",
            "document_id": state["docs"][7],
        },
    ) == {"ok": False, "error": "source_unavailable"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "read", "start": -1},
        {"action": "read", "start": True},
        {"action": "read", "max_chars": 0},
        {"action": "index", "write": True},
        {"action": "search", "query": "x" * 201},
        {"action": "unknown"},
    ],
)
def test_invalid_arguments_never_escape_as_parser_errors(sources, arguments):
    assert call(make_reader(sources), arguments) == {"ok": False, "error": "invalid_arguments"}


def test_reader_errors_do_not_expose_internal_details(sources, monkeypatch):
    def fail(*args):
        raise RuntimeError("sensitive internal SQL")

    monkeypatch.setattr(document_context, "get_ancestors", fail)
    assert call(make_reader(sources), {"action": "index"}) == {
        "ok": False,
        "error": "source_read_failed",
    }
