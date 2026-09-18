"""Conservative validation and bounded context for user-evidenced preference learning."""

import json
import re
import unicodedata

from sqlmodel import Session, select

from .models import Node, PreferenceProfile, PreferenceSupplement
from .preference_supplements import read_learning_state, supplement_source_valid


def normalize_preference(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def preference_snapshot(session: Session, tree_id: int | None) -> dict:
    """Snapshot everything relevant to a write, including rows omitted from the prompt."""
    profile = session.get(PreferenceProfile, 1, populate_existing=True)
    state = read_learning_state(session)
    rows = session.exec(
        select(PreferenceSupplement)
        .where((PreferenceSupplement.scope == "global") | (PreferenceSupplement.tree_id == tree_id))
        .order_by(PreferenceSupplement.id)
    ).all()
    supplements = []
    for row in rows:
        source = session.get(Node, row.source_node_id) if row.source_node_id else None
        supplements.append(
            row.model_dump(mode="json")
            | {
                "source_valid": supplement_source_valid(session, row),
                "source_identity": (
                    [source.created_at.isoformat(), source.request_id, source.status]
                    if source
                    else None
                ),
            }
        )
    return {
        "profile": profile.model_dump(mode="json") if profile else None,
        "state": state.model_dump(),
        "supplements": supplements,
    }


def extraction_context(snapshot: dict, question: str) -> dict:
    """Keep the authoritative profile whole and bound existing automatic-memory context."""
    if not snapshot["state"]["enabled"]:
        return {"enabled": False, "manual_profile": "", "existing": []}

    def terms(text: str) -> set[str]:
        normalized = normalize_preference(text)
        result = set(re.findall(r"[a-z0-9_]+", normalized))
        for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
            result.update(sequence[i : i + 2] for i in range(max(1, len(sequence) - 1)))
        return result

    question_terms = terms(question)

    def relevance(row: dict) -> tuple[bool, int, int]:
        words = terms(row["content"])
        return row["user_edited"], len(question_terms & words), row["id"]

    rows = sorted(snapshot["supplements"], key=relevance, reverse=True)
    existing, characters = [], 0
    for row in rows:
        if not row["source_valid"]:
            continue
        if len(existing) >= 20:
            break
        item = {key: row[key] for key in ("id", "content", "scope", "status", "user_edited")}
        cost = len(json.dumps(item, ensure_ascii=False))
        if characters + cost <= 8000:
            existing.append(item)
            characters += cost
    profile = snapshot["profile"]
    included_ids = {item["id"] for item in existing}
    omitted = [row for row in rows if row["source_valid"] and row["id"] not in included_ids]
    return {
        "enabled": snapshot["state"]["enabled"],
        "manual_profile": profile["content"] if profile else "",
        "existing": existing,
        "existing_truncated": bool(omitted),
        "manual_context_incomplete": any(row["user_edited"] for row in omitted),
    }


_DURABLE = re.compile(
    r"以后|今后|默认|总是|始终|每次|一贯|习惯|偏好|喜欢|倾向|通常|长期|请记住|记住我|"
    r"\b(?:i\s+(?:prefer|like|usually|always)|by default|from now on|in future|"
    r"always|whenever|every time|remember (?:that )?i|my preference)\b",
    re.I,
)
_TEMPORARY = re.compile(
    r"这次|本次|这轮|本轮|今天|暂时|目前|最近|这道题|这个问题|"
    r"\b(?:this time|for now|today|this question|this answer|temporarily|lately)\b",
    re.I,
)
_REPORTED = re.compile(
    r"他说|她说|你说|他们说|有人说|模型说|AI\s*说|假设|假如|例如|比如|翻译|引用|"
    r"\b(?:he says|she says|they say|you said|for example|suppose|imagine|translate|quote)\b",
    re.I,
)
_QUOTED = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|^[ \t]*>.*$|"
    r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|(?<!\w)\x27[^\x27\n]*\x27(?!\w)',
    re.M,
)


def has_user_evidence(question: str, evidence: str) -> bool:
    """Require a durable first-party statement outside quotations and examples.

    This is a conservative second check, not a semantic classifier. The model must
    still establish that the statement really is the user's lasting preference.
    """
    if not evidence or evidence not in question:
        return False
    if not _DURABLE.search(evidence) or _TEMPORARY.search(evidence) or _REPORTED.search(evidence):
        return False
    quoted_spans = [match.span() for match in _QUOTED.finditer(question)]
    start = 0
    while (start := question.find(evidence, start)) >= 0:
        end = start + len(evidence)
        if not any(start < right and end > left for left, right in quoted_spans):
            # A shorter quote must not evade a reporting prefix in the same sentence.
            prefix = re.split(r"[。！？.!?\n]", question[:start])[-1]
            if not _REPORTED.search(prefix) and not _TEMPORARY.search(prefix):
                return True
        start = end
    return False


def validate_preference(item: object, question: str) -> dict | None:
    """Reject model coercions, ungrounded strings and unsafe update identifiers."""
    if not isinstance(item, dict):
        return None
    content, evidence = item.get("content"), item.get("evidence")
    if (
        not isinstance(content, str)
        or not 1 <= len(content.strip()) <= 800
        or not isinstance(evidence, str)
        or not 1 <= len(evidence.strip()) <= 1200
        or item.get("scope") not in ("global", "topic")
        or item.get("action") not in ("add", "update")
        or type(item.get("conflicts_manual")) is not bool
    ):
        return None
    replacement = item.get("replace_id")
    if replacement is not None and (type(replacement) is not int or replacement <= 0):
        return None
    if not has_user_evidence(question, evidence.strip()):
        return None
    return {
        "content": content.strip(),
        "evidence": evidence.strip(),
        "scope": item["scope"],
        "action": item["action"],
        "replace_id": replacement,
        "conflicts_manual": item["conflicts_manual"],
    }
