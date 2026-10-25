#!/usr/bin/env python3
"""Offline evaluation of memory retrieval, topic search and the token estimator.

Evaluate a temporary copy of your own database, or a deterministic synthetic fixture:

    uv run python scripts/evaluate_retrieval.py --database branch_learning.db
    uv run python scripts/evaluate_retrieval.py --synthetic

The source file is copied with SQLite's backup API from a read-only connection
into a fresh private temporary directory; DATABASE_URL points at that copy before
any application module is imported, app.main is never imported, nothing is
written next to the source, and the copy is deleted afterwards. No model API is
called and no weights are downloaded (--hybrid only loads cached local models).
The JSON report holds ids, counts, scores and timings: no absolute paths and,
unless --include-text is given, no question, answer, fact, note or title text.
Relevance labels are structural proxies, not human judgments, unless --labels
names a file of your own grades (scripts/label_retrieval.py writes one). See
docs/retrieval-evaluation.md.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "artifacts" / "retrieval-evaluation.json"
# Human relevance labels (scripts/label_retrieval.py): ids, grades and text hashes only.
LABELS_FORMAT = "learning-tree-retrieval-labels"
LABELS_VERSION = 1
GRADES = (0, 1, 2)  # Not relevant, related, directly useful.
WORKSPACE_PREFIX = "learning-tree-eval-"
# Scope name -> (include_siblings, include_other_trees), as saved in RetrievalSettings.
SCOPES = {"path": (False, False), "tree": (True, False), "all": (True, True)}
# (context window, answer reserve): the default model settings and a 1M-token window.
POLICIES = ((32768, 4096), (1048576, 4096))
SEARCH_LIMIT = 20  # Default page of GET /trees/{id}/search.
MIN_TERM_CHARS = 3  # Trigram full-text search needs three characters.
MAX_TERM_CHARS = 300  # Query limit of GET /trees/{id}/search.
# superseded_by value that hides a not-yet-extracted fact on the copy; ids start at 1.
HIDDEN = 0
_INVALID_SOURCE_STATES = {"pending", "error", "interrupted"}
_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECARS = ("-wal", "-shm", "-journal")
_ENVIRONMENT = (
    "DATABASE_URL",
    "MEMORY_RETRIEVAL_MODE",
    "DEFAULT_API_KEY",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
)
_CJK_RUN = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")


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
                        old.superseded_by = memory_id  # As the app records it.
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
    content: str  # Kept in memory to match labels as retrieval does; never reported.


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
            _Fact(*row[:5], _naive(row[5]), _naive(row[6]), row[7])
            for row in session.exec(
                select(
                    Memory.id,
                    Memory.tree_id,
                    Memory.source_node_id,
                    Memory.fact_type,
                    Memory.superseded_by,
                    Memory.created_at,
                    Memory.updated_at,
                    Memory.content,
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


# ---------- Statistics ----------


def _percentile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(values, digits: int = 3) -> dict:
    """Count, extremes, mean and linearly interpolated percentiles."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0}
    result = {"count": len(ordered), "min": round(ordered[0], digits)}
    for name, fraction in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99)):
        result[name] = round(_percentile(ordered, fraction), digits)
    result["max"] = round(ordered[-1], digits)
    result["mean"] = round(sum(ordered) / len(ordered), digits)
    return result


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


# ---------- (a) Estimator comparison per learning path ----------


def content_units(message: _Message, measure) -> int:
    """One wire message's content cost as estimate_request_tokens counts it (no role overhead).

    `measure` is text_tokens for the byte estimator or estimate_text_tokens; a question
    with images is a list of a text part and image parts, as context.make_content builds.
    """
    from app.context_budget import IMAGE_TOKEN_RESERVE

    if not message.images:
        return measure(message.content)
    part = {"type": "text", "text": message.content}
    serialized = json.dumps(part, ensure_ascii=False, separators=(",", ":"))
    return measure(serialized) + 16 + message.images * (IMAGE_TOKEN_RESERVE + 64)


