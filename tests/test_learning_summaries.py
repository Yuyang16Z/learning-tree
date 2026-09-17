"""Versioned summaries use temporary SQLite and synthetic provider responses only."""

import threading
from dataclasses import replace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import learning_summaries as summaries
from app.llm import LLMSpec
from app.models import ContextSummary, KnowledgeTree, Memory, Message, Node


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'summary.sqlite'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="Learning"), KnowledgeTree(id=2, title="Other")])
        for node_id, tree_id, parent_id in [(1, 1, None), (2, 1, 1), (3, 1, 1), (4, 2, None)]:
            session.add(
                Node(
                    id=node_id,
                    tree_id=tree_id,
                    parent_id=parent_id,
                    title=f"Node {node_id}",
                    status="complete",
                    learning_note=f"My note {node_id}",
                    seed_text=f"Quote {node_id}",
                )
            )
            session.add_all(
                [
                    Message(
                        id=node_id * 10 + 1,
                        node_id=node_id,
                        role="user",
                        content=f"Question {node_id}",
                    ),
                    Message(
                        id=node_id * 10 + 2,
                        node_id=node_id,
                        role="assistant",
                        content=(
                            f"Source {node_id}: Trees represent nested syntax, but this is only one possible representation. "
                            * 35
                        ),
                    ),
                ]
            )
        session.commit()

    def sources(*ids):
        with Session(engine) as session:
            return [
                (
                    session.get(Node, node_id),
                    list(
                        session.exec(
                            select(Message).where(Message.node_id == node_id).order_by(Message.id)
                        ).all()
                    ),
                )
                for node_id in ids
            ]

    calls = []

    def provider(spec, payload, max_output_bytes):
        calls.append((spec, payload, max_output_bytes))
        return f"已覆盖：source {payload[0]['source_id']} is a learning record. [{payload[0]['source_id']}]"

    monkeypatch.setattr(summaries, "summarize_learning_context", provider)
    spec = LLMSpec("Synthetic", "https://provider.invalid/v1", "model", "synthetic-secret-key")
    yield engine, sources, spec, calls
    engine.dispose()


def records(engine):
    with Session(engine) as session:
        return list(session.exec(select(ContextSummary)).all())


def test_one_call_caches_exact_source_and_preserves_originals(workspace):
    engine, sources, spec, calls = workspace
    original = sources(1, 2)
    snapshots = [
        (node.model_dump(), [m.model_dump() for m in messages]) for node, messages in original
    ]
    result = summaries.summarize_history(engine, spec, original, "syntax", 4000)
    assert "模型摘要" in result and "原文摘录" in result
    assert len(result.encode()) <= 4000 and len(calls) == 1
    assert all("node=1," in item["source_id"] for item in calls[0][1])
    assert len(records(engine)) == 1
    cached = records(engine)[0]
    assert cached.tree_id == 1 and cached.source_node_ids == [1]
    assert cached.source_message_ids == [11, 12]
    assert "synthetic-secret-key" not in str(cached.model_dump())
    assert snapshots == [(node.model_dump(), [m.model_dump() for m in ms]) for node, ms in original]
    assert snapshots == [
        (node.model_dump(), [m.model_dump() for m in ms]) for node, ms in sources(1, 2)
    ]
    with Session(engine) as session:
        assert session.exec(select(Memory)).all() == []


def test_cached_prefix_survives_added_path_and_process_local_state_reset(workspace):
    engine, sources, spec, calls = workspace
    first = summaries.summarize_history(engine, spec, sources(1), "first query", 4000)
    assert first and len(calls) == 1
    summaries._FAILURES.clear()
    summaries._INFLIGHT.clear()
    summaries._EPOCHS.clear()
    assert summaries.summarize_history(engine, spec, sources(1), "different query", 4000) == first
    assert len(calls) == 1
    assert summaries.summarize_history(engine, spec, sources(1, 2), "new question", 4000)
    assert len(calls) == 2
    assert all("node=2," in item["source_id"] for item in calls[-1][1])
    assert len(records(engine)) == 2


def test_cache_survives_new_engine_and_does_not_recall_provider(workspace, monkeypatch):
    engine, sources, spec, calls = workspace
    original = sources(1)
    expected = summaries.summarize_history(engine, spec, original, "query", 3000)
    other_engine = create_engine(engine.url)
    monkeypatch.setattr(
        summaries, "summarize_learning_context", lambda *args: pytest.fail("persistent cache hit")
    )
    try:
        assert summaries.summarize_history(other_engine, spec, original, "query", 3000) == expected
    finally:
        other_engine.dispose()
    assert len(calls) == 1


