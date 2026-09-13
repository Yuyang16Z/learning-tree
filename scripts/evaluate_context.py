#!/usr/bin/env python3
"""Reproduce synthetic context-compaction checks without a database or model API."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.context import build_context
from app.context_budget import ContextPolicy, estimate_request_tokens, fit_request
from app.models import Message, Node


def evaluate() -> dict:
    policy = ContextPolicy()
    condition = "只有在验证集完全独立于训练过程时，才能把验证结果用于模型选择。"
    path = []
    for index in range(24):
        node = Node(id=index + 1, tree_id=1, parent_id=index or None, title=f"学习步骤 {index + 1}")
        answer = "这是展开概念的背景解释。\n" * 240
        if index == 0:
            answer += condition + "\n" + "这是之后的补充说明。\n" * 240
        path.append(
            (
                node,
                [
                    Message(
                        id=index * 2 + 1,
                        node_id=node.id,
                        role="user",
                        content=f"解释步骤 {index + 1}。",
                    ),
                    Message(id=index * 2 + 2, node_id=node.id, role="assistant", content=answer),
                ],
            )
        )
    current = Node(
        id=25, tree_id=1, parent_id=24, title="回到验证条件", seed_text="验证结果用于模型选择"
    )
    question = "验证集需要满足什么条件？请保留限制条件。"
    before = [m.model_dump_json() for _, messages in path for m in messages]
    stats = {}
    system, messages = build_context(path, current, [], question, policy=policy, diagnostics=stats)
    blob = system + json.dumps(messages, ensure_ascii=False)
    long_path = {
        **stats,
        "middle_condition_retained": condition in blob,
        "question_preserved": messages[-1]["content"] == question,
        "quote_preserved": current.seed_text in system,
        "originals_unchanged": before == [m.model_dump_json() for _, group in path for m in group],
    }
    assert all(
        long_path[key]
        for key in (
            "compacted",
            "middle_condition_retained",
            "question_preserved",
            "quote_preserved",
            "originals_unchanged",
        )
    )
    assert stats["estimated_input_tokens"] <= policy.input_budget

    corrected = "更正：验证集必须保持独立，最终报告还需要另行使用测试集。"
    path[0][1][1].content = path[0][1][1].content.replace(condition, corrected)
    system2, messages2 = build_context(path, current, [], question, policy=policy)
    revised_blob = system2 + json.dumps(messages2, ensure_ascii=False)
    source_update = {
        "new_condition_retained": corrected in revised_blob,
        "old_condition_absent": condition not in revised_blob,
    }
    assert all(source_update.values())

    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    image_stats = {}
    system3, messages3 = build_context(
        path, current, [], question, [image], policy=policy, diagnostics=image_stats
    )
    images = {
        **image_stats,
        "current_image_intact": messages3[-1]["content"][1]["image_url"]["url"] == image,
    }
    assert (
        images["current_image_intact"] and images["estimated_input_tokens"] <= policy.input_budget
    )

    tool = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    tool_messages = [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_synthetic",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_synthetic", "content": "合成工具结果。" * 20000},
    ]
    fitted = fit_request("固定指令", tool_messages, tool, policy=policy)
    tools = {
        "raw_estimate": estimate_request_tokens("固定指令", tool_messages, tool),
        "estimated_input_tokens": fitted.estimated_input_tokens,
        "omission_disclosed": "中间内容未发送" in fitted.messages[-1]["content"],
        "call_id_preserved": fitted.messages[-1]["tool_call_id"] == "call_synthetic",
        "original_result_unchanged": len(tool_messages[-1]["content"])
        == len("合成工具结果。") * 20000,
    }
    assert all(
        tools[key]
        for key in ("omission_disclosed", "call_id_preserved", "original_result_unchanged")
    )
    assert tools["estimated_input_tokens"] <= policy.input_budget
    return {
        "scope": "Synthetic deterministic validation; no private data, model API, or answer-quality claims.",
        "estimator": "UTF-8 bytes plus metadata and fixed image reservations; not provider token counts.",
        "policy": {**asdict(policy), "input_budget": policy.input_budget},
        "long_path": long_path,
        "source_update": source_update,
        "current_image": images,
        "large_tool_result": tools,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
