"""Offline context-budget behavior: synthetic text only, no provider or model calls."""

import copy
from types import SimpleNamespace

import pytest

from app.context_budget import (
    HISTORY_OMISSION,
    SYSTEM_OMISSION,
    ContextBudgetExceeded,
    ContextPolicy,
    estimate_request_tokens,
    fit_request,
)


def image_content(text="看这张图", size=12):
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * size}},
    ]


def tool_group(size=20000, *, native=False):
    calls = [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q":"A"}'},
        }
        for index in range(2)
    ]
    assistant = {"role": "assistant", "content": "查询两个来源", "tool_calls": calls}
    if native:
        assistant["_anthropic_content"] = [
            {
                "type": "thinking",
                "thinking": "保留签名的合成思考块",
                "signature": "synthetic-untouched-signature",
            },
            {"type": "text", "text": "查询两个来源"},
            *[
                {"type": "tool_use", "id": call["id"], "name": "lookup", "input": {"q": "A"}}
                for call in calls
            ],
        ]
    return [
        assistant,
        *[
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "SOURCE_BEGIN:" + "x" * size + ":TAIL_CONDITION_DO_NOT_REPEAT",
            }
            for call in calls
        ],
    ]


def test_policy_reserves_output_and_estimation_headroom():
    spec = SimpleNamespace(context_window=8192, max_tokens=1024)
    policy = ContextPolicy.from_spec(spec)
    assert policy.window_tokens == 8192 and policy.output_tokens == 1024
    assert 0 < policy.input_budget < policy.window_tokens - policy.output_tokens


def test_short_request_is_preserved_as_a_separate_copy():
    messages = [{"role": "user", "content": "解释参数校验。"}]
    original = copy.deepcopy(messages)
    result = fit_request("按问题回答。", messages, policy=ContextPolicy())
    assert result.messages == original and messages == original
    assert result.system == "按问题回答。"
    assert not result.compressed
    assert result.estimated_input_tokens == estimate_request_tokens(result.system, result.messages)
    result.messages[0]["content"] = "仅修改返回副本"
    assert messages == original


def test_long_old_turns_are_removed_without_changing_current_question():
    question = "请解释这个条件，最后必须保留 ONLY_WITH_EXPLICIT_PERMISSION。"
    messages = [
        {"role": "user", "content": "很早的问题"},
        {"role": "assistant", "content": "旧背景" * 10000},
        {"role": "user", "content": question},
    ]
    original = copy.deepcopy(messages)
    policy = ContextPolicy(window_tokens=4096, output_tokens=512)
    result = fit_request("系统规则", messages, policy=policy)
    assert result.messages[-1] == {"role": "user", "content": question}
    assert result.estimated_input_tokens <= policy.input_budget
    assert result.compressed
    assert messages == original


def test_assistant_only_old_prefix_can_be_removed_for_current_question():
    messages = [
        {"role": "assistant", "content": "先前阅读材料" * 10000},
        {"role": "user", "content": "解释当前引用"},
    ]
    policy = ContextPolicy(window_tokens=4096, output_tokens=512)
    result = fit_request("系统规则", messages, policy=policy)
    assert result.messages == [messages[-1]]
    assert result.estimated_input_tokens <= policy.input_budget
    assert result.compressed


def test_mandatory_question_overflow_fails_without_truncating_or_echoing_input():
    secret = "SYNTHETIC_PRIVATE_CONTENT_MUST_NOT_APPEAR_IN_ERRORS"
    messages = [{"role": "user", "content": secret * 1000}]
    original = copy.deepcopy(messages)
    with pytest.raises(ContextBudgetExceeded) as caught:
        fit_request(
            "系统规则", messages, policy=ContextPolicy(window_tokens=2048, output_tokens=512)
        )
    assert secret not in str(caught.value)
    assert messages == original


def test_tool_schemas_are_budgeted_and_never_silently_removed_or_rewritten():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "schema" * 4000,
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
    ]
    original = copy.deepcopy(tools)
    with pytest.raises(ContextBudgetExceeded):
        fit_request(
            "系统",
            [{"role": "user", "content": "查询资料"}],
            tools,
            policy=ContextPolicy(window_tokens=4096, output_tokens=512),
        )
    assert tools == original