def test_sibling_and_other_tree_never_enter_current_path(workspace):
    engine, sources, spec, calls = workspace
    summaries.summarize_history(engine, spec, sources(3), "secret sibling", 4000)
    summaries.summarize_history(engine, spec, sources(4), "secret other tree", 4000)
    current = summaries.summarize_history(engine, spec, sources(1, 2), "source", 4000)
    assert "node=3" not in current and "node=4" not in current
    assert all("node=1," in item["source_id"] for item in calls[-1][1])
    count = len(calls)
    assert summaries.summarize_history(engine, spec, sources(1, 4), "mixed tree", 4000) is None
    assert summaries.summarize_history(engine, spec, sources(2, 3), "mixed siblings", 4000) is None
    assert len(calls) == count


@pytest.mark.parametrize(
    "change", ["content", "role", "status", "learning_note", "seed_text", "parent_id", "images"]
)
def test_source_versions_invalidate_stale_cache(workspace, change):
    engine, sources, spec, calls = workspace
    before = sources(2)
    assert summaries.summarize_history(engine, spec, before, "query", 4000)
    with Session(engine) as session:
        if change in {"learning_note", "seed_text", "parent_id"}:
            node = session.get(Node, 2)
            setattr(node, change, None if change == "parent_id" else "Changed source")
            session.add(node)
        else:
            message = session.get(Message, 22)
            setattr(
                message,
                change,
                {
                    "content": "Changed conditions. " * 200,
                    "role": "user",
                    "status": "interrupted",
                    "images": ["data:image/png;base64,new-image"],
                }[change],
            )
            session.add(message)
        session.commit()
    assert summaries.summarize_history(engine, spec, before, "query", 4000) is None
    assert len(calls) == 1, "Stale snapshots cannot be summarized again or read from cache"
    if change != "status":
        assert summaries.summarize_history(engine, spec, sources(2), "query", 4000)
        assert len(calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", "anthropic"),
        ("base_url", "https://other.invalid/v1"),
        ("llm_model", "other-model"),
    ],
)
def test_provider_identity_is_part_of_cache(workspace, field, value):
    engine, sources, spec, calls = workspace
    summaries.summarize_history(engine, spec, sources(1), "query", 4000)
    summaries.summarize_history(engine, replace(spec, **{field: value}), sources(1), "query", 4000)
    assert len(calls) == 2 and len(records(engine)) == 2


def test_prompt_version_changes_cache_but_key_rotation_does_not(workspace, monkeypatch):
    engine, sources, spec, calls = workspace
    summaries.summarize_history(engine, spec, sources(1), "query", 4000)
    summaries.summarize_history(
        engine, replace(spec, api_key="rotated-secret"), sources(1), "query", 4000
    )
    assert len(calls) == 1
    monkeypatch.setattr(summaries, "SUMMARY_PROMPT_VERSION", "different-version")
    summaries.summarize_history(engine, spec, sources(1), "query", 4000)
    assert len(calls) == 2


def test_failure_cooldown_uses_local_fallback_without_repeated_provider_call(
    workspace, monkeypatch
):
    engine, sources, spec, _ = workspace
    calls = []
    clock = [100.0]
    monkeypatch.setattr(summaries.time, "monotonic", lambda: clock[0])

    def fail(*args):
        calls.append(1)
        raise RuntimeError("Synthetic provider failure")

    monkeypatch.setattr(summaries, "summarize_learning_context", fail)
    for _ in range(3):
        assert summaries.summarize_history(engine, spec, sources(1), "query", 4000) is None
    assert calls == [1] and records(engine) == []
    clock[0] += summaries.FAILURE_COOLDOWN + 1
    assert summaries.summarize_history(engine, spec, sources(1), "query", 4000) is None
    assert calls == [1, 1]


