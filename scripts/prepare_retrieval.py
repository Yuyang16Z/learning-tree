#!/usr/bin/env python3
"""Download pinned public retrieval models and optionally verify local inference.

Run: uv run python scripts/prepare_retrieval.py --smoke
The smoke uses fixed synthetic examples only and never opens the chat database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.semantic_models import get_backend, prepare


def smoke() -> dict:
    backend = get_backend()
    cases = [
        (
            "为什么训练集表现很好，测试集却很差？",
            [
                "过拟合是模型过度拟合训练样本，导致在未见数据上的泛化能力下降。",
                "月球绕地球公转，海洋潮汐受到月球引力影响。",
            ],
        ),
        (
            "Why does a model score well on training data but poorly on unseen data?",
            [
                "Overfitting means learning training-specific patterns that do not generalize.",
                "An oven preheats before baking bread.",
            ],
        ),
    ]
    results = []
    for query, documents in cases:
        vector = backend.embed_query(query)
        vectors = backend.embed_documents(documents)
        similarities = [sum(a * b for a, b in zip(vector, item)) for item in vectors]
        scores = backend.rerank(query, documents)
        if similarities[0] <= similarities[1] or scores[0] <= scores[1]:
            raise RuntimeError("Synthetic relevant-document ranking failed")
        if abs(sum(value * value for value in vector) - 1) > 0.001:
            raise RuntimeError("Embedding normalization failed")
        results.append(
            {"dimension": len(vector), "embedding_scores": similarities, "rerank_scores": scores}
        )
    return {"passed": True, "synthetic_cases": len(cases), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke", action="store_true", help="Run Chinese and English local examples"
    )
    args = parser.parse_args()
    status = prepare()
    print(json.dumps({"status": status}, ensure_ascii=False))
    if status["state"] == "disabled":
        return 0
    if not status["embedding_ready"] or not status["reranker_ready"]:
        print("Local models are unavailable. Check network access and retry preparation.")
        return 1
    if args.smoke:
        try:
            print(json.dumps({"smoke": smoke()}, ensure_ascii=False))
        except Exception:
            print("Local model inference verification failed.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
