#!/usr/bin/env python3
"""Grade which memory facts are relevant to your own questions, for evaluate_retrieval.py.

    uv run python scripts/label_retrieval.py --database branch_learning.db
    uv run python scripts/evaluate_retrieval.py --database branch_learning.db \\
        --labels artifacts/retrieval-labels.json

Like scripts/evaluate_retrieval.py it reads a temporary copy of the database (or the
synthetic fixture), calls no model API and downloads nothing. Every completed question
is replayed against the facts that existed when it was asked. Its candidates are pooled
from several retrieval settings (keyword retrieval and, with --hybrid, the local semantic
models, in each scope, to --depth) and the facts of its own and parent nodes, up to
--pool facts. Each question with candidates not graded yet is shown in this terminal;
grade each fact 2 (directly useful for answering), 1 (related) or 0 (not relevant).
Grades are saved after every question as ids, grades and SHA-256 hashes of the question
and fact texts, never the texts. A later run asks only for facts without a current
grade, including facts whose text changed since. See docs/retrieval-evaluation.md.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_retrieval as ev  # noqa: E402 -- imports no application module

DEFAULT_LABELS = ROOT / "artifacts" / "retrieval-labels.json"
PROMPT = (
    'Grades for facts 1-{count} (e.g. "1 3=1": listed = 2, n=1 related; '
    "Enter = none relevant, s = skip, q = save and quit): "
)
HELP = (
    "Type the numbers of the relevant facts, each graded 2 (directly useful) unless written "
    "n=1 (related) or n=0; every fact not listed is graded 0. s skips this question, "
    "q saves and stops."
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _interleave(lists: list[list[int]], size: int, text_of) -> list[int]:
    """Round-robin over ranked id lists, one fact per normalized text, at most `size`."""
    pool, texts, depth = [], set(), max((len(items) for items in lists), default=0)
    for rank in range(depth):
        for items in lists:
            if rank < len(items) and len(pool) < size:
                text = text_of(items[rank])
                if text and text not in texts:
                    texts.add(text)
                    pool.append(items[rank])
    return pool


def build_pools(engine, snapshot, scopes, backends, depth: int, size: int) -> dict[int, list[int]]:
    """Candidate fact ids per question message id, most likely relevant first.

    Each question sees the facts that existed, unsuperseded, when it was asked. Its own
    and parent nodes' facts lead, then the ranked lists of every scope and backend at
    `depth`, interleaved by rank; facts whose text the quote or note already holds are
    left out, since retrieval never returns them.
    """
    from sqlmodel import Session

    from app.retrieval import RetrievalConfig, _normalized, retrieve_memory

    text_of = {fact.id: _normalized(fact.content) for fact in snapshot.facts}.get
    by_node: dict[int, list] = {}
    for fact in snapshot.facts:
        by_node.setdefault(fact.source_node_id, []).append(fact)
    ranked: dict[int, list[list[int]]] = {}
    structural: dict[int, list[int]] = {}
    timeline = ev.FactTimeline(engine, snapshot.facts)
    try:
        for scope in scopes:
            siblings, others = ev.SCOPES[scope]
            config = RetrievalConfig(
                top_k=depth,
                candidate_k=max(24, 3 * depth),
                char_budget=1_000_000,
                include_siblings=siblings,
                include_other_trees=others,
                wider_top_k=depth,
                wider_char_budget=1_000_000,
                wider_candidate_k=max(48, 3 * depth),
            )
            timeline.reset()
            for question in snapshot.questions:
                timeline.advance(question.moment)
                lists = ranked.setdefault(question.message_id, [])
                with Session(engine) as session:
                    for backend in backends:
                        result = retrieve_memory(
                            session,
                            question.tree_id,
                            list(question.path),
                            question.text,
                            quoted_text=question.quote,
                            existing_text=question.existing,
                            config=config,
                            backend=backend,
                        )
                        lists.append([item.memory_id for item in result.facts])
                if question.message_id not in structural:
                    existing = _normalized(question.existing)
                    nearby = [*question.thread]
                    if question.parent_id is not None:
                        nearby.append(question.parent_id)
                    structural[question.message_id] = [
                        fact.id
                        for node_id in nearby
                        for fact in by_node.get(node_id, ())
                        if timeline.visible(fact.id)
                        and text_of(fact.id)
                        and not (existing and text_of(fact.id) in existing)
                    ]
    finally:
        timeline.restore()
    return {
        question.message_id: _interleave(
            [structural.get(question.message_id, []), *ranked.get(question.message_id, [])],
            size,
            text_of,
        )
        for question in snapshot.questions
    }


def _graded_ids(labels: dict, question, facts: dict) -> set[int]:
    """Facts of a question whose grade still matches both texts."""
    entry = labels.get(question.message_id)
    if not entry or entry["question_sha256"] != ev.text_digest(question.text):
        return set()
    return {
        memory_id
        for memory_id, judged in entry["facts"].items()
        if memory_id in facts and judged["fact_sha256"] == ev.text_digest(facts[memory_id].content)
    }


def record(labels: dict, question, grades: dict[int, int], facts: dict) -> None:
    """Store grades of one question; an edited question starts a new entry."""
    digest = ev.text_digest(question.text)
    entry = labels.get(question.message_id)
    if not entry or entry["question_sha256"] != digest:
        entry = {"question_sha256": digest, "facts": {}}
    for memory_id, grade in grades.items():
        entry["facts"][memory_id] = {
            "grade": grade,
            "fact_sha256": ev.text_digest(facts[memory_id].content),
        }
    labels[question.message_id] = entry


def parse_answer(text: str, count: int):
    """Grades by candidate number (unlisted ones 0), "skip", "quit", or None if invalid."""
    words = text.strip().lower().split()
    if words in (["q"], ["quit"]):
        return "quit"
    if words in (["s"], ["skip"]):
        return "skip"
    grades = {}
    for word in words:
        number, separator, grade = word.partition("=")
        if not number.isdigit() or not 1 <= int(number) <= count:
            return None
        if separator and grade not in ("0", "1", "2"):
            return None
        grades[int(number)] = int(grade) if separator else 2
    return grades


def label(database: Path, args, labels: dict, labels_path: Path, backend, preloaded: bool) -> None:
    ev._check_binding(database, preloaded)
    engine = ev._engine(database)
    try:
        ev.prepare_copy(engine)
        snapshot = ev.load_snapshot(engine)
        backends = [ev._LexicalOnly()] + ([backend] if args.hybrid else [])
        pools = build_pools(engine, snapshot, args.scopes, backends, args.depth, args.pool)
    finally:
        engine.dispose()
    facts = {fact.id: fact for fact in snapshot.facts}
    todo = []
    judged = candidates = 0
    for question in snapshot.questions:
        pool = pools.get(question.message_id) or []
        current = _graded_ids(labels, question, facts)
        candidates += len(pool)
        judged += sum(memory_id in current for memory_id in pool)
        missing = [memory_id for memory_id in pool if memory_id not in current]
        if missing:
            todo.append((question, missing))
    with_candidates = sum(bool(pools.get(question.message_id)) for question in snapshot.questions)
    print(
        f"{len(snapshot.questions)} completed questions, {with_candidates} questions with "
        f"candidates; judged {judged} of {candidates} candidate facts, {len(todo)} questions "
        "to grade."
    )
    if args.status:
        return
    saved = 0
    for index, (question, missing) in enumerate(todo, 1):
        print(
            f"\n[{index}/{len(todo)}] Question {question.message_id}: {_clip(question.text, 600)}"
        )
        if question.quote.strip():
            print(f"  Quoted passage: {_clip(question.quote, 300)}")
        for number, memory_id in enumerate(missing, 1):
            fact = facts[memory_id]
            print(f"  {number}. {_clip(fact.content, 400)}  [{fact.fact_type}]")
        while (
            answer := parse_answer(input(PROMPT.format(count=len(missing))), len(missing))
        ) is None:
            print(HELP)
        if answer == "quit":
            break
        if answer == "skip":
            continue
        grades = {memory_id: answer.get(number, 0) for number, memory_id in enumerate(missing, 1)}
        record(labels, question, grades, facts)
        ev.write_labels(labels, labels_path)
        saved += 1
    print(f"\nSaved grades for {saved} questions to {ev._display(labels_path)}.")


def parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(
        description="Grade the relevance of memory facts to your own questions, "
        "on a temporary database copy, for scripts/evaluate_retrieval.py --labels."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", metavar="PATH", help="SQLite file; only a copy is read")
    source.add_argument(
        "--synthetic", action="store_true", help="grade the deterministic built-in fixture"
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help="grades file to extend (default artifacts/retrieval-labels.json)",
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="also pool candidates from the cached local embedding and reranking models",
    )
    parser.add_argument(
        "--scopes", type=ev._scopes, default=list(ev.SCOPES), help="comma list of path, tree, all"
    )
    parser.add_argument(
        "--depth", type=ev._positive, default=10, help="facts taken from each ranked list"
    )
    parser.add_argument(
        "--pool", type=ev._positive, default=12, help="most candidates shown per question"
    )
    parser.add_argument(
        "--status", action="store_true", help="only report how much is graded; ask nothing"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    labels_path = args.labels.expanduser().resolve()
    source = None
    if args.database:
        source = Path(args.database).expanduser()
        if not source.is_file():
            print("Database file not found.", file=sys.stderr)
            return 1
        source = source.resolve()
        if ev._overwrites_database(labels_path, source):
            print("The labels must not be written over or beside the database.", file=sys.stderr)
            return 1
    if labels_path.is_dir():
        print("--labels names a directory; give a file path.", file=sys.stderr)
        return 1
    try:
        labels = ev.read_labels(labels_path) if labels_path.exists() else {}
    except ev.EvaluationError as exc:
        print(exc, file=sys.stderr)
        return 1
    preloaded = any(name == "app" or name.startswith("app.") for name in sys.modules)
    try:
        with ev.workspace() as directory:
            database = directory / "labelling.db"
            if source is not None:
                ev.copy_database(source, database)
            # From here on, any application module that is imported binds to the copy.
            previous = ev.bind_environment(database, args.hybrid)
            try:
                backend, _ = ev.semantic_backend(args.hybrid)
                if args.synthetic:
                    ev.build_synthetic(database)
                label(database, args, labels, labels_path, backend, preloaded)
            finally:
                ev.restore_environment(previous)
    except ev.HybridUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    except ev.EvaluationError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped; grades already saved are kept.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
