#!/usr/bin/env python3
"""Offline evaluation harness for a LearningTree database.

Evaluate a temporary copy of your own database, or a deterministic synthetic fixture:

    uv run python scripts/evaluate_retrieval.py --database branch_learning.db
    uv run python scripts/evaluate_retrieval.py --synthetic

The source file is copied with SQLite's backup API from a read-only connection
into a fresh private temporary directory; DATABASE_URL points at that copy before
any application module is imported, app.main is never imported, nothing is
written next to the source, and the copy is deleted afterwards. No model API is
called and no weights are downloaded (--hybrid only loads cached local models).
The JSON report holds ids and counts: no absolute paths and no question, answer,
fact, note or title text.
"""

import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "artifacts" / "retrieval-evaluation.json"
WORKSPACE_PREFIX = "learning-tree-eval-"
# Scope name -> (include_siblings, include_other_trees), as saved in RetrievalSettings.
SCOPES = {"path": (False, False), "tree": (True, False), "all": (True, True)}
_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECARS = ("-wal", "-shm", "-journal")
_ENVIRONMENT = (
    "DATABASE_URL",
    "MEMORY_RETRIEVAL_MODE",
    "DEFAULT_API_KEY",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
)


class EvaluationError(Exception):
    """A refused or failed evaluation; messages never contain database content or paths."""


class HybridUnavailable(EvaluationError):
    """--hybrid was requested but local semantic retrieval cannot run here."""


# ---------- Private workspace and read-only copy ----------


@contextmanager
def workspace():
    """A fresh 0700 temporary directory that is removed with everything in it."""
    path = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX))
    try:
        if os.name != "nt":
            os.chmod(path, 0o700)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _sidecars(source: Path) -> set[str]:
    return {suffix for suffix in _SIDECARS if Path(f"{source}{suffix}").exists()}


def copy_database(source: Path, target: Path) -> None:
    """Copy `source` into `target` through SQLite's backup API, writing nothing next to it.

    A read-only connection to a cleanly closed write-ahead-log database would create
    -wal and -shm files beside the source and leave them there, so such a file is read
    as immutable (it is self-contained). A database with a live log is read through
    that log, which SQLite does without creating files. Anything else is refused.
    """
    try:
        with source.open("rb") as handle:
            header = handle.read(100)
    except OSError:
        raise EvaluationError("The database file cannot be read.") from None
    if len(header) < 100 or not header.startswith(_SQLITE_HEADER):
        raise EvaluationError("The file is not a SQLite database.")
    before = _sidecars(source)
    if "-journal" in before:
        raise EvaluationError(
            "The database has an unfinished rollback journal. Start and stop LearningTree "
            "once so SQLite can recover it, then retry."
        )
    write_ahead_log = header[18] == 2  # File format version 2: journal_mode=WAL.
    if write_ahead_log and "-wal" in before and "-shm" not in before:
        raise EvaluationError(
            "The database has a write-ahead log without its shared-memory file. Start and "
            "stop LearningTree once so SQLite can recover it, then retry."
        )
    if write_ahead_log and "-wal" not in before:
        uri = source.as_uri() + "?mode=ro&immutable=1"
    else:
        uri = source.as_uri() + "?mode=ro"
    try:
        with (
            closing(sqlite3.connect(uri, uri=True)) as reader,
            closing(sqlite3.connect(target)) as writer,
        ):
            reader.backup(writer)
    except sqlite3.Error:
        raise EvaluationError(
            "SQLite could not read the database; nothing was evaluated."
        ) from None
    if _sidecars(source) != before:
        raise EvaluationError(
            "Another program opened the database while it was copied; nothing was "
            "evaluated. Retry while LearningTree is idle."
        )
    if os.name != "nt":
        os.chmod(target, 0o600)
    with closing(sqlite3.connect(target)) as copy:
        if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise EvaluationError("The copied database failed SQLite's integrity check.")


