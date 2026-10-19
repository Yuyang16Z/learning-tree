"""Offline unit tests: include the active ancestor path and exclude sibling branches."""

from app.context import BASE_SYSTEM, build_context, stable_prefix
from app.context_budget import ContextPolicy
from app.models import Message, Node


def _node(nid, title, parent=None, seed=None, summary=None):
    return Node(id=nid, tree_id=1, parent_id=parent, title=title, seed_text=seed, summary=summary)


def _msg(role, content):
    return Message(node_id=1, role=role, content=content)


def test_ancestors_in_system_current_in_messages():
    root = _node(1, "什么是 MCP", summary="MCP 是模型和外部工具之间的协议。")
    current = _node(3, "Schema 校验", parent=2, seed="schema 校验")
    current_msgs = [_msg("user", "schema 是啥"), _msg("assistant", "参数的结构约束")]

    system, messages = build_context(
        ancestors=[(root, [])],
        current=current,
        current_messages=current_msgs,
        question="在哪一步校验？",
    )

    # Include ancestor summaries in the system context.
    assert "什么是 MCP" in system
    assert "MCP 是模型和外部工具之间的协议" in system
    # Include the current node's source passage in the system context.
    assert "schema 校验" in system
    # Include current-node history, followed by the new question.
    assert messages[0] == {"role": "user", "content": "schema 是啥"}
    assert messages[-1] == {"role": "user", "content": "在哪一步校验？"}


def test_sibling_branch_not_leaked():
    """Sibling-branch content must never enter the active context."""
    root = _node(1, "什么是 MCP", summary="根节点摘要")
    current = _node(2, "工具注册", parent=1, seed="工具注册")

    system, messages = build_context(
        ancestors=[(root, [])],
        current=current,
        current_messages=[],
        question="工具怎么注册？",
    )

    blob = system + str(messages)
    assert "stdio 传输" not in blob  # This sibling branch is deliberately excluded from the input.
    assert "工具注册" in system


def test_stable_path_precedes_current_node_and_per_turn_material_comes_last():
    root = _node(1, "ROOT_TITLE", summary="ROOT_IMPORTED_SUMMARY")
    root.learning_note = "ROOT_NOTE"
    current = _node(2, "CURRENT_TITLE", parent=1, seed="CURRENT_QUOTE")
    current.learning_note = "CURRENT_NOTE"
    diagnostics = {}
    system, _ = build_context(
        [(root, [])],
        current,
        [],
        "问题",
        memory_note="OPTIONAL_MEMORY",
        documents=[
            {
                "id": "doc",
                "name": "OPTIONAL_DOCUMENT.txt",
                "characters": 32,
                "warnings": [],
                "sections": [{"label": "1", "text": "OPTIONAL_DOCUMENT_CONTENT"}],
            }
        ],
        diagnostics=diagnostics,
    )
    # The exact base prefix lets inline explanations swap only the fixed rules.
    assert system.startswith(BASE_SYSTEM)
    order = [
        "学习路径：ROOT_TITLE",
        "ROOT_IMPORTED_SUMMARY",
        "ROOT_NOTE",
        "当前节点：CURRENT_TITLE",
        "CURRENT_NOTE",
        "CURRENT_QUOTE",
        "OPTIONAL_DOCUMENT",
        "OPTIONAL_MEMORY",
    ]
    positions = [system.index(marker) for marker in order]
    assert positions == sorted(positions), order
    stable = diagnostics["stable_system"]
    assert (
        stable
        == stable_prefix([(root, [])])
        == BASE_SYSTEM
        + "\n"
        + "\n".join(
            [
                "学习路径：ROOT_TITLE（node=1）",
                "历史导入摘要（无原文可核对）：ROOT_IMPORTED_SUMMARY",
                "用户在「ROOT_TITLE」留下的理解笔记：ROOT_NOTE",
            ]
        )
    )
    assert system.startswith(stable)
    for volatile in ("CURRENT_TITLE", "CURRENT_QUOTE", "OPTIONAL_MEMORY", "OPTIONAL_DOCUMENT"):
        assert volatile not in stable


def test_compacted_context_keeps_only_the_fixed_rules_as_the_stable_prefix():
    root = _node(1, "根", summary=None)
    history = [_msg("user", "早期问题"), _msg("assistant", "早期背景。" * 6000)]
    current = _node(2, "当前", parent=1, seed="引用")
    diagnostics = {}
    system, _ = build_context(
        [(root, history)],
        current,
        [],
        "追问",
        policy=ContextPolicy(8192, 1024),
        diagnostics=diagnostics,
    )
    assert diagnostics["compacted"] is True
    assert diagnostics["stable_system"] == BASE_SYSTEM and system.startswith(BASE_SYSTEM)


def test_no_summary_falls_back_to_compact():
    """Fall back to ancestor question/answer excerpts when no summary is available."""
    root = _node(1, "根", summary=None)
    root_msgs = [_msg("user", "根节点的问题"), _msg("assistant", "根节点的回答")]
    current = _node(2, "子", parent=1)

    system, messages = build_context([(root, root_msgs)], current, [], "追问")
    assert messages[0]["content"] == "根节点的问题"
    assert messages[1]["content"] == "根节点的回答"


def test_document_excerpts_keep_to_a_third_of_the_room_in_a_two_byte_script():
    """Document excerpts are budgeted in UTF-8 bytes converted at the documents' own
    density, so Cyrillic (about 1.8 bytes per unit) does not take about half the room."""
    from app.context import _full_system
    from app.context_budget import estimate_request_tokens, estimate_text_tokens
    from app.document_context import NOTICE

    policy = ContextPolicy(8192, 1024)
    current = _node(1, "Правила")
    question = "Когда действует правило?"
    text = " ".join(f"Правило {i} действует только с явного разрешения." for i in range(400))
    document = {
        "id": "doc-1",
        "name": "rules.txt",
        "characters": len(text),
        "warnings": [],
        "sections": [{"label": "全文", "text": text}],
    }
    system, _ = build_context([], current, [], question, documents=[document], policy=policy)
    note = system[system.index(NOTICE) :]
    mandatory = estimate_request_tokens(
        _full_system([], current, ""), [{"role": "user", "content": question}]
    )
    room = (int(policy.input_budget * policy.compaction_ratio) - mandatory) // 3
    assert "Правило 0 действует" in note
    assert estimate_text_tokens(note) <= room


def test_byte_rate_follows_the_material_within_its_bounds():
    from app.context import BYTES_PER_UNIT, MIN_BYTES_PER_UNIT, bytes_per_unit

    assert bytes_per_unit([]) == bytes_per_unit(["", None]) == BYTES_PER_UNIT
    assert bytes_per_unit(["Plain English prose about trees."]) == BYTES_PER_UNIT
    assert bytes_per_unit(["中文学习记录的内容"]) == BYTES_PER_UNIT, "CJK stays conservative"
    russian = bytes_per_unit(["Синтаксический анализатор проверяет вложенную структуру."])
    assert MIN_BYTES_PER_UNIT <= russian < 1.9
    # Very long material is sampled in spread windows; the blend stays within bounds.
    mixed = bytes_per_unit(["Анализатор проверяет. " * 5000, "Plain English. " * 5000])
    assert russian < mixed < BYTES_PER_UNIT
