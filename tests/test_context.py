"""Offline unit tests: include the active ancestor path and exclude sibling branches."""

from app.context import build_context
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


def test_no_summary_falls_back_to_compact():
    """Fall back to ancestor question/answer excerpts when no summary is available."""
    root = _node(1, "根", summary=None)
    root_msgs = [_msg("user", "根节点的问题"), _msg("assistant", "根节点的回答")]
    current = _node(2, "子", parent=1)

    system, messages = build_context([(root, root_msgs)], current, [], "追问")
    assert messages[0]["content"] == "根节点的问题"
    assert messages[1]["content"] == "根节点的回答"