def measure_estimator(snapshot: _Snapshot, compaction_ratio: float) -> dict:
    """UTF-8 bytes versus the character-class estimate for every learning path."""
    from app.context import _full_system, context_messages
    from app.context_budget import ContextPolicy, estimate_text_tokens, text_tokens

    policies = [ContextPolicy(window, output, 1.0, compaction_ratio) for window, output in POLICIES]
    units: dict[int, tuple[int, int]] = {}

    def cost(message: _Message) -> tuple[int, int]:
        if message.id not in units:
            units[message.id] = (
                content_units(message, text_tokens),
                content_units(message, estimate_text_tokens),
            )
        return units[message.id]

    paths = []
    for node_id in sorted(snapshot.answered):
        node = snapshot.nodes[node_id]
        ancestors = [
            (snapshot.nodes[item], snapshot.messages.get(item, []))
            for item in _ancestry(snapshot.nodes, node)
        ]
        system = _full_system(ancestors, node, "")
        sent = [
            message
            for _, messages in [*ancestors, (node, snapshot.messages.get(node_id, []))]
            for message in context_messages(messages)
        ]
        text = "\n".join([system, *(message.content for message in sent)])
        # Same structure as estimate_request_tokens: system + 64, then 48 per message.
        request = [text_tokens(system) + 64, estimate_text_tokens(system) + 64]
        for message in sent:
            in_bytes, in_units = cost(message)
            request[0] += 48 + in_bytes
            request[1] += 48 + in_units
        paths.append(
            {
                "node_id": node_id,
                "tree_id": node.tree_id,
                "depth": len(ancestors) + 1,
                "messages": len(sent),
                "images": sum(message.images for message in sent),
                "text_bytes": text_tokens(text),
                "text_estimate": estimate_text_tokens(text),
                "request_bytes": request[0],
                "request_estimate": request[1],
            }
        )
    outcomes = []
    for policy in policies:
        by_bytes = sum(path["request_bytes"] > policy.compaction_budget for path in paths)
        by_units = sum(path["request_estimate"] > policy.compaction_budget for path in paths)
        outcomes.append(
            {
                "window_tokens": policy.window_tokens,
                "output_tokens": policy.output_tokens,
                "input_budget": policy.input_budget,
                "compaction_budget": policy.compaction_budget,
                "would_compact": {"utf8_bytes": by_bytes, "estimate": by_units},
                "would_compact_share": {
                    "utf8_bytes": _share(by_bytes, len(paths)),
                    "estimate": _share(by_units, len(paths)),
                },
            }
        )
    return {
        "definition": (
            "One learning path per node with a completed answer: the system prompt that path "
            "gets plus every completed question and answer from the root, as a follow-up from "
            "that node would carry them, without retrieved memories, document excerpts, tool "
            "definitions or the follow-up itself. text_* measure that text; request_* add the "
            "per-message overheads and image reservations of estimate_request_tokens. "
            "utf8_bytes counts text as the earlier UTF-8 byte estimator did; estimate uses "
            "estimate_text_tokens."
        ),
        "paths": len(paths),
        "text_bytes": summarize((path["text_bytes"] for path in paths), 1),
        "text_estimate": summarize((path["text_estimate"] for path in paths), 1),
        "bytes_per_estimated_unit": summarize(
            (path["text_bytes"] / path["text_estimate"] for path in paths if path["text_estimate"]),
            4,
        ),
        "compaction_ratio": compaction_ratio,
        "policies": outcomes,
        "cases": paths,
    }


# ---------- (c) Topic full-text search ----------


def key_term(question: str) -> str | None:
    """The question's longest CJK run or ASCII word of at least three characters.

    A CJK character weighs as two letters, so "请解释 Transformer" gives "Transformer"
    while "What does 过拟合 mean?" keeps "过拟合"; on a tie the CJK run wins.
    """
    candidates = [
        (len(run) * weight, run)
        for pattern, weight in ((_CJK_RUN, 2), (_WORD, 1))
        for run in pattern.findall(question)
        if len(run) >= MIN_TERM_CHARS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1][:MAX_TERM_CHARS]


def _index_ready(engine) -> bool:
    from app.search_index import INDEX_TABLE, fts5_available

    with engine.connect() as connection:
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (INDEX_TABLE,)
        ).first()
        return bool(fts5_available(connection) and exists)


def measure_search(engine, snapshot: _Snapshot, include_text: bool) -> dict:
    from sqlmodel import Session

    from app.search_index import search_text

    cases, skipped = [], 0
    for question in snapshot.questions:
        term = key_term(question.text)
        if term is None:
            skipped += 1
            continue
        with Session(engine) as session:
            started = time.perf_counter()
            hits = search_text(session, term, tree_id=question.tree_id, limit=SEARCH_LIMIT)
            elapsed = (time.perf_counter() - started) * 1000
        # The question's own message contains the term by construction; count it apart.
        own = next(
            (
                hit.rank
                for hit in hits
                if (hit.kind, hit.ref_id) == ("question", question.message_id)
            ),
            None,
        )
        rank = next(
            (hit.rank for hit in hits if hit.node_id == question.node_id and hit.rank != own),
            None,
        )
        case_ = {
            "question_message_id": question.message_id,
            "node_id": question.node_id,
            "tree_id": question.tree_id,
            "term_chars": len(term),
            "hits": len(hits),
            "by_kind": dict(Counter(hit.kind for hit in hits)),
            "own_question_rank": own,
            "node_found": rank is not None,
            "node_rank": rank,
            "latency_ms": round(elapsed, 3),
        }
        if include_text:
            case_["term"] = term
        cases.append(case_)
    found = sum(case_["node_found"] for case_ in cases)
    return {
        "definition": (
            "For each completed question, the full-text part of the map search (search_text, "
            f"limit {SEARCH_LIMIT}) runs in the question's topic with a key term cut from the "
            f"question itself: its longest CJK run or ASCII word of at least {MIN_TERM_CHARS} "
            "characters, a CJK character weighing as two letters. The question's own message "
            "contains the term, so own_question_found counts the queries that matched it; "
            "node_found and node_rank only count other hits on the question's node (its "
            "answers, facts, note or other turns)."
        ),
        "index": "fts5" if _index_ready(engine) else "like",
        "queries": len(cases),
        "skipped_without_term": skipped,
        "hits": summarize((case_["hits"] for case_ in cases), 1),
        "by_kind": dict(sum((Counter(case_["by_kind"]) for case_ in cases), Counter())),
        "own_question_found": sum(case_["own_question_rank"] is not None for case_ in cases),
        "node_found": found,
        "node_found_share": _share(found, len(cases)),
        "node_rank": summarize(
            (case_["node_rank"] for case_ in cases if case_["node_rank"] is not None), 1
        ),
        "latency_ms": summarize(case_["latency_ms"] for case_ in cases),
        "cases": cases,
    }


