"""Local input-budget estimates and protocol-safe last-mile request fitting.

Request sizes come from a character-class token estimator with a safety factor;
image reservations are estimates, not provider token counts. No tokenizer
downloads or API calls. ``text_tokens`` stays a UTF-8 byte length for the
summary, extraction and document producers that budget bytes.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass

from .config import settings

IMAGE_TOKEN_RESERVE = 4096
HISTORY_OMISSION = (
    "\n[部分较早问答因输入预算未附送，不能据此猜测省略内容；"
    "如有 read_learning_source 工具，可按当前学习路径回查原文。]"
)
TOOL_OMISSION = (
    "\n[工具结果因上下文预算仅保留首尾原文；中间内容未发送。"
    "结果不完整，不能据此断言某内容不存在，也不要为补读而重复执行有副作用的操作。]\n"
)
SYSTEM_OMISSION = (
    "\n[本轮工具调用增长，可选历史摘录、记忆及文档摘录已省略，原始资料仍保留。"
    "不要猜测省略内容；如有 read_learning_source 或 read_document_source 工具，可回查原文。]"
)


class ContextBudgetExceeded(ValueError):
    """A safe, actionable error: never embeds user content or provider data."""

    def __init__(self):
        super().__init__(
            "本轮问题、引用、图片或工具信息超出上下文预算。请缩短输入、减少图片或工具，"
            "或在模型设置中填写该模型实际支持的更大上下文窗口。"
        )


@dataclass(frozen=True)
class ContextPolicy:
    window_tokens: int = 32768
    output_tokens: int = 4096
    # Learned ratio of provider-reported to estimated input tokens for this model.
    calibration: float = 1.0
    # Share of the input budget at which history is compacted (Settings.context_compaction_ratio).
    compaction_ratio: float = 0.6

    @property
    def raw_input_budget(self) -> int:
        # Reserve response space and serialization/estimation headroom.
        return max(0, self.window_tokens - self.output_tokens - max(512, self.window_tokens // 20))

    @property
    def input_budget(self) -> int:
        # An estimator that runs low for this model shrinks the budget; one that
        # runs high never enlarges it, so the headroom stays conservative.
        raw = self.raw_input_budget
        return int(raw / self.calibration) if self.calibration > 1 else raw

    @property
    def compaction_budget(self) -> int:
        # Compaction starts here so tool results and follow-ups keep room below
        # the hard budget, which fit_request still enforces on every request.
        return int(self.input_budget * self.compaction_ratio)

    @property
    def tool_reserve(self) -> int:
        return min(4096, self.input_budget // 3)

    @classmethod
    def from_spec(cls, spec) -> ContextPolicy:
        return cls(
            getattr(spec, "context_window", 32768),
            spec.max_tokens,
            getattr(spec, "calibration", 1.0),
            settings.context_compaction_ratio,
        )


@dataclass(frozen=True)
class BudgetedRequest:
    system: str
    messages: list[dict]
    estimated_input_tokens: int
    compressed: bool


def text_tokens(text: str) -> int:
    """UTF-8 byte length: the unit of summary, extraction and document byte budgets."""
    return len(text.encode("utf-8"))


# Character classes with roughly uniform tokenizer cost. The ranges are disjoint
# from each other and from Unicode whitespace, so every code point lands in one
# class. Other non-ASCII code points (accents, emoji, symbols) cost one unit.
_CJK_RUNS = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]+")
_ASCII_RUNS = re.compile(r"[!-~]+")  # Visible ASCII: letters, digits and punctuation.
_SPACE_RUNS = re.compile(r"\s+")
CJK_UNITS_PER_CHAR = 0.7
ASCII_UNITS_PER_CHAR = 0.33
SPACE_UNITS_PER_CHAR = 0.1
OTHER_UNITS_PER_CHAR = 1.0
ESTIMATE_SAFETY_FACTOR = 1.15


def estimate_text_tokens(text: str) -> int:
    """Approximate provider tokens from character classes; never a provider count.

    CJK ideographs, Kana and Hangul cost about 0.7 unit each, visible ASCII about a
    third, whitespace a tenth and any other non-ASCII code point one unit. The 1.15
    safety factor keeps the estimate conservative for code, emoji and mixed scripts.
    """
    if not text:
        return 0
    cjk = sum(len(run) for run in _CJK_RUNS.findall(text))
    ascii_visible = sum(len(run) for run in _ASCII_RUNS.findall(text))
    spaces = sum(len(run) for run in _SPACE_RUNS.findall(text))
    other = len(text) - cjk - ascii_visible - spaces
    units = (
        CJK_UNITS_PER_CHAR * cjk
        + ASCII_UNITS_PER_CHAR * ascii_visible
        + SPACE_UNITS_PER_CHAR * spaces
        + OTHER_UNITS_PER_CHAR * other
    )
    return math.ceil(round(units * ESTIMATE_SAFETY_FACTOR, 6))


def _json_tokens(value) -> int:
    return estimate_text_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _is_image(part) -> bool:
    return isinstance(part, dict) and part.get("type") in ("image", "image_url")


def _content_tokens(content) -> int:
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if not content:
        return 0
    if not isinstance(content, list):
        return _json_tokens(content)
    total = 0
    for part in content:
        if _is_image(part):
            total += IMAGE_TOKEN_RESERVE + 64
        else:
            total += _json_tokens(part) + 16
    return total


def has_images(messages: list[dict]) -> bool:
    """Whether a request carries image parts, which the estimate reserves rather than counts."""
    return any(
        isinstance(value, list) and any(_is_image(part) for part in value)
        for message in messages
        for value in (message.get("content"), message.get("_anthropic_content"))
    )


def estimate_request_tokens(
    system: str, messages: list[dict], tools: list[dict] | None = None, protocol: str = "openai"
) -> int:
    total = estimate_text_tokens(system) + 64
    for message in messages:
        total += 48
        if protocol == "anthropic" and message.get("_anthropic_content") is not None:
            # Native blocks already include the mirrored text/tool-use payload.
            total += _content_tokens(message["_anthropic_content"])
        else:
            total += _content_tokens(message.get("content"))
            if message.get("tool_calls"):
                total += _json_tokens(message["tool_calls"])
            reasoning = message.get("reasoning_content")
            if protocol != "anthropic" and isinstance(reasoning, str):
                # OpenAI-compatible thinking endpoints get their reasoning back on
                # assistant turns; the Anthropic adapter never sends this field.
                total += estimate_text_tokens(reasoning)
        if message.get("tool_call_id"):
            total += estimate_text_tokens(message["tool_call_id"]) + 32
    if tools:
        total += _json_tokens(tools) + 64 * len(tools)
    return total


def _trim_result(text: str, token_budget: int) -> str:
    """Keep the head and tail of a tool result within a token-scale budget."""
    if estimate_text_tokens(text) <= token_budget:
        return text
    room = max(0, token_budget - estimate_text_tokens(TOOL_OMISSION))
    # Slice by characters at the text's own token density, then let the
    # estimator decide; shrinking converges because chars strictly decreases.
    density = estimate_text_tokens(text) / len(text)
    chars = min(len(text), int(room / density))
    while True:
        front = text[: chars // 2]
        back = text[len(text) - (chars - chars // 2) :] if chars else ""
        result = front + TOOL_OMISSION + back
        if chars == 0 or estimate_text_tokens(result) <= token_budget:
            return result
        chars = chars * 9 // 10 if chars > 20 else chars - 1


def fit_request(
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    policy: ContextPolicy,
    protocol: str = "openai",
    protected_system: str | None = None,
) -> BudgetedRequest:
    """Keep mandatory input and native blocks intact; modify request copies only.

    Older user turns are removed as whole protocol groups. The current question
    and all subsequent tool call IDs/arguments/signed blocks remain intact. When
    supplied by the context builder, protected_system allows optional system
    context to be removed as a whole without parsing any source text. Tool-result
    bodies are abridged only after this, with an explicit warning.
    """
    fitted = copy.deepcopy(messages)
    budget = policy.input_budget

    def estimate():
        return estimate_request_tokens(system, fitted, tools, protocol)

    compressed = False
    history_omitted = False
    while estimate() > budget:
        user_positions = [i for i, m in enumerate(fitted) if m.get("role") == "user"]
        if user_positions and user_positions[0] > 0:
            fitted = fitted[user_positions[0] :]
            if not history_omitted:
                system += HISTORY_OMISSION
                history_omitted = True
            compressed = True
            continue
        if len(user_positions) < 2:
            break
        # Never leave a tool result orphaned by trimming individual messages.
        fitted = fitted[user_positions[1] :]
        if not history_omitted:
            system += HISTORY_OMISSION
            history_omitted = True
        compressed = True

    if estimate() > budget and protected_system is not None:
        reduced = protected_system + SYSTEM_OMISSION
        if history_omitted:
            reduced += HISTORY_OMISSION
        # An already minimal system must not gain a larger omission notice.
        if estimate_text_tokens(reduced) < estimate_text_tokens(system):
            system = reduced
            compressed = True

    if estimate() > budget:
        results = [m for m in fitted if m.get("role") == "tool"]
        originals = [str(m.get("content") or "") for m in results]
        minimum = estimate_text_tokens(TOOL_OMISSION) + 128
        largest = max((estimate_text_tokens(s) for s in originals), default=0)
        cap = largest
        while cap > minimum and estimate() > budget:
            cap = max(minimum, cap // 2)
            for message, original in zip(results, originals, strict=True):
                message["content"] = _trim_result(original, cap)
            compressed = True
    count = estimate()
    if count > budget:
        raise ContextBudgetExceeded()
    return BudgetedRequest(system, fitted, count, compressed)
