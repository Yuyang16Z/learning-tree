"""Learning-style presets: how the tutor teaches the questions of a topic.

Built-in styles ship as app/learning_presets/*.md. Learners add their own as files in
learning-presets/ at the project root (LEARNING_PRESETS_DIR; the Docker image uses
/data/learning-presets). A file holds a settings block between its first two `---`
lines and then the instruction, which ends the system prompt of every answer asked
with that style (app/context.py). Custom ids are `custom:<file name>`, so a custom file
never shadows a built-in. A custom file that cannot be used is reported by its file
name and a fixed reason; its contents and the directory's path are never echoed.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from fastapi import APIRouter

from .config import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_DIRECTORY = Path(__file__).resolve().with_name("learning_presets")
CUSTOM_PREFIX = "custom:"
MAX_VALUE_CHARS = 120
MAX_INSTRUCTION_CHARS = 4000
MAX_CUSTOM_FILES = 50
MAX_FILE_BYTES = 16 * 1024
SETTING_KEYS = ("title_zh", "title_en", "description_zh", "description_en", "order")
# The only file names the loader reads; other entries in the directory are ignored.
FILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\.md")
# A well-formed stored id: a built-in name, or custom: and a custom file's name.
PRESET_ID = re.compile(r"(?:custom:)?[a-z0-9][a-z0-9-]{0,47}")
_ORDER = re.compile(r"[+-]?\d{1,9}")
# Never follow a symlink, never wait on a FIFO swapped in after listing.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)

# Fixed, user-facing reasons (paired with English in web/src/i18n/workspace.ts).
UNKNOWN_PRESET = "这个学习方式不存在或已不可用，请重新选择。"
TOO_LARGE = "文件超过 16 KB。"
TOO_MANY = "自定义学习方式超过 50 个，这个文件没有载入。"
NOT_UTF8 = "文件需要使用 UTF-8 编码。"
UNREADABLE = "无法读取这个文件。"
NO_SETTINGS = "文件开头需要一个由两行 --- 包住的设置区。"
BAD_LINE = "设置区的每一行需要写成「键: 值」。"
UNKNOWN_KEY = "设置区只能使用 title_zh、title_en、description_zh、description_en 和 order。"
DUPLICATE_KEY = "设置区的同一个键只能出现一次。"
MISSING_TITLE = "需要填写 title_zh 和 title_en。"
LONG_VALUE = "每个设置值不能超过 120 个字符。"
BAD_ORDER = "order 需要是整数。"
EMPTY_INSTRUCTION = "设置区之后需要写下教学说明。"
LONG_INSTRUCTION = "教学说明不能超过 4,000 个字符。"


class PresetError(ValueError):
    """A file that cannot be used; the message is one of the fixed reasons above."""


@dataclass(frozen=True)
class LearningPreset:
    id: str
    source: str  # "builtin" | "custom"
    title_zh: str
    title_en: str
    description_zh: str
    description_en: str
    instruction: str
    order: int | None = None
    name: str = ""  # The file name without .md; orders styles with equal order.

    def public(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "title_zh": self.title_zh,
            "title_en": self.title_en,
            "description_zh": self.description_zh,
            "description_en": self.description_en,
            "instruction": self.instruction,
        }


def parse_preset(text: str) -> dict:
    """Read one preset file: `key: value` settings between the first two `---` lines,
    then the instruction. Strict: a missing settings block, a line without a colon, an
    unknown or repeated key, a missing title, a value over 120 characters, a non-integer
    order, or an empty or over-long instruction raises PresetError. A byte-order mark
    and CRLF line ends are accepted."""
    lines = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[0].strip() != "---":
        raise PresetError(NO_SETTINGS)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise PresetError(NO_SETTINGS)
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, colon, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not colon or not key:
            raise PresetError(BAD_LINE)
        if key not in SETTING_KEYS:
            raise PresetError(UNKNOWN_KEY)
        if key in values:
            raise PresetError(DUPLICATE_KEY)
        if len(value) > MAX_VALUE_CHARS:
            raise PresetError(LONG_VALUE)
        values[key] = value
    if not values.get("title_zh") or not values.get("title_en"):
        raise PresetError(MISSING_TITLE)
    order = values.get("order")
    if order is not None and not _ORDER.fullmatch(order):
        raise PresetError(BAD_ORDER)
    instruction = "\n".join(lines[end + 1 :]).strip()
    if not instruction:
        raise PresetError(EMPTY_INSTRUCTION)
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise PresetError(LONG_INSTRUCTION)
    return {
        "title_zh": values["title_zh"],
        "title_en": values["title_en"],
        "description_zh": values.get("description_zh", ""),
        "description_en": values.get("description_en", ""),
        "instruction": instruction,
        "order": None if order is None else int(order),
    }


def _display_order(preset: LearningPreset) -> tuple:
    # Styles without an order follow those with one; equal orders go by file name.
    return (preset.order is None, preset.order or 0, preset.name)


@cache
def builtin_presets() -> tuple[LearningPreset, ...]:
    """The shipped styles by order. A built-in file that fails to parse is a packaging
    error and raises, which the test suite checks."""
    loaded = [
        LearningPreset(
            id=path.stem,
            source="builtin",
            name=path.stem,
            **parse_preset(path.read_text(encoding="utf-8")),
        )
        for path in BUILTIN_DIRECTORY.glob("*.md")
    ]
    return tuple(sorted(loaded, key=_display_order))


def presets_directory(configured: str) -> Path:
    """LEARNING_PRESETS_DIR; a relative path starts at the project root, never at the
    working directory of whatever started the server."""
    path = Path(configured.strip() or "learning-presets").expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


CUSTOM_DIRECTORY = presets_directory(settings.learning_presets_dir)

_lock = threading.Lock()
# One directory's parsed files: name -> ((mtime_ns, size), preset or fixed reason).
_cache: dict[str, dict[str, tuple[tuple[int, int], LearningPreset | str]]] = {}


def _read_custom(path: Path, name: str) -> LearningPreset | str | None:
    """Parse one custom file: a preset, a fixed reason it cannot be used, or None when
    the entry is no longer a regular file (deleted, or replaced by a link or a pipe)."""
    try:
        descriptor = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return None if exc.errno == errno.ELOOP else UNREADABLE
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            if info.st_size > MAX_FILE_BYTES:
                return TOO_LARGE
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return UNREADABLE
    if len(data) > MAX_FILE_BYTES:
        return TOO_LARGE
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return NOT_UTF8
    try:
        fields = parse_preset(text)
    except PresetError as exc:
        return str(exc)
    stem = name.removesuffix(".md")
    return LearningPreset(id=CUSTOM_PREFIX + stem, source="custom", name=stem, **fields)


def custom_presets(directory: Path) -> tuple[list[LearningPreset], list[dict]]:
    """The usable custom styles in `directory` by order and name, and {file, reason}
    for each file that cannot be used. Only regular files named like `my-style.md`
    count, at most 50 of them in name order; symbolic links (the directory included)
    and other entries are ignored. Parsed files are reused while their modification
    time and size are unchanged."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            return [], []
        with os.scandir(directory) as entries:
            files = sorted(
                (entry.name, entry)
                for entry in entries
                if FILE_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            )
    except OSError:
        return [], []
    presets: list[LearningPreset] = []
    errors: list[dict] = []
    with _lock:
        previous = _cache.get(str(directory), {})
        fresh: dict[str, tuple[tuple[int, int], LearningPreset | str]] = {}
        for index, (name, entry) in enumerate(files):
            if index >= MAX_CUSTOM_FILES:
                errors.append({"file": name, "reason": TOO_MANY})
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            key = (info.st_mtime_ns, info.st_size)
            cached = previous.get(name)
            result = (
                cached[1] if cached and cached[0] == key else _read_custom(directory / name, name)
            )
            if result is None:
                continue
            fresh[name] = (key, result)
            if isinstance(result, str):
                errors.append({"file": name, "reason": result})
            else:
                presets.append(result)
        # Only the directory in use is kept, so the cache never outgrows it.
        _cache.clear()
        _cache[str(directory)] = fresh
    return sorted(presets, key=_display_order), errors


