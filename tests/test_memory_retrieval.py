"""Memory retrieval behavior uses synthetic records and deterministic provider doubles.

These tests verify routing, source boundaries and lifecycle, not embedding quality.
Real model relevance is checked separately against synthetic text on the local runtime.
"""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from sqlalchemy import delete, event
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import KnowledgeTree, Memory, MemoryEmbedding, Node
from app.retrieval import RetrievalConfig, retrieve_memory


class Backend:
    """Explicit test double; vectors are fixed fixtures, never production embeddings."""

    model_key = "synthetic-test-model-v1"

    def __init__(self, relevant=(), *, vectors=None, query_vectors=None, scores=None):
        self.relevant = set(relevant)
        self.vectors = vectors or {}
        self.query_vectors = query_vectors or {}
        self.scores = scores or {}
        self.document_calls = []
        self.query_calls = []
        self.rerank_calls = []

    def embed_query(self, query):
        self.query_calls.append(query)
        return self.query_vectors.get(query, [1.0, 0.0, 0.0])

    def embed_documents(self, documents):
        self.document_calls.append(list(documents))
        return [
            self.vectors.get(text, [1.0, 0.0, 0.0] if text in self.relevant else [0.0, 1.0, 0.0])
            for text in documents
        ]

    def rerank(self, query, documents):
        self.rerank_calls.append((query, list(documents)))
        return [self.scores.get(text, 0.95 if text in self.relevant else 0.0) for text in documents]


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory-test.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([KnowledgeTree(id=1, title="学习"), KnowledgeTree(id=2, title="另一棵树")])
        session.add_all(
            [
                Node(id=1, tree_id=1, title="根", status="complete"),
                Node(id=2, tree_id=1, parent_id=1, title="当前路径", status="complete"),
                Node(id=3, tree_id=1, parent_id=1, title="兄弟分支", status="complete"),
                Node(id=4, tree_id=2, title="另一棵树的根", status="complete"),
                Node(id=5, tree_id=1, parent_id=1, title="生成中", status="pending"),
                Node(id=6, tree_id=1, parent_id=1, title="失败", status="error"),
                Node(id=7, tree_id=1, parent_id=1, title="中断", status="interrupted"),
                Node(id=8, tree_id=1, parent_id=1, title="旧节点", status="idle"),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def remember(session, content, *, kind="fact", tree_id=1, source_node_id=1):
    memory = Memory(kind=kind, content=content, tree_id=tree_id, source_node_id=source_node_id)
    session.add(memory)
    session.commit()
    session.refresh(memory)
    return memory


def ids(result):
    return {hit.memory_id for hit in result.facts}


def test_old_relevant_memory_is_not_lost_to_recent_eight_limit(session):
    content = "过拟合：训练集表现好而未见数据泛化差，可以用验证集和正则化检查。"
    old = remember(session, content)
    for index in range(12):
        remember(session, f"不相关的地理记录 {index}：当地河流自西向东流。")
    backend = Backend([content])

    result = retrieve_memory(
        session, 1, [1, 2], query="为什么换一批测试样本效果就很差？", backend=backend
    )

    assert old.id in ids(result)
    assert result.mode == "hybrid"
    assert content in result.text
    assert content in sum(backend.document_calls, [])
    assert len(sum(backend.document_calls, [])) == 13


def test_current_question_changes_selected_memory(session):
    apples = remember(session, "苹果树栽培需要光照、灌溉和修剪。")
    compiler = remember(session, "编译器把源程序转换成机器码。")
    backend = Backend(
        vectors={apples.content: [1.0, 0.0, 0.0], compiler.content: [0.0, 1.0, 0.0]},
        query_vectors={"orchard": [1.0, 0.0, 0.0], "translation": [0.0, 1.0, 0.0]},
    )
    config = RetrievalConfig(top_k=1, rerank=False)

    first = retrieve_memory(session, 1, [1], query="orchard", config=config, backend=backend)
    second = retrieve_memory(session, 1, [1], query="translation", config=config, backend=backend)

    assert ids(first) == {apples.id}
    assert ids(second) == {compiler.id}
    assert len(sum(backend.document_calls, [])) == 2, "cached documents should not be re-embedded"


def test_source_filter_precedes_embedding_and_reranking(session):
    valid = remember(session, "当前来源：schema 校验在执行工具前进行。")
    disallowed = [
        remember(session, "兄弟分支 schema 秘密", source_node_id=3),
        remember(session, "跨树 schema 秘密", tree_id=2, source_node_id=4),
        remember(session, "来源和树不符的 schema 秘密", source_node_id=4),
        remember(session, "不存在来源的 schema 秘密", source_node_id=999),
        remember(session, "无来源 schema 秘密", source_node_id=None),
        remember(session, "生成中 schema 秘密", source_node_id=5),
        remember(session, "失败 schema 秘密", source_node_id=6),
        remember(session, "中断 schema 秘密", source_node_id=7),
    ]
    backend = Backend([valid.content, *(item.content for item in disallowed)])

    result = retrieve_memory(
        session, 1, [1, 2, 4, 5, 6, 7, 999], query="schema 校验", backend=backend
    )

    assert ids(result) == {valid.id}
    evaluated = sum(backend.document_calls, []) + [
        text for _, texts in backend.rerank_calls for text in texts
    ]
    for item in disallowed:
        assert item.content not in evaluated
        assert item.content not in result.text


def test_global_preferences_are_independent_of_query_and_have_valid_sources(session):
    legacy = remember(
        session, "喜欢先举例再给公式。", kind="preference", tree_id=None, source_node_id=None
    )
    other_tree = remember(
        session, "使用中文解释。", kind="preference", tree_id=None, source_node_id=4
    )
    stale = remember(session, "应该忽略用户。", kind="preference", tree_id=None, source_node_id=999)
    interrupted = remember(
        session, "来自未完成回答。", kind="preference", tree_id=None, source_node_id=7
    )
    backend = Backend()

    result = retrieve_memory(session, 1, [1, 2], query="矩阵特征值", backend=backend)

    assert {hit.memory_id for hit in result.preferences} == {legacy.id, other_tree.id}
    assert not result.facts
    assert legacy.content in result.text and other_tree.content in result.text
    assert stale.content not in result.text and interrupted.content not in result.text
    assert not backend.document_calls, "global preferences are not subject to vector relevance"


def test_legacy_idle_source_is_still_eligible(session):
    old = remember(session, "旧学习记录：SQL 使用参数化查询。", source_node_id=8)
    result = retrieve_memory(
        session, 1, [1, 8], query="SQL 参数化查询", backend=Backend([old.content])
    )
    assert ids(result) == {old.id}


@pytest.mark.parametrize("tree_id,sources", [(1, []), (1, None), (None, [1])])
def test_empty_scope_never_opens_global_fact_search(session, tree_id, sources):
    fact = remember(session, "schema 参数校验")
    backend = Backend([fact.content])
    result = retrieve_memory(session, tree_id, sources, query="schema", backend=backend)
    assert not result.facts
    assert not backend.document_calls


def test_unrelated_memories_do_not_fill_top_k(session):
    remember(session, "苹果树通常需要定期灌溉。")
    remember(session, "河流沿地形从高处流向低处。")
    result = retrieve_memory(session, 1, [1], query="量子纠缠实验", backend=Backend())
    assert not result.facts
    assert result.text == ""


def test_reranker_can_reject_semantic_false_positive(session):
    wrong = remember(session, "这是一段属于其他问题的说明。")
    right = remember(session, "保存的是模型在当前数据分布下的判断。")
    backend = Backend(
        [wrong.content, right.content], scores={wrong.content: 0.001, right.content: 0.9}
    )

    result = retrieve_memory(
        session,
        1,
        [1],
        query="输出的含义",
        config=RetrievalConfig(min_rerank_score=0.05),
        backend=backend,
    )

    assert ids(result) == {right.id}
    assert result.reranked


def test_quote_is_available_to_semantic_query(session):
    fact = remember(session, "正则化通过惩罚复杂度约束模型。")
    backend = Backend([fact.content])
    result = retrieve_memory(
        session, 1, [1], query="这是为什么？", quoted_text="正则化", backend=backend
    )
    assert fact.id in ids(result)
    assert any("正则化" in query and "这是为什么" in query for query in backend.query_calls)


def test_existing_context_and_duplicate_memories_do_not_waste_budget(session):
    repeated = "SQL 参数化查询把数据与指令分开处理。"
    already = "SQLite 是嵌入式关系数据库。"
    first = remember(session, repeated, source_node_id=1)
    second = remember(session, repeated, source_node_id=2)
    present = remember(session, already)
    result = retrieve_memory(
        session,
        1,
        [1, 2],
        query="SQL 数据库",
        existing_text=f"用户之前看到的完整回答：{already}",
        backend=Backend([repeated, already]),
    )
    assert len(ids(result) & {first.id, second.id}) == 1
    assert present.id not in ids(result)
    assert result.text.count(repeated) == 1


def test_retrieval_obeys_top_k_and_character_budget(session):
    contents = [f"参数校验规则 {index}：" + "明确格式和类型。" * 16 for index in range(10)]
    for content in contents:
        remember(session, content)
    config = RetrievalConfig(top_k=2, char_budget=420, preference_budget=80)
    result = retrieve_memory(
        session, 1, [1], query="参数校验", config=config, backend=Backend(contents)
    )
    assert 0 < len(result.facts) <= 2
    assert sum(len(hit.content) for hit in result.facts) <= config.char_budget
    for hit in result.facts:
        assert hit.source_node_id == 1
        assert hit.content in result.text


def test_document_cache_changes_with_content_and_embedding_model(session):
    memory = remember(session, "schema 参数格式的约束。")
    backend = Backend([memory.content])
    config = RetrievalConfig(rerank=False)
    retrieve_memory(session, 1, [1], query="schema", config=config, backend=backend)
    retrieve_memory(session, 1, [1], query="schema", config=config, backend=backend)
    assert backend.document_calls == [[memory.content]]

    previous_hashes = {entry.content_hash for entry in session.exec(select(MemoryEmbedding)).all()}
    memory.content = "schema 参数格式与必填条件的约束。"
    backend.relevant.add(memory.content)
    session.add(memory)
    session.commit()
    retrieve_memory(session, 1, [1], query="schema", config=config, backend=backend)
    assert backend.document_calls[-1] == [memory.content]
    current_hashes = {entry.content_hash for entry in session.exec(select(MemoryEmbedding)).all()}
    assert current_hashes != previous_hashes

    changed_model = Backend([memory.content])
    changed_model.model_key = "synthetic-test-model-v2"
    retrieve_memory(session, 1, [1], query="schema", config=config, backend=changed_model)
    assert changed_model.document_calls == [[memory.content]]


def test_authoritative_memory_deletion_cannot_be_revived_by_vector_cache(session):
    memory = remember(session, "需要被删除的 schema 规则。")
    backend = Backend([memory.content])
    assert ids(retrieve_memory(session, 1, [1], query="schema", backend=backend)) == {memory.id}
    session.delete(memory)
    session.commit()
    result = retrieve_memory(session, 1, [1], query="schema", backend=backend)
    assert not result.facts and memory.content not in result.text


def test_embedding_failure_uses_lexical_retrieval_without_crashing(session):
    content = "MCP 的 inputSchema 定义工具参数及必填字段。"
    relevant = remember(session, content)
    remember(session, "苹果树需要灌溉。")

    class Unavailable(Backend):
        def embed_query(self, query):
            raise RuntimeError("synthetic embedding unavailable")

        def embed_documents(self, documents):
            raise RuntimeError("synthetic embedding unavailable")

    result = retrieve_memory(session, 1, [1], query="MCP inputSchema", backend=Unavailable())
    assert result.mode == "lexical"
    assert ids(result) == {relevant.id}
    assert "synthetic embedding unavailable" not in result.text


def test_reranker_failure_preserves_successful_hybrid_candidates(session):
    fact = remember(session, "过拟合使模型在新数据上的表现变差。")

    class UnavailableReranker(Backend):
        def rerank(self, query, documents):
            raise RuntimeError("synthetic reranker unavailable")

    result = retrieve_memory(
        session, 1, [1], query="换一组测试样本就不准了", backend=UnavailableReranker([fact.content])
    )
    assert ids(result) == {fact.id}
    assert result.mode == "hybrid"
    assert not result.reranked


@pytest.fixture
def api(session, monkeypatch):
    from fastapi.testclient import TestClient

    import app.db as database
    import app.main as main
    import app.service as service
    from app.models import ModelConfig
    from app.routers import nodes

    engine = session.get_bind()
    for module in (database, main, service, nodes):
        monkeypatch.setattr(module, "engine", engine)

    def session_override():
        with Session(engine) as other:
            yield other

    main.app.dependency_overrides[database.get_session] = session_override
    nodes._GENERATIONS.clear()
    session.add(
        ModelConfig(
            label="Synthetic test",
            base_url="https://synthetic.invalid/v1",
            llm_model="synthetic",
            api_key="synthetic-key-never-sent",
            is_default=True,
        )
    )
    session.commit()
    client = TestClient(main.app)
    yield client
    for generation in list(nodes._GENERATIONS.values()):
        generation.stop.set()
    nodes._GENERATIONS.clear()
    main.app.dependency_overrides.clear()


def test_retrieval_status_and_prepare_return_only_public_state(api, monkeypatch):
    from app import semantic_models

    calls = []
    status = {"state": "ready", "embedding_ready": True, "reranker_ready": True}
    monkeypatch.setattr(semantic_models, "get_status", lambda: status.copy())

    def prepare():
        calls.append("prepare")
        return status.copy()

    monkeypatch.setattr(semantic_models, "start_prepare", prepare)
    read = api.get("/api/memories/retrieval/status")
    assert read.status_code == 200
    assert read.json() == status
    started = api.post("/api/memories/retrieval/prepare")
    assert started.status_code == 200
    assert started.json() == status
    assert calls == ["prepare"]


@pytest.mark.parametrize("deletion", ["single", "clear", "node", "tree"])
def test_memory_management_deletes_corresponding_vectors(api, session, deletion):
    target = remember(session, "待删除的学习事实。", source_node_id=2)
    retained = remember(session, "另一棵树的事实。", tree_id=2, source_node_id=4)
    for memory in (target, retained):
        session.add(
            MemoryEmbedding(
                memory_id=memory.id,
                model_key="synthetic-test-cache",
                content_hash="synthetic-hash",
                vector=[1.0, 0.0, 0.0],
            )
        )
    session.commit()
    endpoint = {
        "single": f"/api/memories/{target.id}",
        "clear": "/api/memories",
        "node": "/api/nodes/2",
        "tree": "/api/trees/1",
    }[deletion]

    response = api.delete(endpoint)

    assert response.status_code == 200, response.text
    session.expire_all()
    remaining_memories = {item.id for item in session.exec(select(Memory)).all()}
    remaining_vectors = {item.memory_id for item in session.exec(select(MemoryEmbedding)).all()}
    expected = set() if deletion == "clear" else {retained.id}
    assert remaining_memories == expected
    assert remaining_vectors == expected


def test_slow_retrieval_does_not_hold_request_lock_or_prevent_stop(api, monkeypatch):
    import threading

    from app.routers import nodes

    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    failures = []
    model_calls = []

    def slow_retrieval(*args, **kwargs):
        entered.set()
        release.wait(4)
        return ""

    def model(*args, **kwargs):
        model_calls.append(True)
        yield "text", "停止后不应继续生成"

    monkeypatch.setattr(nodes, "fetch_memory_note", slow_retrieval)
    monkeypatch.setattr(nodes, "stream_chat", model)
    monkeypatch.setattr(nodes, "extract_and_save", lambda *args, **kwargs: None)

    def ask():
        try:
            response = api.post(
                "/api/nodes/1/ask", json={"question": "测试问题", "request_id": "retrieval-stop"}
            )
            assert response.status_code == 200, response.text
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    try:
        assert entered.wait(3), "retrieval must run as part of this generation"
        acquired = nodes._REQUEST_LOCK.acquire(timeout=0.5)
        if acquired:
            nodes._REQUEST_LOCK.release()
        assert acquired, "embedding/reranking must not hold the global generation lock"
        stopped = api.post("/api/nodes/1/stop", json={"request_id": "retrieval-stop"})
        assert stopped.status_code == 200, stopped.text
        assert completed.wait(2), "stop should finish even while retrieval is still running"
    finally:
        release.set()
        worker.join(5)
    assert not failures
    assert not model_calls, "cancelled retrieval must not lead to a model call"


def test_missing_semantic_models_never_download_during_query(monkeypatch):
    from app import semantic_models

    monkeypatch.setattr(semantic_models, "_enabled", lambda: True)
    backend = semantic_models.SemanticBackend()
    monkeypatch.setattr(
        backend, "_load", lambda *args, **kwargs: pytest.fail("query-time model download")
    )
    with pytest.raises(semantic_models.SemanticUnavailable):
        backend.embed_query("只包含合成文本的问题")
    with pytest.raises(semantic_models.SemanticUnavailable):
        backend.embed_documents(["只包含合成文本的记忆"])
    assert backend.status() == {
        "state": "degraded",
        "embedding_ready": False,
        "reranker_ready": False,
    }


def test_lexical_mode_does_not_start_semantic_preparation(monkeypatch):
    from app import semantic_models

    monkeypatch.setattr(semantic_models, "_enabled", lambda: False)
    backend = semantic_models.SemanticBackend()
    monkeypatch.setattr(
        backend, "_load", lambda *args, **kwargs: pytest.fail("disabled model preparation")
    )
    assert backend.start(allow_download=True) == {
        "state": "disabled",
        "embedding_ready": False,
        "reranker_ready": False,
    }
    with pytest.raises(semantic_models.SemanticUnavailable):
        backend.embed_query("合成问题")


def test_explicit_prepare_is_not_lost_during_cached_model_warmup(monkeypatch):
    import threading

    from app import semantic_models

    monkeypatch.setattr(semantic_models, "_enabled", lambda: True)
    backend = semantic_models.SemanticBackend()
    entered = threading.Event()
    release = threading.Event()
    loads = []

    def load(allow_download):
        loads.append(allow_download)
        if not allow_download:
            entered.set()
            release.wait(3)
        else:
            backend._embedding = object()
            backend._reranker = object()

    monkeypatch.setattr(backend, "_load", load)
    backend.start(allow_download=False)
    try:
        assert entered.wait(2)
        worker = backend._worker
        assert backend.start(allow_download=True)["state"] == "preparing"
        assert backend.start(allow_download=True)["state"] == "preparing"
        assert backend._worker is worker, "preparation requests must share one background worker"
    finally:
        release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert loads == [False, True]
    assert backend.status()["state"] == "ready"


def test_single_candidate_obeys_explicit_rerank_relevance_threshold(session):
    memory = remember(session, "这是某个其他问题的说明。")
    backend = Backend([memory.content], scores={memory.content: 0.001})
    result = retrieve_memory(
        session,
        1,
        [1],
        query="输出的含义",
        config=RetrievalConfig(min_rerank_score=0.05),
        backend=backend,
    )
    assert result.reranked
    assert not result.facts


def test_invalid_cache_dimension_is_rebuilt_instead_of_permanent_lexical_fallback(session):
    memory = remember(session, "缓存中的向量应与当前模型维数一致。")
    backend = Backend([memory.content])
    config = RetrievalConfig(rerank=False)
    first = retrieve_memory(
        session, 1, [1], query="semantic paraphrase", config=config, backend=backend
    )
    assert ids(first) == {memory.id}
    cached = session.exec(select(MemoryEmbedding)).one()
    cached.vector = [1.0, 0.0]
    session.add(cached)
    session.commit()

    result = retrieve_memory(
        session, 1, [1], query="semantic paraphrase", config=config, backend=backend
    )

    assert result.mode == "hybrid"
    assert ids(result) == {memory.id}
    assert backend.document_calls == [[memory.content], [memory.content]]


def test_delete_immediately_before_cache_insert_cannot_recreate_orphan_vectors(session):
    memory = remember(session, "删除后不应继续保留的 schema 学习规则。")
    memory_id = memory.id
    session.add(
        MemoryEmbedding(
            memory_id=memory_id,
            model_key="previous-synthetic-model",
            content_hash="synthetic-old-hash",
            vector=[1.0, 0.0, 0.0],
        )
    )
    session.commit()
    engine = session.get_bind()
    deletions = []

    def delete_before_insert(connection, cursor, statement, parameters, context, executemany):
        if deletions or not statement.lstrip().lower().startswith("insert into memoryembedding"):
            return
        # Force the exact race: candidates and vectors are already computed,
        # but the SQLite cache INSERT has not yet executed. A separate committed
        # deletion must win over this stale retrieval's pending cache write.
        with Session(engine) as deleting:
            deleting.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id))
            deleting.execute(delete(Memory).where(Memory.id == memory_id))
            deleting.commit()
        deletions.append(memory_id)

    event.listen(engine, "before_cursor_execute", delete_before_insert)
    try:
        result = retrieve_memory(session, 1, [1], query="schema", backend=Backend([memory.content]))
    finally:
        event.remove(engine, "before_cursor_execute", delete_before_insert)

    assert deletions == [memory_id], "the competing deletion must commit before the cache write"
    assert result.mode == "hybrid", "the race must not be hidden by a semantic error fallback"
    assert not result.facts
    with Session(engine) as checking:
        assert checking.get(Memory, memory_id) is None
        assert checking.exec(select(MemoryEmbedding)).all() == []