# ---------- (b) Memory retrieval with proxy labels ----------


class FactTimeline:
    """Replays, on the copy, which facts existed when each question was asked.

    A fact counts from its created_at; a superseded fact stops counting once the fact
    that replaced it was created. Hidden facts get a temporary superseded_by of 0, which
    retrieval skips; restore() writes the original values back. Questions must be
    visited in time order after reset().

    Every superseded_by write would fire the search index's memory triggers, which drop
    and re-index the fact each time (on an index built before rowid lookups, each firing
    scanned the whole index). Retrieval never reads the index, so the triggers are
    suspended from construction until restore(), which recreates them once every
    original value is back; the index then matches the restored rows again.
    """

    def __init__(self, engine, facts: list[_Fact]) -> None:
        self.engine = engine
        self.original = {fact.id: fact.superseded_by for fact in facts}
        created = {fact.id: fact.created_at for fact in facts}
        self.initial: dict[int, int | None] = {}
        events = []
        for fact in facts:
            retired = None
            if fact.superseded_by is not None:
                retired = created.get(fact.superseded_by) or fact.updated_at
            superseded = fact.superseded_by
            if fact.created_at is None:
                self.initial[fact.id] = superseded if superseded and retired is None else None
                if superseded and retired is not None:
                    events.append((retired, fact.id, superseded))
                continue
            self.initial[fact.id] = HIDDEN
            if superseded is None:
                events.append((fact.created_at, fact.id, None))
            elif retired is None or retired <= fact.created_at:
                events.append((fact.created_at, fact.id, superseded))
            else:
                events.append((fact.created_at, fact.id, None))
                events.append((retired, fact.id, superseded))
        self.events = sorted(events, key=lambda event: (event[0], event[1]))
        self.current = dict(self.original)  # What the copy holds right now.
        self.triggers = self._suspend_index_triggers()
        self.reset()

    def reset(self) -> None:
        self.state = dict(self.initial)
        self.position = 0
        self.dirty = set(self.state)

    def advance(self, moment: datetime) -> None:
        """Show the facts as they were just before `moment`."""
        while self.position < len(self.events) and self.events[self.position][0] < moment:
            _, fact_id, value = self.events[self.position]
            self.state[fact_id] = value
            self.dirty.add(fact_id)
            self.position += 1
        self._write({fact_id: self.state[fact_id] for fact_id in self.dirty})
        self.dirty.clear()

    def visible(self, fact_id: int) -> bool:
        return self.state.get(fact_id, HIDDEN) is None

    def restore(self) -> None:
        self._write(self.original)
        if self.triggers:
            with self.engine.begin() as connection:
                for statement in self.triggers:
                    connection.exec_driver_sql(statement)
            self.triggers = []

    def _suspend_index_triggers(self) -> list[str]:
        """Drop the copy's search-index triggers on memory rows; return their definitions."""
        with self.engine.begin() as connection:
            rows = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'memory' AND name GLOB 'search_*'"
            ).all()
            for name, _ in rows:
                connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        return [statement for _, statement in rows]

    def _write(self, target: dict[int, int | None]) -> None:
        from sqlalchemy import text

        changes = [
            {"value": value, "id": fact_id}
            for fact_id, value in target.items()
            if self.current[fact_id] != value
        ]
        if not changes:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE memory SET superseded_by = :value WHERE id = :id"), changes
            )
        for change in changes:
            self.current[change["id"]] = change["value"]


class _LexicalOnly:
    """A backend that refuses, so retrieve_memory takes its keyword path, as in lexical mode."""

    model_key = "lexical-evaluation"

    def embed_query(self, *args):
        raise RuntimeError("semantic retrieval is not part of this evaluation")

    embed_documents = rerank = embed_query