def list_presets(directory: Path | None = None) -> tuple[list[LearningPreset], list[dict]]:
    """Every usable style in display order (built-ins by order, then custom styles by
    order and name) and the custom files that could not be used."""
    custom, errors = custom_presets(CUSTOM_DIRECTORY if directory is None else directory)
    return [*builtin_presets(), *custom], errors


def preset_catalog(directory: Path | None = None) -> dict[str, LearningPreset]:
    """Usable styles by id. A stored id missing here (a deleted custom file) is kept as
    history by its node or topic and answers in the default way."""
    presets, _ = list_presets(directory)
    return {preset.id: preset for preset in presets}


def preset_section(preset: LearningPreset) -> str:
    """The final section of an answer's system prompt for the style it was asked with."""
    return (
        f"学习方式：{preset.title_zh}（用户为这个问题选择的教学方式）\n"
        f"{preset.instruction}\n"
        "用户本次问题中的明确要求优先于这个学习方式；"
        "学习方式只改变讲解的组织方式，不降低事实准确和严谨的要求。"
    )


router = APIRouter(prefix="/learning-presets", tags=["learning-presets"])


@router.get("")
def get_learning_presets() -> dict:
    presets, errors = list_presets()
    return {"presets": [preset.public() for preset in presets], "errors": errors}
