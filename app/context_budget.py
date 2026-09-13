"""Local input-budget estimates and protocol-safe last-mile request fitting.

UTF-8 bytes deliberately overestimate ordinary text tokens; image reservations
are estimates, not provider token counts. No tokenizer downloads or API calls.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

IMAGE_TOKEN_RESERVE = 4096
HISTORY_OMISSION = (
    "\n[部分较早问答因输入预算未附送，不能据此猜测省略内容；"
    "如有 read_learning_source 工具，可按当前学习路径回查原文。]"
)
TOOL_OMISSION = (
    "\n[工具结果因上下文预算仅保留首尾原文；中间内容未发送。"
    "结果不完整，不能据此断言某内容不存在，也不要为补读而重复执行有副作用的操作。]\n"
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

    @property
    def input_budget(self) -> int:
        # Reserve response space and serialization/estimation headroom.
        return max(0, self.window_tokens - self.output_tokens - max(512, self.window_tokens // 20))

    @property
    def tool_reserve(self) -> int:
        return min(4096, self.input_budget // 3)

    @classmethod
    def from_spec(cls, spec) -> ContextPolicy:
        return cls(getattr(spec, "context_window", 32768), spec.max_tokens)


@dataclass(frozen=True)
class BudgetedRequest:
    system: str
    messages: list[dict]
    estimated_input_tokens: int
    compressed: bool


def text_tokens(text: str) -> int:
    return len(text.encode("utf-8"))


def _json_tokens(value) -> int:
    return text_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _content_tokens(content) -> int:
    if isinstance(content, str):
        return text_tokens(content)
    if not content:
        return 0
    if not isinstance(content, list):
        return _json_tokens(content)
    total = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("image", "image_url"):
            total += IMAGE_TOKEN_RESERVE + 64
        else:
            total += _json_tokens(part) + 16
    return total


def estimate_request_tokens(
    system: str, messages: list[dict], tools: list[dict] | None = None, protocol: str = "openai"
) -> int:
    total = text_tokens(system) + 64
    for message in messages:
        total += 48
        if protocol == "anthropic" and message.get("_anthropic_content") is not None:
            # Native blocks already include the mirrored text/tool-use payload.
            total += _content_tokens(message["_anthropic_content"])
        else:
            total += _content_tokens(message.get("content"))
            if message.get("tool_calls"):
                total += _json_tokens(message["tool_calls"])
        if message.get("tool_call_id"):
            total += text_tokens(message["tool_call_id"]) + 32
    if tools:
        total += _json_tokens(tools) + 64 * len(tools)
    return total


def _trim_result(text: str, byte_budget: int) -> str:
    if text_tokens(text) <= byte_budget:
        return text
    room = max(0, byte_budget - text_tokens(TOOL_OMISSION))
    data = text.encode("utf-8")
    front = data[: room // 2].decode("utf-8", errors="ignore")
    back = data[-(room - room // 2) :].decode("utf-8", errors="ignore") if room else ""
    return front + TOOL_OMISSION + back


def fit_request(
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    policy: ContextPolicy,
    protocol: str = "openai",
) -> BudgetedRequest:
    """Keep mandatory input and native blocks intact; modify request copies only.

    Older user turns are removed as whole protocol groups. The current question
    and all subsequent tool call IDs/arguments/signed blocks remain intact. If
    necessary only tool-result bodies are abridged, with an explicit warning.
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

    if estimate() > budget:
        results = [m for m in fitted if m.get("role") == "tool"]
        originals = [str(m.get("content") or "") for m in results]
        minimum = text_tokens(TOOL_OMISSION) + 128
        largest = max((text_tokens(s) for s in originals), default=0)
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