@pytest.mark.parametrize("deletion", ["node", "tree"])
def test_branch_creation_and_deletion_leave_no_orphan_nodes(session, deletion):
    import threading

    from app.routers import nodes, trees
    from app.schemas import BranchIn

    engine = session.get_bind()
    before_insert = threading.Event()
    release_insert = threading.Event()
    deletion_started = threading.Event()
    deletion_finished = threading.Event()
    created = []
    failures = []

    def pause_branch_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("insert into node "):
            before_insert.set()
            release_insert.wait(5)

    def create_branch():
        try:
            with Session(engine) as creating:
                created.append(nodes.branch(2, BranchIn(seed_text="合成引用"), session=creating))
        except BaseException as exc:
            failures.append(exc)

    def delete_parent():
        deletion_started.set()
        try:
            with Session(engine) as deleting:
                if deletion == "node":
                    nodes.delete_node(2, session=deleting)
                else:
                    trees.delete_tree(1, session=deleting)
        except BaseException as exc:
            failures.append(exc)
        finally:
            deletion_finished.set()

    branch_worker = threading.Thread(target=create_branch, daemon=True)
    delete_worker = threading.Thread(target=delete_parent, daemon=True)
    event.listen(engine, "before_cursor_execute", pause_branch_insert)
    try:
        branch_worker.start()
        assert before_insert.wait(3), "branch must have validated its parent before this race"
        delete_worker.start()
        assert deletion_started.wait(2)
        assert not deletion_finished.wait(0.25), "deletion must wait for in-flight branch creation"
    finally:
        release_insert.set()
        branch_worker.join(5)
        if delete_worker.ident is not None:
            delete_worker.join(5)
        event.remove(engine, "before_cursor_execute", pause_branch_insert)

    assert not branch_worker.is_alive() and not delete_worker.is_alive()
    assert not failures
    assert len(created) == 1
    with Session(engine) as checking:
        remaining = checking.exec(select(Node)).all()
        remaining_ids = {node.id for node in remaining}
        assert created[0]["id"] not in remaining_ids, "deletion must discover the committed child"
        assert all(node.parent_id is None or node.parent_id in remaining_ids for node in remaining)