class _RecordingBackend:
    """Delegates to the local models and counts calls, so silent fallbacks are visible.

    retrieve_memory swallows every semantic failure, and it also rejects invalid reranker
    scores without an exception reaching this wrapper; a reranker call whose result was
    not applied (RetrievalResult.reranked false) is therefore what marks that fallback.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.model_key = inner.model_key
        self.queries = 0
        self.failures = 0
        self.reranks = 0

    def embed_query(self, query):
        self.queries += 1
        try:
            return self.inner.embed_query(query)
        except Exception:
            self.failures += 1
            raise

    def embed_documents(self, documents):
        try:
            return self.inner.embed_documents(documents)
        except Exception:
            self.failures += 1
            raise

    def rerank(self, query, documents):
        self.reranks += 1
        return self.inner.rerank(query, documents)


def semantic_backend(hybrid: bool):
    """The backend to evaluate with and its readiness; never downloads weights.

    --hybrid measures the whole semantic pipeline, so both the embedding model and the
    separately downloaded reranker must be ready.
    """
    if not hybrid:
        return _LexicalOnly(), {"state": "lexical", "embedding_ready": False}
    from app.semantic_models import get_backend

    backend = get_backend()
    status = backend.prepare(allow_download=False)
    missing = [
        name
        for name, key in (("embedding", "embedding_ready"), ("reranking", "reranker_ready"))
        if not status.get(key)
    ]
    if missing:
        state = status.get("state")
        if state == "not_installed":
            hint = "install them with: uv run python scripts/manage.py retrieval"
        elif state == "disabled":
            hint = "this process runs with MEMORY_RETRIEVAL_MODE=lexical"
        else:
            hint = "prepare the models with: uv run python scripts/manage.py retrieval"
        models = "models are" if len(missing) > 1 else "model is"
        raise HybridUnavailable(
            f"Hybrid retrieval was requested, but the local {' and '.join(missing)} {models} "
            f"unavailable (state: {state}); {hint}. Nothing was evaluated."
        )
    return _RecordingBackend(backend), status


def _proxy(entries: list[tuple[int, int, int | None]], k: int, in_prompt: int) -> dict:
    """recall@k, hit rate@k and MRR@k over (found in top k, relevant, first rank).

    in_prompt counts the label facts left out because the prompt already held their text.
    """
    if not entries:
        return {
            "cases": 0,
            "recall_at_k": None,
            "hit_rate_at_k": None,
            "mrr_at_k": None,
            "facts_in_prompt": in_prompt,
        }
    return {
        "cases": len(entries),
        "recall_at_k": round(
            sum(found / min(relevant, k) for found, relevant, _ in entries) / len(entries), 4
        ),
        "hit_rate_at_k": round(sum(found > 0 for found, _, _ in entries) / len(entries), 4),
        "mrr_at_k": round(sum(1 / rank if rank else 0 for _, _, rank in entries) / len(entries), 4),
        "facts_in_prompt": in_prompt,
    }


def _label(ranked: list[str], relevant: set[str], k: int) -> dict:
    top = ranked[:k]
    rank = next((position for position, item in enumerate(top, 1) if item in relevant), None)
    return {"relevant": len(relevant), "found": sum(item in relevant for item in top), "rank": rank}


# ---------- Human relevance labels ----------


def text_digest(text: str) -> str:
    """SHA-256 of a question or fact text: labels notice an edit without storing the text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _unusable(reason: str) -> EvaluationError:
    return EvaluationError(f"The labels file is not usable: {reason}.")


