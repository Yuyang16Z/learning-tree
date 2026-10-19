"""Local lexical relevance shared by memory recall and context compaction.

Tokens are Latin words (dotted, slashed or hyphenated compounds also yield their
parts), Chinese character bigrams and complexity notation such as O(n log n). A
short stopword list drops question words and filler. BM25 is computed within the
candidate set itself, so a term most candidates contain carries little weight and
a long text gains no advantage merely from containing more words.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence

STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "what",
        "why",
        "how",
        "to",
        "of",
        "and",
        "in",
        "it",
        "this",
        "that",
        "please",
        "explain",
        "为什么",
        "什么",
        "为何",
        "怎么",
        "如何",
        "这个",
        "那个",
        "一下",
        "请问",
        "解释",
        "为什",
        "user",
        "users",
        "model",
        "models",
        "data",
        "can",
        "could",
        "should",
        "does",
        "do",
        "not",
        "will",
        "be",
        "on",
        "with",
        "from",
        "for",
        "about",
        "用户",
        "可以",
        "能够",
        "进行",
        "使用",
        "我们",
        "一个",
        "问题",
        "时候",
        "需要",
        "支持",
        "是否",
        "模型",
    }
)
K1 = 1.2
B = 0.75
# A text that holds this many of the question's most distinctive matchable terms,
# once each and at average length, counts as fully relevant.
SATURATION_TERMS = 4


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokens(text: str) -> list[str]:
    """Latin terms plus Chinese bigrams; no whitespace-only tokenization of Chinese."""
    value = normalized(text)
    words = re.findall(r"[a-z0-9_]+(?:[.+/-][a-z0-9_]+)*|[\u3400-\u9fff]+", value)
    result: list[str] = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            parts = [word] if len(word) == 1 else [word[i : i + 2] for i in range(len(word) - 1)]
            result.extend(part for part in parts if part not in STOPWORDS)
        elif word not in STOPWORDS:
            result.append(word)
            result.extend(
                part for part in re.split(r"[.+/-]", word) if part != word and part not in STOPWORDS
            )
    # Preserve common complexity notation as an additional exact term.
    result.extend(re.sub(r"\s+", "", item) for item in re.findall(r"o\([^)]{1,80}\)", value))
    return result


def idf(document_count: int, frequency: int) -> float:
    return math.log(1 + (document_count - frequency + 0.5) / (frequency + 0.5))


def bm25(
    terms: Iterable[str], documents: Sequence[Counter]
) -> tuple[list[float], dict[str, float]]:
    """BM25 of every document for the query terms, with IDF from these documents only.

    Returns the scores and the IDF of each query term that occurs in some document.
    """
    query = set(terms)
    count = len(documents)
    if not query or not count:
        return [0.0] * count, {}
    average = sum(sum(doc.values()) for doc in documents) / count or 1
    frequencies = Counter(term for doc in documents for term in doc if term in query)
    weights = {term: idf(count, frequencies[term]) for term in query if frequencies[term]}
    scores = []
    for doc in documents:
        length, score = sum(doc.values()), 0.0
        for term, weight in weights.items():
            tf = doc[term]
            if tf:
                score += weight * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / average))
        scores.append(score)
    return scores, weights


def relevance(terms: Iterable[str], documents: Sequence[Counter]) -> list[float]:
    """BM25 normalised to 0..1 against the question's most distinctive matchable terms.

    The scale is the score of an average-length text holding the SATURATION_TERMS
    rarest query terms that occur among the candidates, once each. It does not
    depend on how many words the question has, and common terms add little.
    """
    scores, weights = bm25(terms, documents)
    if not weights:
        return [0.0] * len(documents)
    scale = sum(sorted(weights.values(), reverse=True)[:SATURATION_TERMS])
    return [min(1.0, score / scale) if scale > 0 else 0.0 for score in scores]