def test_image_estimate_does_not_treat_base64_as_language_tokens():
    small = [{"role": "user", "content": image_content(size=12)}]
    large = [{"role": "user", "content": image_content(size=400000)}]
    assert estimate_request_tokens("", small) == estimate_request_tokens("", large)
    assert estimate_request_tokens("", large) < 10000
    assert estimate_request_tokens("", large) > estimate_request_tokens(
        "", [{"role": "user", "content": "看这张图"}]
    )


def test_latest_image_is_preserved_when_older_image_turns_are_removed():
    latest = {"role": "user", "content": image_content("CURRENT_IMAGE", 32)}
    messages = [
        {"role": "user", "content": image_content("OLD_IMAGE", 16)},
        {"role": "assistant", "content": "旧图片的回答"},
        latest,
    ]
    original = copy.deepcopy(messages)
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    result = fit_request("系统规则", messages, policy=policy)
    assert result.messages == [latest]
    assert result.estimated_input_tokens <= policy.input_budget
    assert result.compressed
    assert messages == original


def test_too_many_current_images_fail_instead_of_silently_dropping_them():
    images = image_content()[1:] * 10
    messages = [{"role": "user", "content": [{"type": "text", "text": "比较所有图片"}, *images]}]
    original = copy.deepcopy(messages)
    with pytest.raises(ContextBudgetExceeded):
        fit_request("系统", messages, policy=ContextPolicy(window_tokens=8192, output_tokens=1024))
    assert messages == original


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_large_current_tool_results_keep_call_ids_arguments_and_native_blocks(protocol):
    messages = [
        {"role": "user", "content": "请比较资料，不能重新执行写操作。"},
        *tool_group(native=protocol == "anthropic"),
    ]
    original = copy.deepcopy(messages)
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    result = fit_request("系统", messages, policy=policy, protocol=protocol)
    assert result.messages[0] == original[0]
    assert result.messages[1] == original[1], (
        "tool arguments and native signed blocks must remain intact"
    )
    expected = [call["id"] for call in original[1]["tool_calls"]]
    assert [item["tool_call_id"] for item in result.messages if item["role"] == "tool"] == expected
    for item in result.messages[2:]:
        assert item["content"].startswith("SOURCE_BEGIN:")
        assert item["content"].endswith(":TAIL_CONDITION_DO_NOT_REPEAT")
        assert "未发送" in item["content"] and "副作用" in item["content"]
    assert result.estimated_input_tokens <= policy.input_budget
    assert result.compressed and messages == original