def read_labels(path: Path) -> dict[int, dict]:
    """Grades by question message id: {"question_sha256", "facts": {memory id: {...}}}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise _unusable("it cannot be read as JSON") from None
    if not isinstance(data, dict) or data.get("format") != LABELS_FORMAT:
        raise _unusable("it is not a LearningTree labels file")
    if data.get("version") != LABELS_VERSION or not isinstance(data.get("questions"), dict):
        raise _unusable(f"only version {LABELS_VERSION} is supported")
    labels = {}
    for key, entry in data["questions"].items():
        if (
            not key.isdigit()
            or not isinstance(entry, dict)
            or not isinstance(entry.get("question_sha256"), str)
            or not isinstance(entry.get("facts"), dict)
        ):
            raise _unusable("a question entry is malformed")
        facts = {}
        for fact_key, judged in entry["facts"].items():
            if (
                not fact_key.isdigit()
                or not isinstance(judged, dict)
                or type(judged.get("grade")) is not int
                or judged["grade"] not in GRADES
                or not isinstance(judged.get("fact_sha256"), str)
            ):
                raise _unusable("a grade is malformed (grades are 0, 1 or 2)")
            facts[int(fact_key)] = {"grade": judged["grade"], "fact_sha256": judged["fact_sha256"]}
        labels[int(key)] = {"question_sha256": entry["question_sha256"], "facts": facts}
    return labels


def write_labels(labels: dict[int, dict], path: Path) -> None:
    """Write the grades privately and atomically, as reports are written."""
    payload = {
        "format": LABELS_FORMAT,
        "version": LABELS_VERSION,
        "questions": {
            str(message_id): {
                "question_sha256": entry["question_sha256"],
                "facts": {
                    str(memory_id): judged for memory_id, judged in sorted(entry["facts"].items())
                },
            }
            for message_id, entry in sorted(labels.items())
        },
    }
    _write_report(payload, path)


def current_grades(labels: dict[int, dict], snapshot: _Snapshot) -> tuple[dict, int, int]:
    """Grades whose question and fact texts still match their hashes, by message id.

    A labelled question that is no longer replayed (edited, deleted, or without a
    completed answer) is stale, and so is a grade whose fact was edited or deleted.
    """
    questions = {question.message_id: question for question in snapshot.questions}
    facts = {fact.id: fact for fact in snapshot.facts}
    grades, stale_questions, stale_facts = {}, 0, 0
    for message_id, entry in labels.items():
        question = questions.get(message_id)
        if question is None or text_digest(question.text) != entry["question_sha256"]:
            stale_questions += 1
            continue
        current = {}
        for memory_id, judged in entry["facts"].items():
            fact = facts.get(memory_id)
            if fact is None or text_digest(fact.content) != judged["fact_sha256"]:
                stale_facts += 1
            else:
                current[memory_id] = judged["grade"]
        grades[message_id] = current
    return grades, stale_questions, stale_facts


def _dcg(gains) -> float:
    return sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))


def _graded(ranked: list[str], grades: dict[str, int], k: int) -> dict:
    """One question's top k against human grades of fact texts; an unjudged text gains 0."""
    top = ranked[:k]
    relevant = {text for text, grade in grades.items() if grade > 0}
    first = next((rank for rank, text in enumerate(top, 1) if text in relevant), None)
    ideal = _dcg(sorted((grade for grade in grades.values() if grade > 0), reverse=True)[:k])
    return {
        "returned": len(top),
        "judged": sum(text in grades for text in top),
        "relevant": len(relevant),
        "found": sum(text in relevant for text in top),
        "rank": first,
        "ndcg": round(_dcg([grades.get(text, 0) for text in top]) / ideal, 4) if ideal else None,
    }


def _labelled(entries: list[dict], k: int, *, stale_questions: int, stale_facts: int) -> dict:
    """Means over the labelled questions with a relevant fact; judged share over all."""
    scored = [entry for entry in entries if entry["relevant"]]
    returned = sum(entry["returned"] for entry in entries)

    def mean(values) -> float | None:
        values = list(values)
        return round(sum(values) / len(values), 4) if values else None

    return {
        "questions": len(entries),
        "with_relevant": len(scored),
        "precision_at_k": mean(entry["found"] / k for entry in scored),
        "recall_at_k": mean(entry["found"] / min(entry["relevant"], k) for entry in scored),
        "mrr_at_k": mean(1 / entry["rank"] if entry["rank"] else 0 for entry in scored),
        "ndcg_at_k": mean(entry["ndcg"] for entry in scored),
        "judged_at_k": (
            round(sum(entry["judged"] for entry in entries) / returned, 4) if returned else None
        ),
        "stale_questions": stale_questions,
        "stale_facts": stale_facts,
    }


def _read_scope_row(engine):
    from sqlmodel import Session

    from app.models import RetrievalSettings

    with Session(engine) as session:
        row = session.get(RetrievalSettings, 1)
        return (
            None if row is None else (row.include_siblings, row.include_other_trees, row.revision)
        )


def _write_scope_row(engine, values) -> None:
    """Save RetrievalSettings on the copy: (siblings, other topics, revision) or None."""
    from sqlmodel import Session

    from app.models import RetrievalSettings

    with Session(engine) as session:
        row = session.get(RetrievalSettings, 1)
        if values is None:
            if row is not None:
                session.delete(row)
        else:
            row = row or RetrievalSettings(id=1)
            row.include_siblings, row.include_other_trees, row.revision = values
            session.add(row)
        session.commit()