def test_same_block_concurrency_deduplicates_calls_and_keeps_provider_outside_mutation_lock(
    workspace, monkeypatch
):
    engine, sources, spec, _ = workspace
    entered, release = threading.Event(), threading.Event()
    lock = threading.RLock()
    calls = []
    results = []
    original = sources(1)

    def provider(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return "Synthetic summary [node=1,message=11]"

    monkeypatch.setattr(summaries, "summarize_learning_context", provider)
    worker = threading.Thread(
        target=lambda: results.append(
            summaries.summarize_history(
                engine,
                spec,
                original,
                "query",
                4000,
                mutation_lock=lock,
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(5)
        assert lock.acquire(timeout=1), "The provider cannot hold the source mutation lock"
        lock.release()
        assert (
            summaries.summarize_history(engine, spec, original, "query", 4000, mutation_lock=lock)
            is None
        )
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and results[0]
    assert calls == [1] and len(records(engine)) == 1


def test_stale_initial_cache_miss_is_rechecked_after_claim(workspace, monkeypatch):
    engine, sources, spec, calls = workspace
    missed, proceed = threading.Event(), threading.Event()
    delegate = threading.RLock()
    results = []

    class PauseBeforeFirstClaim:
        paused = False

        def __enter__(self):
            if threading.current_thread().name == "late-reader" and not self.paused:
                self.paused = True
                missed.set()
                assert proceed.wait(5)
            delegate.acquire()

        def __exit__(self, *args):
            delegate.release()

    monkeypatch.setattr(summaries, "_LOCK", PauseBeforeFirstClaim())
    original = sources(1)
    worker = threading.Thread(
        name="late-reader",
        target=lambda: results.append(
            summaries.summarize_history(engine, spec, original, "query", 4000)
        ),
    )
    worker.start()
    try:
        assert missed.wait(5)
        expected = summaries.summarize_history(engine, spec, original, "query", 4000)
        assert expected and len(calls) == 1
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results == [expected]
    assert len(calls) == 1, "A completed cache row wins over an earlier stale miss"
    assert len(records(engine)) == 1


@pytest.mark.parametrize(
    "change", ["delete_tree", "delete_node", "edit", "invalidate_only", "stop"]
)
def test_late_provider_cannot_restore_deleted_or_changed_sources(workspace, monkeypatch, change):
    engine, sources, spec, _ = workspace
    original = sources(1)
    stop, lock = threading.Event(), threading.RLock()

    def provider(*args):
        with lock, Session(engine) as session:
            if change == "stop":
                stop.set()
            elif change == "edit":
                node = session.get(Node, 1)
                node.learning_note = "New understanding"
                session.add(node)
            else:
                summaries.invalidate_summaries(
                    session, tree_id=1, node_ids=None if change == "delete_tree" else [1]
                )
                if change in {"delete_tree", "delete_node"}:
                    for message in session.exec(select(Message).where(Message.node_id == 1)).all():
                        session.delete(message)
                    session.delete(session.get(Node, 1))
                    if change == "delete_tree":
                        session.delete(session.get(KnowledgeTree, 1))
            session.commit()
        return "Late answer must not be persisted [node=1,message=11]"

    monkeypatch.setattr(summaries, "summarize_learning_context", provider)
    assert (
        summaries.summarize_history(
            engine, spec, original, "query", 4000, stop=stop, mutation_lock=lock
        )
        is None
    )
    assert records(engine) == []


def test_scoped_invalidation_preserves_other_branches_and_topics(workspace):
    engine, sources, spec, _ = workspace
    for node_id in [1, 2, 3, 4]:
        summaries.summarize_history(engine, spec, sources(node_id), "query", 4000)
    with Session(engine) as session:
        summaries.invalidate_summaries(session, node_ids=[2])
        session.commit()
    assert {row.source_node_ids[0] for row in records(engine)} == {1, 3, 4}
    with Session(engine) as session:
        summaries.invalidate_summaries(session, tree_id=1)
        session.commit()
    assert {row.source_node_ids[0] for row in records(engine)} == {4}


def test_long_chinese_message_stays_whole_and_oversized_input_falls_back(workspace):
    engine, sources, spec, calls = workspace
    content = "嵌套结构只有满足语法规则时才能生成有效语法树。" * 200
    assert 8000 < len(content.encode()) < 24000
    with Session(engine) as session:
        answer = session.get(Message, 12)
        answer.content = content
        session.add(answer)
        session.commit()
    assert summaries.summarize_history(engine, spec, sources(1), "query", 3000)
    assert len(calls) == 1
    assert calls[0][1][0]["text"] == content
    with Session(engine) as session:
        answer = session.get(Message, 12)
        answer.content *= 3
        session.add(answer)
        session.commit()
    assert summaries.summarize_history(engine, spec, sources(1), "query", 3000) is None
    assert len(calls) == 1


def test_mock_small_budget_and_stopped_request_never_call_provider(workspace):
    engine, sources, spec, calls = workspace
    stop = threading.Event()
    stop.set()
    assert summaries.summarize_history(engine, spec, sources(1), "query", 4000, stop=stop) is None
    assert summaries.summarize_history(engine, spec, sources(1), "query", 300) is None
    assert (
        summaries.summarize_history(
            engine, replace(spec, api_key="mock"), sources(1), "query", 4000
        )
        is None
    )
    assert (
        summaries.summarize_history(
            engine, replace(spec, base_url="mock://offline"), sources(1), "query", 4000
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize("budget", [512, 700, 1100, 2000, 4000])
def test_rendered_cached_and_extractive_text_always_fits_budget(workspace, budget):
    engine, sources, spec, _ = workspace
    summaries.summarize_history(engine, spec, sources(1, 2), "syntax", 4000)
    result = summaries.summarize_history(engine, spec, sources(1, 2), "syntax", budget)
    if result:
        assert len(result.encode()) <= budget
        assert "模型摘要" in result


def test_superseded_assistant_cannot_become_a_fresh_summary(workspace):
    engine, sources, spec, calls = workspace
    old = sources(1)
    with Session(engine) as session:
        session.add(
            Message(
                id=13,
                node_id=1,
                role="assistant",
                status="interrupted",
                content="New partial attempt",
            )
        )
        session.commit()
    assert summaries.summarize_history(engine, spec, old, "query", 4000) is None
    assert calls == []