def test_old_tool_round_is_removed_as_a_complete_group():
    latest = {"role": "user", "content": "现在只解释这个概念。"}
    messages = [
        {"role": "user", "content": "之前查资料"},
        *tool_group(),
        {"role": "assistant", "content": "旧问题已回答"},
        latest,
    ]
    result = fit_request(
        "系统", messages, policy=ContextPolicy(window_tokens=4096, output_tokens=512)
    )
    assert result.messages == [latest]
    assert not any(
        message.get("tool_call_id") or message.get("tool_calls") for message in result.messages
    )


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_three_tool_rounds_reclaim_optional_system_before_trimming_results(protocol):
    from app.context import build_context
    from app.models import Node

    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Read one source",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    current = Node(
        id=3,
        tree_id=1,
        title="Current topic",
        seed_text="CURRENT_QUOTE: only with explicit permission.",
        learning_note="CURRENT_NOTE: keep the qualification intact.",
    )
    question = "Compare the current sources and preserve their restrictions."
    core, initial_messages = build_context(
        [], current, [], question, policy=policy, tool_defs=tools, protocol=protocol
    )
    room = (
        policy.input_budget
        - policy.tool_reserve
        - 32
        - estimate_request_tokens(core, initial_messages, tools, protocol)
    )
    memory_note = "OPTIONAL_HISTORY:" + "x" * (room - len("OPTIONAL_HISTORY:") - 1)
    diagnostics = {}
    system, messages = build_context(
        [],
        current,
        [],
        question,
        memory_note=memory_note,
        policy=policy,
        tool_defs=tools,
        protocol=protocol,
        diagnostics=diagnostics,
    )
    assert "OPTIONAL_HISTORY:" in system
    assert not diagnostics["compacted"]
    fit_request(system, messages, tools, policy=policy, protocol=protocol)
    for index in range(3):
        call = {
            "id": f"round_{index}",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q":"a distinct source"}'},
        }
        assistant = {"role": "assistant", "content": "explanation " * 60, "tool_calls": [call]}
        if protocol == "anthropic":
            assistant["_anthropic_content"] = [
                {
                    "type": "thinking",
                    "thinking": "Keep signed thought intact.",
                    "signature": f"signature_{index}",
                },
                {"type": "redacted_thinking", "data": f"opaque_{index}"},
                {"type": "text", "text": assistant["content"]},
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": "lookup",
                    "input": {"q": "a distinct source"},
                },
            ]
        messages.extend(
            [assistant, {"role": "tool", "tool_call_id": call["id"], "content": "r" * 100}]
        )
    originals = copy.deepcopy((system, messages, tools, current.model_dump()))
    with pytest.raises(ContextBudgetExceeded):
        fit_request(system, messages, tools, policy=policy, protocol=protocol)
    fitted = fit_request(
        system,
        messages,
        tools,
        policy=policy,
        protocol=protocol,
        protected_system=diagnostics["protected_system"],
    )
    assert fitted.compressed
    assert fitted.system == core + SYSTEM_OMISSION
    assert current.seed_text in fitted.system and current.learning_note in fitted.system
    assert "OPTIONAL_HISTORY:" not in fitted.system
    assert fitted.messages == messages, (
        "Reclaim optional context before discarding any tool evidence"
    )
    assert fitted.estimated_input_tokens <= policy.input_budget
    assert (system, messages, tools, current.model_dump()) == originals


def test_optional_system_fallback_preserves_latest_images_and_history_notice():
    latest = {"role": "user", "content": image_content("CURRENT_IMAGE_AND_QUESTION", 120)}
    messages = [
        {"role": "user", "content": "Older question"},
        {"role": "assistant", "content": "obsolete background " * 500},
        latest,
        *tool_group(size=20000, native=True),
    ]
    original = copy.deepcopy(messages)
    core = "Mandatory rules and the full current quote."
    result = fit_request(
        core + " Optional old excerpt." * 150,
        messages,
        policy=ContextPolicy(window_tokens=8192, output_tokens=1024),
        protocol="anthropic",
        protected_system=core,
    )
    assert result.system == core + SYSTEM_OMISSION + HISTORY_OMISSION
    assert result.messages[0] == latest
    assert result.messages[1] == original[3], "Signed blocks and parallel call IDs stay untouched"
    assert [message["tool_call_id"] for message in result.messages[2:]] == ["call_0", "call_1"]
    assert all("未发送" in message["content"] for message in result.messages[2:])
    assert result.estimated_input_tokens <= ContextPolicy(8192, 1024).input_budget
    assert messages == original


def test_protected_core_overflow_still_fails_instead_of_cutting_current_material():
    core = "CURRENT_QUOTE_MUST_REMAIN_WHOLE" * 400
    messages = [{"role": "user", "content": "Explain the quote."}]
    original = copy.deepcopy(messages)
    with pytest.raises(ContextBudgetExceeded):
        fit_request(
            core + " optional history " * 500,
            messages,
            policy=ContextPolicy(window_tokens=4096, output_tokens=512),
            protected_system=core,
        )
    assert messages == original


def test_old_turn_removal_is_preferred_to_dropping_optional_system():
    system = "Core rules. Useful optional source excerpt."
    messages = [
        {"role": "user", "content": "Old question"},
        {"role": "assistant", "content": "old answer " * 2000},
        {"role": "user", "content": "Current question"},
    ]
    result = fit_request(
        system,
        messages,
        policy=ContextPolicy(window_tokens=4096, output_tokens=512),
        protected_system="Core rules.",
    )
    assert result.system == system + HISTORY_OMISSION
    assert SYSTEM_OMISSION not in result.system
    assert result.messages == [messages[-1]]