def measure_retrieval(
    engine,
    snapshot: _Snapshot,
    scopes: list[str],
    k: int,
    backend,
    include_text: bool,
    grades: dict[int, dict[int, int]] | None = None,
    stale: tuple[int, int] = (0, 0),
) -> dict:
    from sqlmodel import Session

    from app.retrieval import (
        RetrievalConfig,
        _normalized,
        read_retrieval_settings,
        retrieve_memory,
    )

    valid_nodes = {
        node_id
        for node_id, node in snapshot.nodes.items()
        if node.status not in _INVALID_SOURCE_STATES
    }
    by_node: dict[int, list[_Fact]] = {}
    for fact in snapshot.facts:
        source = snapshot.nodes.get(fact.source_node_id)
        if source is not None and source.id in valid_nodes and source.tree_id == fact.tree_id:
            by_node.setdefault(fact.source_node_id, []).append(fact)
    fact_types = {fact.id: fact.fact_type for fact in snapshot.facts}
    fact_texts = {fact.id: fact.content for fact in snapshot.facts}
    timeline = FactTimeline(engine, snapshot.facts)
    graded_texts: dict[int, dict[str, int]] = {}
    saved_scope = _read_scope_row(engine)
    labels: dict[int, tuple[list, tuple | None]] = {}
    cases, summaries = [], {}
    recording = isinstance(backend, _RecordingBackend)

    def label(node_id: int, existing: str) -> tuple[int, set[str], int]:
        """A node's facts visible now, compared as retrieve_memory compares them.

        Its _deduplicate keeps one fact per normalized text and drops every fact whose text
        the question's quote or note already holds. A label is therefore a set of distinct
        normalized texts, and facts already in the prompt are left out and counted.
        """
        texts, in_prompt = set(), 0
        for fact in by_node.get(node_id, ()):
            content = _normalized(fact.content)
            if not content or not timeline.visible(fact.id):
                continue
            if existing and content in existing:
                in_prompt += 1
            else:
                texts.add(content)
        return node_id, texts, in_prompt

    def text_grades(message_id: int, existing: str) -> dict[str, int]:
        """A question's human grades by normalized text, compared as its proxy labels are:
        the best grade of each text, leaving out facts that did not exist yet or whose
        text the prompt already holds, since retrieval never returns those."""
        result: dict[str, int] = {}
        for memory_id, grade in grades[message_id].items():
            content = _normalized(fact_texts.get(memory_id, ""))
            if not content or not timeline.visible(memory_id) or (existing and content in existing):
                continue
            result[content] = max(grade, result.get(content, 0))
        return result

    def run(session, question, config):
        return retrieve_memory(
            session,
            question.tree_id,
            list(question.path),
            question.text,
            quoted_text=question.quote,
            existing_text=question.existing,
            config=config,
            backend=backend,
        )

    try:
        for scope in scopes:
            siblings, others = SCOPES[scope]
            _write_scope_row(engine, (siblings, others, uuid4().hex))
            timeline.reset()
            warmed = False
            latencies, returned, modes, types = [], Counter(), Counter(), Counter()
            sizes, self_entries, parent_entries, labelled_entries = [], [], [], []
            reranked = eligible = fallbacks = rerank_calls = rerank_fallbacks = 0
            self_in_prompt = parent_in_prompt = 0
            for question in snapshot.questions:
                timeline.advance(question.moment)
                if question.message_id not in labels:
                    existing = _normalized(question.existing)
                    thread = [label(node_id, existing) for node_id in question.thread]
                    parent = None
                    if question.parent_id is not None:
                        parent = label(question.parent_id, existing)
                    labels[question.message_id] = (thread, parent)
                    if grades and question.message_id in grades:
                        graded_texts[question.message_id] = text_grades(
                            question.message_id, existing
                        )
                thread, parent = labels[question.message_id]
                with Session(engine) as session:
                    # The same settings read and config that service.fetch_memory_note uses.
                    stored = read_retrieval_settings(session)
                    config = RetrievalConfig(
                        include_siblings=stored.include_siblings,
                        include_other_trees=stored.include_other_trees,
                    )
                    if not warmed:
                        run(session, question, config)  # Untimed warm-up per scope.
                        warmed = True
                    if recording:
                        calls = (backend.queries, backend.failures, backend.reranks)
                    started = time.perf_counter()
                    result = run(session, question, config)
                    elapsed = (time.perf_counter() - started) * 1000
                ranked = [item.memory_id for item in result.facts]
                origins = Counter(item.scope for item in result.facts)
                case_ = {
                    "scope": scope,
                    "question_message_id": question.message_id,
                    "node_id": question.node_id,
                    "tree_id": question.tree_id,
                    "returned": {name: origins.get(name, 0) for name in ("path", "tree", "other")},
                    "memory_ids": ranked,
                    "mode": result.mode,
                    "reranked": result.reranked,
                    "latency_ms": round(elapsed, 3),
                    "self": [],
                    "parent": None,
                }
                if recording:
                    # Reranking only follows a successful embedding stage, and an empty
                    # candidate list legitimately skips it, so only a reranker call whose
                    # result was not applied is a reranking fallback.
                    tried = backend.queries > calls[0]
                    rerank_called = backend.reranks > calls[2]
                    rerank_failed = rerank_called and not result.reranked
                    embedding_failed = tried and (
                        backend.failures > calls[1] or result.mode != "hybrid"
                    )
                    eligible += tried
                    fallbacks += embedding_failed or rerank_failed
                    rerank_calls += rerank_called
                    rerank_fallbacks += rerank_failed
                texts = [_normalized(item.content) for item in result.facts]
                for node_id, relevant, in_prompt in thread:
                    self_in_prompt += in_prompt
                    if relevant:
                        score = _label(texts, relevant, k)
                        case_["self"].append(
                            {"source_node_id": node_id, **score, "in_prompt": in_prompt}
                        )
                        self_entries.append((score["found"], score["relevant"], score["rank"]))
                if parent:
                    node_id, relevant, in_prompt = parent
                    parent_in_prompt += in_prompt
                    if relevant:
                        score = _label(texts, relevant, k)
                        case_["parent"] = {
                            "source_node_id": node_id,
                            **score,
                            "in_prompt": in_prompt,
                        }
                        parent_entries.append((score["found"], score["relevant"], score["rank"]))
                if question.message_id in graded_texts:
                    score = _graded(texts, graded_texts[question.message_id], k)
                    case_["labelled"] = score
                    labelled_entries.append(score)
                if include_text:
                    case_["question"] = question.text
                    case_["facts"] = [item.content for item in result.facts]
                cases.append(case_)
                latencies.append(elapsed)
                sizes.append(len(ranked))
                returned.update(origins)
                modes[result.mode] += 1
                reranked += result.reranked
                types.update(fact_types.get(memory_id, "conclusion") for memory_id in ranked)
            summaries[scope] = {
                "queries": len(snapshot.questions),
                "with_facts": sum(size > 0 for size in sizes),
                "returned": {name: returned.get(name, 0) for name in ("path", "tree", "other")},
                "returned_per_query": summarize(sizes, 2),
                "by_fact_type": dict(sorted(types.items())),
                "modes": dict(modes),
                "reranked": reranked,
                "semantic_attempts": eligible,
                "semantic_fallbacks": fallbacks,
                "rerank_attempts": rerank_calls,
                "rerank_fallbacks": rerank_fallbacks,
                "latency_ms": summarize(latencies),
                "self_recall": _proxy(self_entries, k, self_in_prompt),
                "parent_recall": _proxy(parent_entries, k, parent_in_prompt),
            }
            if grades is not None:
                summaries[scope]["labelled"] = _labelled(
                    labelled_entries, k, stale_questions=stale[0], stale_facts=stale[1]
                )
    finally:
        timeline.restore()
        _write_scope_row(engine, saved_scope)
    return {
        "definition": (
            "Every completed question is replayed through retrieve_memory with its node's "
            "learning path, quote and note, against the facts that existed when it was asked "
            "(created earlier and not yet superseded), once per scope with RetrievalSettings "
            "saved on the copy. Rank is the position in the rendered fact list (path facts, "
            "then other branches, then other topics). Labels are structural proxies, not human "
            "relevance judgments: self = facts from the question's own node and the earlier "
            "turns of its follow-up thread, scored once per source node; parent = facts from "
            "the parent node. Labels compare facts by normalized text, as retrieve_memory "
            "deduplicates them: facts with the same text count once, and facts whose text the "
            "question's quote or note already holds are left out (in_prompt, facts_in_prompt), "
            "since retrieval never returns them. recall_at_k = relevant texts in the top k / "
            "min(k, relevant); "
            "hit_rate_at_k = share with at least one in the top k; mrr_at_k = mean of 1/rank "
            "of the first relevant fact within the top k. Latency times the retrieve_memory "
            "call after one untimed warm-up per scope. With a labels file, labelled scores "
            "the questions you graded (0 not relevant, 1 related, 2 directly useful) by "
            "normalized text in the same way: precision_at_k = relevant in the top k / k, "
            "recall_at_k as above, mrr_at_k, and ndcg_at_k with gain 2^grade - 1 and "
            "discount log2(rank + 1) against the ideal order of the graded facts, each "
            "averaged over graded questions with a relevant fact; judged_at_k = graded "
            "texts among all returned in the top k. Unjudged facts count as not relevant."
        ),
        "k": k,
        "scopes": summaries,
        "cases": cases,
    }


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


