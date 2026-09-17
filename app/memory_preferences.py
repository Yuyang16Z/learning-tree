"""Preference profile migration reads and stable optimistic concurrency tokens."""

import hashlib
import json
import re
import unicodedata

from sqlalchemy import text
from sqlmodel import Session, select

from .models import Memory, PreferenceProfile
from .schemas import PreferenceProfileOut

PROFILE_HEADING = "【用户编辑的偏好（当前明确要求优先）】"
PROFILE_END = "【用户偏好结束】"


def begin_memory_write(session: Session) -> None:
    """Serialize read-modify-write operations, including the singleton's first save."""
    session.execute(text("BEGIN IMMEDIATE"))


def read_preferences(session: Session) -> PreferenceProfileOut:
    profile = session.get(PreferenceProfile, 1, populate_existing=True)
    if profile is not None:
        return PreferenceProfileOut(
            content=profile.content, revision=profile.revision, managed=True
        )
    rows = session.exec(select(Memory).where(Memory.kind == "preference").order_by(Memory.id)).all()
    seen, paragraphs = set(), []
    for row in rows:
        content = row.content.strip()
        normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", content)).casefold()
        if normalized and normalized not in seen:
            paragraphs.append(content)
            seen.add(normalized)
    # Include IDs and original contents, not only the deduplicated display text.
    encoded = json.dumps([(row.id, row.content) for row in rows], ensure_ascii=False)
    revision = "legacy-" + hashlib.sha256(encoded.encode()).hexdigest()
    return PreferenceProfileOut(content="\n\n".join(paragraphs), revision=revision, managed=False)


def render_profile(content: str) -> str:
    if not content:
        return ""
    return f"{PROFILE_HEADING}\n{content}\n{PROFILE_END}"


def split_profile(note: str) -> tuple[str, str]:
    """Extract the user-owned block as one unit when budgeting context."""
    start = note.find(PROFILE_HEADING + "\n")
    end = note.rfind("\n" + PROFILE_END)
    if start < 0 or end < start:
        return "", note
    end += len("\n" + PROFILE_END)
    return note[start:end], (note[:start] + note[end:]).strip()