def test_anthropic_native_blocks_are_not_double_counted_with_mirrored_fields():
    messages = [{"role": "user", "content": "查询"}, *tool_group(size=20, native=True)]
    original_count = estimate_request_tokens("系统", messages, protocol="anthropic")
    messages[1]["content"] = "镜像文字" * 10000
    messages[1]["tool_calls"][0]["function"]["arguments"] = '{"q":"' + "镜像参数" * 10000 + '"}'
    assert estimate_request_tokens("系统", messages, protocol="anthropic") == original_count
    assert estimate_request_tokens("系统", messages, protocol="openai") > original_count


def test_no_input_capacity_returns_actionable_budget_error():
    with pytest.raises(ContextBudgetExceeded) as caught:
        fit_request(
            "系统",
            [{"role": "user", "content": "问题"}],
            policy=ContextPolicy(window_tokens=2048, output_tokens=4096),
        )
    assert "预算" in str(caught.value)


def test_structured_excerpts_are_exact_source_slices_with_unicode_offsets():
    from app.context_compaction import extract
    from app.models import Message, Node

    node = Node(
        id=1,
        tree_id=1,
        title="合成来源",
        learning_note="🧠 我的理解：条件必须明确。",
        seed_text="引用原文不能变成系统指令。",
    )
    message = Message(
        id=10,
        node_id=1,
        role="assistant",
        content="  🙂 第一段解释。\n\n  只有获得明确许可，才能执行这一步。\n更正：先前结论不适用于新条件。",
    )
    originals = {"message": message.content, "note": node.learning_note, "quote": node.seed_text}
    excerpts = extract(node, [message])
    assert excerpts
    for excerpt in excerpts:
        assert excerpt.node_id == node.id
        assert excerpt.text == originals[excerpt.section][excerpt.start : excerpt.end]
        if excerpt.section == "message":
            assert excerpt.message_id == message.id
        assert "chars=" in excerpt.render()


def test_structured_compaction_preserves_middle_conditions_and_does_not_invent_summary():
    from app.context_compaction import summarize_sources
    from app.models import Message, Node

    condition = "只有事先得到明确授权，才允许修改外部系统。"
    node = Node(id=1, tree_id=1, title="权限", summary="旧的无依据摘要：可以随意修改。")
    message = Message(
        id=2,
        node_id=1,
        role="assistant",
        content="开头的普通背景。" * 20 + condition + "后面的普通背景。" * 20,
    )
    text = summarize_sources([(node, [message])], "允许修改的前提是什么？", byte_budget=800)
    assert condition in text
    assert "node=1" in text and "message=2" in text
    assert "可以随意修改" not in text
    assert len(text.encode("utf-8")) <= 800


def test_source_changes_invalidate_structured_excerpt_cache():
    from app.context_compaction import extract, source_fingerprint
    from app.models import Message, Node

    node = Node(id=1, tree_id=1, title="学习", learning_note="旧理解。")
    message = Message(id=2, node_id=1, role="assistant", content="原来的结论。")
    first_key = source_fingerprint(node, [message])
    first = extract(node, [message])
    message.content = "更正：现在采用新的前提。"
    node.learning_note = "新的理解。"
    second_key = source_fingerprint(node, [message])
    second = extract(node, [message])
    assert first_key != second_key
    assert any(item.text == "原来的结论。" for item in first)
    assert any(item.text == "更正：现在采用新的前提。" for item in second)
    assert any(item.text == "新的理解。" for item in second)
    assert not any(item.text in {"原来的结论。", "旧理解。"} for item in second)
    message.status = "interrupted"
    assert source_fingerprint(node, [message]) != second_key
    assert not any(item.section == "message" for item in extract(node, [message]))