def evaluate(
    database: Path, args, backend, status: dict, preloaded: bool, labels: dict | None = None
) -> dict:
    """All measurements, on the copy at `database`."""
    from app.config import settings

    _check_binding(database, preloaded)
    engine = _engine(database)
    try:
        prepare_copy(engine)
        snapshot = load_snapshot(engine)
        grades, stale = None, (0, 0)
        if labels is not None:
            grades, *stale = current_grades(labels, snapshot)
        started = time.perf_counter()
        estimator = measure_estimator(snapshot, settings.context_compaction_ratio)
        search_started = time.perf_counter()
        search = measure_search(engine, snapshot, args.include_text)
        retrieval_started = time.perf_counter()
        retrieval = measure_retrieval(
            engine, snapshot, args.scopes, args.k, backend, args.include_text, grades, tuple(stale)
        )
        finished = time.perf_counter()
        counts = _database_counts(engine, snapshot)
    finally:
        engine.dispose()
    scopes = retrieval["scopes"].values()
    attempts = sum(item["semantic_attempts"] for item in scopes)
    fallbacks = sum(item["semantic_fallbacks"] for item in scopes)
    ready = bool(status.get("embedding_ready") and status.get("reranker_ready"))
    semantic = {
        "requested": args.hybrid,
        "state": status.get("state"),
        "embedding_ready": bool(status.get("embedding_ready")),
        "reranker_ready": bool(status.get("reranker_ready")),
        "semantic_attempts": attempts,
        "rerank_attempts": sum(item["rerank_attempts"] for item in scopes),
        "hybrid_cases": sum(item["modes"].get("hybrid", 0) for item in scopes),
        "fallback_cases": fallbacks,
        "rerank_fallbacks": sum(item["rerank_fallbacks"] for item in scopes),
        # Both models ran for at least one question and no retrieval fell back from either.
        "achieved": bool(args.hybrid and ready and attempts and not fallbacks),
    }
    if grades is None:
        relevance = "Relevance labels are structural proxies, not human judgments"
    else:
        relevance = (
            f"Human relevance labels from a labels file score {len(grades)} of the replayed "
            "questions, beside the structural proxies"
        )
    report = {
        "scope": (
            f"Offline evaluation on a temporary copy that was deleted afterwards. {relevance}; "
            "no model API was called and nothing was downloaded."
        ),
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
        "semantic": semantic,
        "timings_s": {
            "estimator": round(search_started - started, 3),
            "search": round(retrieval_started - search_started, 3),
            "retrieval": round(finished - retrieval_started, 3),
        },
        "estimator": estimator,
        "full_text_search": search,
        "memory_retrieval": retrieval,
    }
    if grades is not None:
        report["labels"] = {
            "questions": len(grades),
            "graded_facts": sum(len(facts) for facts in grades.values()),
            "stale_questions": stale[0],
            "stale_facts": stale[1],
        }
    return report


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False  # Either path is missing or unreadable.