def bind_environment(database: Path, hybrid: bool) -> dict[str, str | None]:
    """Point the application at the copy and keep providers and downloads out of reach."""
    previous = {key: os.environ.get(key) for key in _ENVIRONMENT}
    os.environ.update(
        DATABASE_URL=f"sqlite:///{database.as_posix()}",
        MEMORY_RETRIEVAL_MODE="hybrid" if hybrid else "lexical",
        DEFAULT_API_KEY="",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    return previous


def restore_environment(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _engine(database: Path):
    from sqlalchemy.engine import URL
    from sqlmodel import create_engine

    if not database.parent.name.startswith(WORKSPACE_PREFIX):
        raise EvaluationError("Refusing to open a database outside the temporary workspace.")
    return create_engine(
        URL.create("sqlite", database=str(database)), connect_args={"check_same_thread": False}
    )


def _check_binding(database: Path, preloaded: bool) -> None:
    """In a fresh process the application's own engine must point at the copy."""
    import app.db as app_db

    if preloaded:
        return  # An embedding process imported the app first; only our engine is used.
    bound = app_db.engine.url.database
    if not bound or Path(bound).resolve() != database.resolve():
        raise EvaluationError("The application is not bound to the temporary copy.")


def prepare_copy(engine) -> None:
    """Run the app's additive start-up migration (init_db) against the copy only."""
    import app.db as app_db

    original = app_db.engine
    app_db.engine = engine  # init_db and its helpers read this module global.
    try:
        app_db.init_db()
    finally:
        app_db.engine = original


# ---------- Deterministic synthetic fixture ----------

_TINY_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _lines(head: str, line: str, count: int) -> str:
    return head + "\n" + "\n".join(line.format(index) for index in range(1, count + 1))


# Topics: tree_id -> (title, archived). Topic 4 is archived and must never be recalled.
_TOPICS = {
    4: ("Fixture · 已归档的旧主题", True),
    3: ("Fixture · 数据库索引", False),
    2: ("Fixture · Machine learning basics", False),
    1: ("Fixture · 排序算法", False),
}
# Question/answer turns in creation order: (node_id, tree_id, parent_id, seed_text,
# question, answer). A node's first turn creates it: a root without a parent, a branch
# with the quoted seed_text, otherwise a follow-up. Node 25 holds two turns, like a legacy
# multi-turn node. A None answer is an unfinished attempt, described in _UNFINISHED.
_TURNS = (
    (
        41,
        4,
        None,
        None,
        "快速排序和堆排序哪个更快？",
        "在多数实际数据上，快速排序通常比堆排序更快：它的内存访问更连续，缓存局部性更好；"
        "堆排序的最坏情况仍是 O(n log n)。",
    ),
    (
        31,
        3,
        None,
        None,
        "B+ 树索引为什么适合范围查询？",
        "B+ 树把记录放在叶子节点，叶子节点按键的顺序用指针相连。范围查询先找到起点，"
        "再沿叶子链表顺序扫描。一次查找的时间复杂度是 O(log n) 次页访问。",
    ),
    (
        32,
        3,
        31,
        None,
        "哈希索引能做范围查询吗？",
        "哈希索引把键映射到桶，只能高效支持等值查询；相邻的键在哈希后分散到不同的桶，"
        "所以不能用它做范围查询。",
    ),
    (
        21,
        2,
        None,
        None,
        "What is overfitting in machine learning?",
        "Overfitting happens when a model learns the noise and accidental details of its "
        "training data instead of the underlying pattern, so it scores well on the training "
        "data but generalizes poorly to new data.",
    ),
    (
        22,
        2,
        21,
        None,
        "How does regularization reduce overfitting in this plot?",
        "Regularization adds a penalty for large weights to the training loss. With L2 "
        "regularization the optimizer prefers smaller weights, which keeps the model smoother "
        "and reduces overfitting.",
    ),
    (
        24,
        2,
        21,
        "training data",
        "Why keep a separate validation set apart from the training data?",
        "A validation set is data the model never trains on. It is used to choose "
        "hyperparameters and compare models; the separate test set is kept for the final "
        "accuracy report.",
    ),
    (
        23,
        2,
        22,
        None,
        "Should I use dropout or L2 regularization against overfitting?",
        "Both reduce overfitting. Dropout randomly disables units during training while L2 "
        "regularization shrinks weights; which works better depends on the architecture and "
        "the data, so compare them on a validation set.",
    ),
    (
        25,
        2,
        24,
        None,
        "What is k-fold cross-validation?",
        "K-fold cross-validation splits the training data into k folds. Each fold serves once "
        "as the validation fold while the model trains on the others, and the scores are "
        "averaged.",
    ),
    (
        25,
        2,
        24,
        None,
        "How many folds should cross-validation use?",
        "Five or ten folds are common: more folds train on more data each time but cost more "
        "computation.",
    ),
    (26, 2, 23, None, "Can early stopping replace regularization?", None),
    (
        1,
        1,
        None,
        None,
        "快速排序的平均时间复杂度是多少？",
        "快速排序采用分治策略：先选择一个枢轴，把数组划分为小于枢轴和大于枢轴的两部分，"
        "再递归排序。平均情况下划分比较均衡，时间复杂度是 O(n log n)。",
    ),
    (
        2,
        1,
        1,
        None,
        "快速排序在什么情况下会退化？",
        "如果每次选到的枢轴都是当前区间的最小或最大元素，划分就极不均衡，递归深度变成 n，"
        "快速排序退化为 O(n^2)。另外，快速排序不是稳定排序。",
    ),
    (
        4,
        1,
        1,
        "分治",
        "分治思想在归并排序中如何体现？",
        "归并排序同样使用分治：把数组递归地拆成两半，分别排序后再把两个有序子数组合并。"
        "合并是线性的，所以时间复杂度稳定为 O(n log n)。",
    ),
    (
        5,
        1,
        4,
        None,
        "归并排序需要多少额外空间？",
        "数组版本的归并排序在合并时需要与原数组等长的辅助数组，额外空间是 O(n)；"
        "链表版本可以做到 O(1) 额外空间。",
    ),
    (
        6,
        1,
        2,
        "枢轴",
        "枢轴元素在划分中起什么作用？",
        "枢轴是划分时的比较基准：小于枢轴的元素放到左边，大于枢轴的放到右边，"
        "枢轴最后落在它的最终位置上。",
    ),
    (
        3,
        1,
        2,
        None,
        "怎样选择枢轴可以避免快速排序退化？",
        "常用办法是随机选择枢轴，或者三数取中：比较区间首、中、尾三个元素，取中位数作为枢轴。"
        "这样很难持续选到极端元素，退化的概率很低。",
    ),
    (
        # Long answers: this path compacts under the byte estimator only.
        8,
        1,
        5,
        None,
        "请完整推导归并排序的递推式。",
        _lines(
            "设 T(n) 为归并排序处理 n 个元素的时间，下面逐层展开。",
            "第{}层：把规模为 n 的问题拆成两个规模减半的子问题，合并代价与 n 成正比。",
            250,
        ),
    ),
    (
        # This path compacts under both estimators with a 32,768-token window.
        9,
        1,
        8,
        None,
        "外部排序为什么也用归并？",
        _lines(
            "外部排序的数据放不进内存，需要多轮归并磁盘上的有序段。",
            "第{}轮：把磁盘上的有序段两两归并，顺序读写比随机读写快得多。",
            700,
        ),
    ),
    (7, 1, 3, None, "快速排序可以并行化吗？", None),
)
# Unfinished nodes: node_id -> (node and answer status, partial answer).
_UNFINISHED = {26: ("error", ""), 7: ("pending", "快速排序可以把两个子区间")}
_NOTES = {
    24: "Validation chooses models; the test set only reports final accuracy.",
    4: "归并排序稳定，快速排序不稳定。",
    3: "三数取中：比较首、中、尾三个元素，取中位数作枢轴。",
}
_IMAGE_QUESTION_NODE = 22
# Facts extracted after a node's n-th turn: (node_id, n) -> ((fact_type, text, terms), ...).
_FACTS = {
    (41, 0): (
        (
            "conclusion",
            "快速排序通常比堆排序更快，因为快速排序的缓存局部性更好。",
            "快速排序 堆排序",
        ),
    ),
    (31, 0): (
        ("definition", "B+ 树的叶子节点按键的顺序用指针相连，适合范围查询。", "B+树 范围查询"),
        ("conclusion", "B+ 树一次查找的时间复杂度是 O(log n) 次页访问。", "时间复杂度 B+树"),
    ),
    (32, 0): (("condition", "哈希索引只适合等值查询，不能用来做范围查询。", "哈希索引 范围查询"),),
    (21, 0): (
        (
            "definition",
            "Overfitting means a model fits noise in the training data and generalizes poorly "
            "to new data.",
            "overfitting generalization",
        ),
        (
            "condition",
            "Overfitting is more likely when a model has many parameters relative to the number "
            "of training examples.",
            "overfitting parameters",
        ),
    ),
    (22, 0): (
        (
            "conclusion",
            "L2 regularization penalizes large weights, which reduces overfitting.",
            "regularization L2",
        ),
    ),
    (24, 0): (
        (
            "learner_state",
            "The learner is mixing up the validation set and the test set.",
            "validation test",
        ),
    ),
    (23, 0): (
        (
            "open_question",
            "Whether dropout or L2 regularization works better against overfitting depends on "
            "the architecture.",
            "dropout regularization",
        ),
    ),
    (25, 0): (
        (
            "definition",
            "K-fold cross-validation splits the training data into k folds and rotates the "
            "validation fold.",
            "cross-validation folds",
        ),
    ),
    (25, 1): (
        (
            "conclusion",
            "Five or ten folds are common choices for k-fold cross-validation.",
            "cross-validation folds",
        ),
    ),
    (1, 0): (
        (
            "definition",
            "快速排序是基于分治的排序算法：先选枢轴，再把数组划分成两部分递归排序。",
            "快速排序 分治 枢轴",
        ),
        ("conclusion", "快速排序的平均时间复杂度是 O(n log n)。", "快速排序 时间复杂度"),
        ("conclusion", "快速排序是稳定排序。", "快速排序 稳定排序"),
    ),
    (2, 0): (
        (
            "condition",
            "当枢轴总是当前区间的最小或最大元素时，快速排序退化为 O(n^2)。",
            "快速排序 退化 枢轴",
        ),
        (
            "misconception",
            "快速排序不是稳定排序：相等元素的相对次序可能改变。",
            "快速排序 稳定排序",
        ),
    ),
    (4, 0): (
        (
            "definition",
            "归并排序把数组拆成两半分别排序，再合并两个有序子数组。",
            "归并排序 分治",
        ),
        ("conclusion", "归并排序的时间复杂度稳定为 O(n log n)。", "归并排序 时间复杂度"),
    ),
    (5, 0): (("conclusion", "数组实现的归并排序需要 O(n) 额外空间。", "归并排序 额外空间"),),
    (6, 0): (
        (
            "definition",
            "枢轴是划分时的比较基准，小于它的放左边，大于它的放右边。",
            "枢轴 划分",
        ),
        ("learner_state", "学习者正在区分枢轴和哨兵的作用。", "枢轴 哨兵"),
    ),
    (3, 0): (
        (
            "conclusion",
            "随机选择枢轴或三数取中可以显著降低快速排序退化的概率。",
            "快速排序 枢轴 三数取中",
        ),
    ),
    (8, 0): (
        (
            "conclusion",
            "归并排序的递推式 T(n)=2T(n/2)+O(n) 的解是 O(n log n)。",
            "归并排序 递推式",
        ),
    ),
    (9, 0): (("open_question", "外部归并时缓冲区大小怎样选择仍待确认。", "外部排序 缓冲区"),),
}
# (old fact, new fact): extracting the second marks the first as superseded.
_SUPERSEDED = ("快速排序是稳定排序。", "快速排序不是稳定排序：相等元素的相对次序可能改变。")


def build_synthetic(path: Path) -> None:
    """Write the deterministic synthetic fixture into a new SQLite file at `path`."""
    from sqlalchemy.engine import URL
    from sqlmodel import Session, SQLModel, create_engine

    import app.db  # noqa: F401 -- per-connection SQLite settings and the search-index hook
    from app.models import KnowledgeTree, Memory, Message, Node

    engine = create_engine(URL.create("sqlite", database=str(path)))
    moment = datetime(2026, 1, 5, 9, 0, 0)

    def tick() -> datetime:
        nonlocal moment
        moment += timedelta(minutes=1)
        return moment

    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            message_id = memory_id = 0
            topics: set[int] = set()
            turns: Counter = Counter()  # Turns written per node so far.
            facts: dict[str, Memory] = {}
            for node_id, tree_id, parent_id, seed, question, answer in _TURNS:
                if tree_id not in topics:
                    topics.add(tree_id)
                    title, archived = _TOPICS[tree_id]
                    session.add(
                        KnowledgeTree(id=tree_id, title=title, archived=archived, created_at=tick())
                    )
                status, partial = _UNFINISHED.get(node_id, ("complete", ""))
                if not turns[node_id]:
                    session.add(
                        Node(
                            id=node_id,
                            tree_id=tree_id,
                            parent_id=parent_id,
                            title=question[:24],
                            title_state="ai",
                            kind="branch" if seed else "followup",
                            seed_text=seed,
                            source_node_id=parent_id if seed else None,
                            learning_note=_NOTES.get(node_id),
                            status=status,
                            created_at=tick(),
                        )
                    )
                message_id += 1
                session.add(
                    Message(
                        id=message_id,
                        node_id=node_id,
                        role="user",
                        content=question,
                        images=[_TINY_PNG] if node_id == _IMAGE_QUESTION_NODE else None,
                        created_at=tick(),
                    )
                )
                message_id += 1
                session.add(
                    Message(
                        id=message_id,
                        node_id=node_id,
                        role="assistant",
                        content=partial if answer is None else answer,
                        status=status,
                        answered_by="Fixture model",
                        created_at=tick(),
                    )
                )
                for fact_type, text, terms in _FACTS.get((node_id, turns[node_id]), ()):
                    memory_id += 1
                    created = tick()
                    row = Memory(
                        id=memory_id,
                        kind="fact",
                        content=text,
                        tree_id=tree_id,
                        source_node_id=node_id,
                        fact_type=fact_type,
                        terms=terms.split(),
                        created_at=created,
                    )
                    session.add(row)
                    facts[text] = row
                    if text == _SUPERSEDED[1]:
                        old = facts[_SUPERSEDED[0]]
                        old.superseded_by, old.updated_at = memory_id, created
                turns[node_id] += 1
            session.commit()
    finally:
        engine.dispose()


# ---------- Reading the copy ----------


@dataclass(frozen=True)
class _Message:
    id: int
    node_id: int
    role: str
    status: str
    content: str
    created_at: datetime | None
    images: int


@dataclass(frozen=True)
class _Fact:
    id: int
    tree_id: int | None
    source_node_id: int | None
    fact_type: str
    superseded_by: int | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class _Question:
    message_id: int
    node_id: int
    tree_id: int
    text: str
    moment: datetime
    path: tuple[int, ...]  # Root first, ending with the question's node.
    thread: tuple[int, ...]  # The node itself, then earlier turns of its follow-up thread.
    parent_id: int | None
    quote: str
    existing: str


@dataclass
class _Snapshot:
    archived: dict[int, bool]
    nodes: dict
    messages: dict[int, list[_Message]]
    facts: list[_Fact]
    questions: list[_Question]
    answered: set[int]  # Nodes with at least one completed answer.
    skipped_empty: int


def _naive(value):
    """Compare stored timestamps as naive UTC; SQLite keeps no zone."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _ancestry(nodes: dict, node) -> list[int]:
    """Ancestor ids root-first within the node's tree, like service.get_ancestors."""
    chain, seen, parent_id = [], {node.id}, node.parent_id
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        parent = nodes.get(parent_id)
        if parent is None or parent.tree_id != node.tree_id:
            break
        chain.append(parent.id)
        parent_id = parent.parent_id
    chain.reverse()
    return chain


def _continues_thread(node) -> bool:
    # A follow-up turn continues its parent's conversation; branches and revisions
    # start a new one (the same grouping the learning map uses for its cards).
    return (
        node.parent_id is not None
        and not (node.seed_text or "").strip()
        and node.kind not in ("branch", "revision")
        and not node.revision_of
    )


def _thread(nodes: dict, node) -> list[int]:
    """The node, then the earlier turns of its follow-up thread, nearest first."""
    result, current = [node.id], node
    while _continues_thread(current):
        parent = nodes.get(current.parent_id)
        if parent is None or parent.tree_id != current.tree_id or parent.id in result:
            break
        result.append(parent.id)
        current = parent
    return result


def load_snapshot(engine) -> _Snapshot:
    from sqlalchemy import case, func
    from sqlmodel import Session, select

    from app.models import KnowledgeTree, Memory, Message, Node

    image_count = case(
        (func.json_valid(Message.images) == 1, func.json_array_length(Message.images)), else_=0
    )
    with Session(engine) as session:
        archived = dict(session.exec(select(KnowledgeTree.id, KnowledgeTree.archived)).all())
        nodes = {node.id: node for node in session.exec(select(Node).order_by(Node.id))}
        messages: dict[int, list[_Message]] = {}
        for row in session.exec(
            select(
                Message.id,
                Message.node_id,
                Message.role,
                Message.status,
                Message.content,
                Message.created_at,
                image_count,
            ).order_by(Message.id)
        ):
            message = _Message(*row[:5], _naive(row[5]), int(row[6] or 0))
            messages.setdefault(message.node_id, []).append(message)
        facts = [
            _Fact(row[0], row[1], row[2], row[3], row[4], _naive(row[5]), _naive(row[6]))
            for row in session.exec(
                select(
                    Memory.id,
                    Memory.tree_id,
                    Memory.source_node_id,
                    Memory.fact_type,
                    Memory.superseded_by,
                    Memory.created_at,
                    Memory.updated_at,
                )
                .where(Memory.kind == "fact")
                .order_by(Memory.id)
            )
        ]
    questions, answered, skipped = [], set(), 0
    for node in nodes.values():
        if node.tree_id not in archived:
            continue
        pairs: list[list] = []
        for message in messages.get(node.id, []):
            if message.role == "user":
                pairs.append([message, None])
            elif pairs:
                pairs[-1][1] = message  # The latest attempt is the active answer.
        path = (*_ancestry(nodes, node), node.id)
        parent = nodes.get(node.parent_id) if node.parent_id is not None else None
        for question, answer in pairs:
            if answer is None or answer.status != "complete":
                continue
            answered.add(node.id)
            if not question.content.strip():
                skipped += 1
                continue
            quote = node.seed_text or ""
            questions.append(
                _Question(
                    message_id=question.id,
                    node_id=node.id,
                    tree_id=node.tree_id,
                    text=question.content,
                    moment=question.created_at or datetime.max,
                    path=path,
                    thread=tuple(_thread(nodes, node)),
                    parent_id=parent.id if parent and parent.tree_id == node.tree_id else None,
                    quote=quote,
                    existing=quote + "\n" + (node.learning_note or ""),
                )
            )
    questions.sort(key=lambda item: (item.moment, item.message_id))
    return _Snapshot(archived, nodes, messages, facts, questions, answered, skipped)


def _index_ready(engine) -> bool:
    from app.search_index import INDEX_TABLE, fts5_available

    with engine.connect() as connection:
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (INDEX_TABLE,)
        ).first()
        return bool(fts5_available(connection) and exists)


def semantic_status(hybrid: bool) -> dict:
    """Readiness of the local semantic models for --hybrid; never downloads weights."""
    if not hybrid:
        return {"state": "lexical", "embedding_ready": False}
    from app.semantic_models import get_backend

    status = get_backend().prepare(allow_download=False)
    if not status.get("embedding_ready"):
        state = status.get("state")
        if state == "not_installed":
            hint = "install them with: uv run python scripts/manage.py retrieval"
        elif state == "disabled":
            hint = "this process runs with MEMORY_RETRIEVAL_MODE=lexical"
        else:
            hint = "prepare the models with: uv run python scripts/manage.py retrieval"
        raise HybridUnavailable(
            f"Hybrid retrieval was requested, but the local semantic models are unavailable "
            f"(state: {state}); {hint}. Nothing was evaluated."
        )
    return status


# ---------- Report ----------


def _database_counts(engine, snapshot: _Snapshot) -> dict:
    statuses = Counter(node.status for node in snapshot.nodes.values())
    active = [fact for fact in snapshot.facts if fact.superseded_by is None]
    types = Counter(fact.fact_type for fact in active)
    return {
        "topics": len(snapshot.archived),
        "archived_topics": sum(snapshot.archived.values()),
        "nodes": len(snapshot.nodes),
        "nodes_by_status": dict(sorted(statuses.items())),
        "messages": sum(len(items) for items in snapshot.messages.values()),
        "completed_questions": len(snapshot.questions),
        "skipped_empty_questions": snapshot.skipped_empty,
        "question_images": sum(
            message.images
            for items in snapshot.messages.values()
            for message in items
            if message.role == "user"
        ),
        "learning_notes": sum(bool(node.learning_note) for node in snapshot.nodes.values()),
        "facts": len(active),
        "superseded_facts": len(snapshot.facts) - len(active),
        "facts_by_type": dict(sorted(types.items())),
        "search_index": "fts5" if _index_ready(engine) else "like",
    }


def evaluate(database: Path, args, status: dict, preloaded: bool) -> dict:
    """Describe the copy at `database`."""
    _check_binding(database, preloaded)
    engine = _engine(database)
    try:
        prepare_copy(engine)
        snapshot = load_snapshot(engine)
        counts = _database_counts(engine, snapshot)
    finally:
        engine.dispose()
    return {
        "scope": "Offline evaluation on a temporary copy that was deleted afterwards.",
        "source": "synthetic fixture" if args.synthetic else "copy of a user database",
        "settings": {
            "k": args.k,
            "scopes": args.scopes,
            "retrieval_mode": "hybrid" if args.hybrid else "lexical",
            "include_text": args.include_text,
        },
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "machine": platform.machine(),
        },
        "database": counts,
        "semantic": {
            "requested": args.hybrid,
            "state": status.get("state"),
            "embedding_ready": bool(status.get("embedding_ready")),
            "reranker_ready": bool(status.get("reranker_ready")),
        },
    }