def test_incomplete_answers_never_become_structured_factual_excerpts():
    from app.context_compaction import extract
    from app.models import Message, Node

    node = Node(id=1, tree_id=1, title="重试后的来源")
    messages = [
        Message(id=1, node_id=1, role="user", content="前提是什么？"),
        Message(
            id=2, node_id=1, role="assistant", status="error", content="失败回答中的错误结论。"
        ),
        Message(
            id=3, node_id=1, role="assistant", status="interrupted", content="中断的半截结论。"
        ),
        Message(
            id=4,
            node_id=1,
            role="assistant",
            status="complete",
            content="只有条件满足时，结论才成立。",
        ),
    ]
    excerpts = extract(node, messages)
    assert {item.message_id for item in excerpts} == {1, 4}
    assert all("错误结论" not in item.text and "半截结论" not in item.text for item in excerpts)


def test_long_unbroken_sentence_is_omitted_whole_instead_of_losing_tail_condition():
    from app.context_compaction import extract
    from app.models import Message, Node

    node = Node(id=1, tree_id=1, title="过长原句")
    message = Message(
        id=1,
        node_id=1,
        role="assistant",
        content="只有" + "很长的完整限定条件" * 1000 + "全部满足时才能执行。",
    )
    assert extract(node, [message]) == ()
    assert message.content.endswith("全部满足时才能执行。")


def synthetic_path(length=8):
    from app.models import Message, Node

    ancestors = []
    for index in range(1, length + 1):
        node = Node(
            id=index,
            tree_id=1,
            parent_id=index - 1 if index > 1 else None,
            title=f"主题 {index}",
            status="complete",
        )
        messages = [
            Message(id=index * 2 - 1, node_id=index, role="user", content=f"问题 {index}？"),
            Message(
                id=index * 2, node_id=index, role="assistant", content=f"这是问题 {index} 的解释。"
            ),
        ]
        ancestors.append((node, messages))
    current = Node(id=length + 1, tree_id=1, parent_id=length, title="当前追问", status="pending")
    return ancestors, current


def context_text(system, messages):
    texts = [system]
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part.get("text", "") for part in content if part.get("type") == "text")
    return "\n".join(texts)


def test_long_path_compaction_preserves_conditions_quote_question_and_original_records():
    from app.context import build_context

    ancestors, current = synthetic_path()
    condition = "只有事先得到明确授权，才允许修改外部系统。"
    ancestors[0][1][1].content = "早期普通背景。" * 500 + condition + "末尾普通背景。" * 500
    current.seed_text = "本次引用原文不能丢失。"
    current.learning_note = "我还不理解这里的适用条件。"
    original_nodes = [node.model_dump() for node, _ in ancestors] + [current.model_dump()]
    original_messages = [
        [message.model_dump() for message in messages] for _, messages in ancestors
    ]
    question = "这里为什么要求授权？保留末尾条件 ONLY_WITH_EXPLICIT_PERMISSION。"
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)

    system, messages = build_context(ancestors, current, [], question, policy=policy)

    assert estimate_request_tokens(system, messages) <= policy.input_budget
    assert condition in context_text(system, messages)
    assert current.seed_text in system and current.learning_note in system
    assert messages[-1] == {"role": "user", "content": question}
    assert "结构化" in system and "省略" in system and "read_learning_source" in system
    assert "node=1" in system and "message=2" in system
    assert [node.model_dump() for node, _ in ancestors] + [current.model_dump()] == original_nodes
    assert [
        [message.model_dump() for message in group] for _, group in ancestors
    ] == original_messages


def test_changed_source_does_not_reuse_stale_compacted_claim():
    from app.context import build_context

    ancestors, current = synthetic_path()
    old_condition = "只有批准甲条件，才允许执行。"
    new_condition = "只有批准乙条件，才允许执行。"
    ancestors[0][1][1].content = "合成背景。" * 1000 + old_condition
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    first = build_context(ancestors, current, [], "执行前提是什么？", policy=policy)
    assert old_condition in context_text(*first)
    ancestors[0][1][1].content = ancestors[0][1][1].content.replace(old_condition, new_condition)
    second = build_context(ancestors, current, [], "执行前提是什么？", policy=policy)
    assert new_condition in context_text(*second)
    assert old_condition not in context_text(*second)


