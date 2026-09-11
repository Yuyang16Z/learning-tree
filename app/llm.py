"""统一消息接口，按模型配置选择 OpenAI 兼容或 Anthropic 原生协议。

三种能力都在这里：
- 普通/深度思考：stream_chat 逐段吐 ("text"|"reasoning", str)；推理模型的 reasoning_content 单独归到 reasoning。
- 工具调用：run_agent 跑"回合制 agent 循环"——模型说要调工具→执行→塞回结果→再问，直到出最终答案。
- 多模态：messages 里的 content 可以是"文本 + image_url"数组（在 context.py 组装）。

mock 模式（api_key/base_url=mock）把上面三样都模拟了，没 key 也能 curl 跑通全链路。
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from openai import OpenAI

from . import anthropic_provider

MOCK_ANSWER = (
    "（mock 模型）我收到的上下文只有从根节点到当前节点这一条脊柱，"
    "兄弟分支没掺进来，所以能聚焦地回答你当前这个问题。"
)


@dataclass
class LLMSpec:
    label: str
    base_url: str
    llm_model: str
    api_key: str
    protocol: Literal["openai", "anthropic"] = "openai"
    max_tokens: int = 4096


def _is_mock(spec: LLMSpec) -> bool:
    return spec.api_key == "mock" or spec.base_url.startswith("mock")


def _client(spec: LLMSpec) -> OpenAI:
    return OpenAI(base_url=spec.base_url, api_key=spec.api_key, timeout=45.0, max_retries=0)


def _chunks(s: str, n: int = 4) -> Iterator[str]:
    for i in range(0, len(s), n):
        yield s[i : i + n]


def _text_of(content) -> str:
    """content 可能是 str 或多模态数组，取其中的文字。"""
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    return content or ""


def _last_user_text(convo: list[dict]) -> str:
    for m in reversed(convo):
        if m.get("role") == "user":
            return _text_of(m.get("content"))
    return ""


def _mock_args(tool: dict, q: str) -> dict:
    """mock 模式下按工具 schema 生成占位参数，让 MCP 工具也能真被调起来。"""
    params = tool["function"].get("parameters", {})
    props = params.get("properties", {})
    required = params.get("required", list(props.keys()))
    args: dict = {}
    for key in required:
        typ = props.get(key, {}).get("type", "string")
        if typ in ("integer", "number"):
            args[key] = 1
        elif typ == "boolean":
            args[key] = True
        else:
            args[key] = q[:40] or "test"
    return args


# ---------- 普通 / 深度思考：流式 ----------
def stream_chat(
    spec: LLMSpec, system: str, messages: list[dict], deep: bool = False
) -> Iterator[tuple[str, str]]:
    """逐段产出 (kind, text)，kind ∈ {"reasoning","text"}。"""
    if _is_mock(spec):
        if deep:
            for ch in _chunks("（mock 思考）先拆解问题 → 定位关键概念 → 组织答案……"):
                yield ("reasoning", ch)
        for ch in _chunks(MOCK_ANSWER):
            yield ("text", ch)
        return

    if spec.protocol == "anthropic":
        yield from anthropic_provider.stream_chat(spec, system, messages, deep)
        return

    stream = _client(spec).chat.completions.create(
        model=spec.llm_model,
        messages=[{"role": "system", "content": system}, *messages],
        stream=True,
    )
    finished = False
    try:
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                if choice.finish_reason not in ("stop",):
                    raise RuntimeError(f"模型输出提前结束（{choice.finish_reason}），可以重试。")
                finished = True
            delta = choice.delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ("reasoning", reasoning)
            if delta.content:
                yield ("text", delta.content)
        if not finished:
            raise RuntimeError("模型连接在回答完成前断开，可以重试。")
    finally:
        stream.close()


# ---------- 工具调用：agent 回合制 ----------
def _chat_once(spec: LLMSpec, convo: list[dict], tools: list[dict]) -> dict:
    """一次非流式调用，返回 {content, tool_calls:[{id,name,args}]}。"""
    if _is_mock(spec):
        has_tool_result = any(m.get("role") == "tool" for m in convo)
        if tools and not has_tool_result:
            q = _last_user_text(convo)
            names = [t["function"]["name"] for t in tools]
            if "http" in q and "fetch" in names:
                name, args = (
                    "fetch",
                    {
                        "url": next(
                            (w for w in q.split() if w.startswith("http")), "http://127.0.0.1"
                        )
                    },
                )
            elif "web_search" in names:
                name, args = "web_search", {"query": q[:40]}
            else:
                # 通用工具（含 MCP）：按 schema 把必填参数填上占位值。
                tool = tools[0]
                name, args = tool["function"]["name"], _mock_args(tool, q)
            return {
                "content": None,
                "tool_calls": [{"id": "call_mock_1", "name": name, "args": args}],
            }
        return {"content": "（mock 模型）我调用工具拿到结果后，据此作答。", "tool_calls": []}

    if spec.protocol == "anthropic":
        return anthropic_provider.chat_once(spec, convo, tools)

    resp = _client(spec).chat.completions.create(
        model=spec.llm_model,
        messages=convo,
        tools=tools,
        stream=False,
    )
    if resp.choices[0].finish_reason not in ("stop", "tool_calls"):
        raise RuntimeError("模型回答未完整结束，可以重试。")
    msg = resp.choices[0].message
    tcs = [
        {"id": tc.id, "name": tc.function.name, "args": json.loads(tc.function.arguments or "{}")}
        for tc in (msg.tool_calls or [])
    ]
    return {"content": msg.content, "tool_calls": tcs}


def run_agent(
    spec: LLMSpec,
    system: str,
    messages: list[dict],
    tool_defs: list[dict],
    execute: Callable[[str, dict], str],
    max_rounds: int = 4,
) -> Iterator[dict]:
    """产出事件：{"type":"tool_start"|"tool_end"|"delta", ...}。

    tool_defs：OpenAI 兼容的工具定义（内置 + MCP 混在一起）。
    execute(name, args)：由上层提供的路由执行器（内置直调、MCP 转发）。
    """
    if _is_mock(spec):
        # A demo must never launch a real process or invoke a selected external tool.
        for ch in _chunks(MOCK_ANSWER + "（演示模式未执行外部工具。）"):
            yield {"type": "delta", "text": ch}
        return
    convo: list[dict] = [{"role": "system", "content": system}, *messages]

    for _ in range(max_rounds):
        r = _chat_once(spec, convo, tool_defs)
        if r["tool_calls"]:
            convo.append(
                {
                    "role": "assistant",
                    "content": r["content"] or None,
                    **(
                        {"_anthropic_content": r["_anthropic_content"]}
                        if "_anthropic_content" in r
                        else {}
                    ),
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"], ensure_ascii=False),
                            },
                        }
                        for tc in r["tool_calls"]
                    ],
                }
            )
            for tc in r["tool_calls"]:
                yield {"type": "tool_start", "name": tc["name"], "args": tc["args"]}
                result = execute(tc["name"], tc["args"])
                yield {"type": "tool_end", "name": tc["name"], "result": result}
                convo.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            for ch in _chunks(r["content"] or ""):
                yield {"type": "delta", "text": ch}
            return

    yield {"type": "delta", "text": "（已达最大工具轮数，先答到这。）"}


# ---------- 非流式：摘要用 ----------
def complete(spec: LLMSpec, system: str, messages: list[dict]) -> str:
    if _is_mock(spec):
        return "（mock 摘要）本节点讲清了这个概念的关键点，可作为下层分支的背景。"
    if spec.protocol == "anthropic":
        return anthropic_provider.complete(spec, system, messages)
    resp = _client(spec).chat.completions.create(
        model=spec.llm_model,
        messages=[{"role": "system", "content": system}, *messages],
        stream=False,
    )
    if resp.choices[0].finish_reason != "stop":
        raise RuntimeError("模型回答未完整结束，可以重试。")
    return resp.choices[0].message.content or ""


_EXTRACT_SYSTEM = (
    "你是记忆提取器。从下面这轮学习问答里，提取值得长期记住的信息，宁缺毋滥，没有就返回空数组。\n"
    "分两类：preference=关于用户本人的持久偏好/习惯/正在学什么（跨话题通用，如“喜欢生活化例子”“在学 MCP”）；"
    "fact=这个话题下的关键结论/事实。\n"
    '只输出 JSON：{"preferences": ["…"], "facts": ["…"]}，每条一句话，各最多 3 条。'
)


def extract_memories(spec: LLMSpec, question: str, answer: str) -> dict:
    """从一轮问答里提炼记忆，返回 {"preferences": [...], "facts": [...]}。"""
    if _is_mock(spec):
        return {
            "preferences": ["（mock）偏好简短、生活化的解释"],
            "facts": [f"（mock）关于「{question[:12]}」的一个关键结论"],
        }
    try:
        raw = complete(
            spec, _EXTRACT_SYSTEM, [{"role": "user", "content": f"问：{question}\n答：{answer}"}]
        )
        import re

        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"preferences": [], "facts": []}
        d = json.loads(m.group(0))
        return {
            "preferences": [str(x).strip() for x in d.get("preferences", []) if str(x).strip()][:3],
            "facts": [str(x).strip() for x in d.get("facts", []) if str(x).strip()][:3],
        }
    except Exception:  # noqa: BLE001
        return {"preferences": [], "facts": []}


def test_connection(spec: LLMSpec) -> tuple[bool, str]:
    if _is_mock(spec):
        return True, "mock 可用"
    try:
        if spec.protocol == "anthropic":
            anthropic_provider.test_connection(spec)
        else:
            with _client(spec) as api:
                api.chat.completions.create(
                    model=spec.llm_model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
        return True, "连接成功"
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status_code", None)
        details = {
            400: "请求参数不匹配，请检查协议、模型名和输出设置。",
            401: "API Key 无效，请检查密钥。",
            403: "当前密钥没有此模型的访问权限。",
            404: "服务地址或模型不存在，请检查地址、协议和模型名。",
            429: "调用额度或速率受限，请稍后重试。",
        }
        # Provider errors can contain request bodies and credentials. Never return them.
        if status:
            return (
                False,
                f"HTTP {status}：{details.get(status, '模型服务暂时不可用，请稍后重试。')}",
            )
        if isinstance(e, ValueError):
            return False, "配置格式无效，请检查服务地址和协议。"
        return False, "连接失败，请检查服务地址和网络后重试。"