def _fold(name: str) -> str:
    """Case-folded, canonically decomposed: how case- and normalization-insensitive file
    systems such as APFS compare names."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _overwrites_database(output: Path, source: Path) -> bool:
    """Whether writing the report at `output` could replace the database or a file beside it.

    Files and directories are compared by identity, so case variants and aliases such as
    macOS firmlinks are caught. Names in the database's own directory are compared
    caselessly, which also covers -wal, -shm and -journal files that do not exist yet.
    """
    if _same_file(output, source):
        return True
    return _same_file(output.parent, source.parent) and _fold(output.name).startswith(
        _fold(source.name)
    )


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
        help="use the cached local embedding and reranking models; exit 2 if either is "
        "unavailable, any retrieval falls back from either, or no question reaches them",
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
        "--labels",
        type=Path,
        metavar="FILE",
        help="score retrieval against your own grades too (written by scripts/label_retrieval.py)",
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
        if _overwrites_database(output, source):
            print("The report must not be written over or beside the database.", file=sys.stderr)
            return 1
    if output.is_dir():
        print("--output names a directory; give a file path.", file=sys.stderr)
        return 1
    labels = None
    if args.labels is not None:
        if not args.labels.expanduser().is_file():
            print("Labels file not found.", file=sys.stderr)
            return 1
        try:
            labels = read_labels(args.labels.expanduser())
        except EvaluationError as exc:
            print(exc, file=sys.stderr)
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
                backend, status = semantic_backend(args.hybrid)
                if args.synthetic:
                    build_synthetic(database)
                report = evaluate(database, args, backend, status, preloaded, labels)
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
        f"Evaluated a temporary database ({report['source']}): "
        f"{counts['completed_questions']} completed questions, "
        f"{report['estimator']['paths']} learning paths, scopes {','.join(args.scopes)}, "
        f"{report['settings']['retrieval_mode']} retrieval."
    )
    if "labels" in report:
        graded = report["labels"]["questions"]
        print(f"Report: {_display(output)} (your grades for {graded} questions, and proxy labels).")
    else:
        print(f"Report: {_display(output)} (proxy labels, not human relevance judgments).")
    semantic = report["semantic"]
    if args.hybrid and not semantic["achieved"]:
        if not semantic["semantic_attempts"]:
            reason = "no question had facts to rank, so the local models were never used"
        else:
            reason = (
                f"{semantic['fallback_cases']} of {semantic['semantic_attempts']} retrievals "
                f"fell back, {semantic['rerank_fallbacks']} of them at the reranking stage"
            )
        print(
            f"Hybrid retrieval was requested but not achieved: {reason}. See the semantic "
            "section of the report.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