def test_latest_incomplete_attempt_is_not_replaced_by_an_older_completed_claim():
    from app.context import build_context
    from app.models import Message

    ancestors, current = synthetic_path(length=1)
    ancestors[0][1][1].content = "旧完整回答中的过时结论。"
    ancestors[0][1].append(
        Message(
            id=3, node_id=1, role="assistant", status="interrupted", content="最新中断的半截结论。"
        )
    )
    system, messages = build_context(ancestors, current, [], "重新解释条件。")
    text = context_text(system, messages)
    assert "过时结论" not in text and "半截结论" not in text
    assert "问题 1" in text


def test_compaction_excludes_foreign_tree_sources_even_if_they_match_query():
    from app.context import build_context
    from app.models import Message, Node

    ancestors, current = synthetic_path()
    ancestors[0][1][1].content = "普通背景。" * 1000
    forbidden = "其他知识树的授权秘密。"
    ancestors.insert(
        0,
        (
            Node(id=999, tree_id=2, title="其他树"),
            [Message(id=999, node_id=999, role="assistant", content=forbidden)],
        ),
    )
    system, messages = build_context(
        ancestors,
        current,
        [],
        "授权秘密是什么？",
        policy=ContextPolicy(window_tokens=8192, output_tokens=1024),
    )
    assert forbidden not in context_text(system, messages)
    assert "node=999" not in system


def test_current_quote_that_exceeds_budget_is_not_silently_shortened():
    from app.context import build_context

    ancestors, current = synthetic_path(length=1)
    current.seed_text = "必须完整保留的本次选文" * 1000
    original = current.seed_text
    with pytest.raises(ContextBudgetExceeded):
        build_context(
            ancestors,
            current,
            [],
            "解释选文",
            policy=ContextPolicy(window_tokens=4096, output_tokens=512),
        )
    assert current.seed_text == original


def test_compacted_old_images_are_marked_unavailable_while_current_image_is_kept():
    from app.context import build_context

    ancestors, current = synthetic_path(length=1)
    ancestors[0][1][0].images = ["data:image/png;base64," + "A" * 24]
    latest_image = "data:image/png;base64," + "B" * 24
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    system, messages = build_context(
        ancestors, current, [], "当前图片中的条件？", [latest_image], policy=policy
    )
    assert estimate_request_tokens(system, messages) <= policy.input_budget
    assert messages[-1]["content"][1]["image_url"]["url"] == latest_image
    assert "历史图片" in system and "不可见" in system
    assert all("A" * 24 not in str(message) for message in messages)


def test_multiline_memory_is_kept_or_omitted_as_one_record_with_its_conditions():
    from app.context import build_context

    _, current = synthetic_path(length=1)
    claim = "允许删除已有文件。"
    condition = "只有" + "完整备份已经得到确认，" * 70 + "才执行删除。"
    record = f"- [记忆 42；来源节点 1] {claim}\n  {condition}"
    memory = "【当前路径历史记忆】\n" + record
    system, messages = build_context(
        [],
        current,
        [],
        "操作之前有什么前提？",
        memory_note=memory,
        policy=ContextPolicy(window_tokens=4096, output_tokens=2048),
    )
    assert estimate_request_tokens(system, messages) <= ContextPolicy(4096, 2048).input_budget
    assert (claim in system) == (condition in system), (
        "do not keep a conclusion while dropping its condition"
    )


def test_context_diagnostics_report_estimates_without_private_source_text():
    import json

    from app.context import build_context

    ancestors, current = synthetic_path()
    private_marker = "SYNTHETIC_PRIVATE_SOURCE_MARKER"
    ancestors[0][1][1].content = private_marker * 1000 + "只有明确授权才执行。"
    policy = ContextPolicy(window_tokens=8192, output_tokens=1024)
    diagnostics = {}
    system, messages = build_context(
        ancestors, current, [], "执行的条件是什么？", policy=policy, diagnostics=diagnostics
    )
    assert diagnostics["compacted"] is True
    assert diagnostics["raw_estimate"] > diagnostics["estimated_input_tokens"]
    assert diagnostics["estimated_input_tokens"] == estimate_request_tokens(system, messages)
    assert diagnostics["estimated_input_tokens"] <= policy.input_budget
    assert private_marker not in json.dumps(diagnostics)