def _write_report(report: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(partial, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(partial, output)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _scopes(value: str) -> list[str]:
    chosen = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(chosen) - set(SCOPES))
    if not chosen or unknown:
        raise argparse.ArgumentTypeError("choose from path, tree and all, separated by commas")
    return [name for name in SCOPES if name in chosen]


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(
        description="Evaluate memory retrieval, topic search and the token estimator "
        "on a temporary database copy."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", metavar="PATH", help="SQLite file; only a copy is read")
    source.add_argument(
        "--synthetic", action="store_true", help="evaluate the deterministic built-in fixture"
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="use cached local semantic models; exit 2 if hybrid retrieval is not achieved",
    )
    parser.add_argument(
        "--scopes", type=_scopes, default=list(SCOPES), help="comma list of path, tree, all"
    )
    parser.add_argument("--k", type=_positive, default=5, help="cut-off for the proxy metrics")
    parser.add_argument(
        "--include-text",
        action="store_true",
        help="also write questions, search terms and returned fact text (private output)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON report path (default artifacts/retrieval-evaluation.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output.expanduser().resolve()
    source = None
    if args.database:
        source = Path(args.database).expanduser()
        if not source.is_file():
            print("Database file not found.", file=sys.stderr)
            return 1
        source = source.resolve()
        if output.parent == source.parent and output.name.startswith(source.name):
            print("The report must not be written over or beside the database.", file=sys.stderr)
            return 1
    if output.is_dir():
        print("--output names a directory; give a file path.", file=sys.stderr)
        return 1
    preloaded = any(name == "app" or name.startswith("app.") for name in sys.modules)
    try:
        with workspace() as directory:
            database = directory / "evaluation.db"
            if source is not None:
                copy_database(source, database)
            # From here on, any application module that is imported binds to the copy.
            previous = bind_environment(database, args.hybrid)
            try:
                status = semantic_status(args.hybrid)
                if args.synthetic:
                    build_synthetic(database)
                report = evaluate(database, args, status, preloaded)
            finally:
                restore_environment(previous)
    except HybridUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    except EvaluationError as exc:
        print(exc, file=sys.stderr)
        return 1
    _write_report(report, output)
    counts = report["database"]
    print(
        f"Evaluated a temporary database ({report['source']}): {counts['topics']} topics, "
        f"{counts['completed_questions']} completed questions, {counts['facts']} facts."
    )
    print(f"Report: {_display(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
