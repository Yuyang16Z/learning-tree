"""上下文脊柱的纯单测：不联网，验证「只喂脊柱、不喂兄弟分支」。"""

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

    # 祖先摘要进 system
    assert "什么是 MCP" in system
    assert "MCP 是模型和外部工具之间的协议" in system
    # 当前节点的引子进 system
    assert "schema 校验" in system
    # 当前节点历史 + 新问题进 messages，且新问题在最后
    assert messages[0] == {"role": "user", "content": "schema 是啥"}
    assert messages[-1] == {"role": "user", "content": "在哪一步校验？"}


def test_sibling_branch_not_leaked():
    """兄弟分支的内容绝不能出现在上下文里。"""
    root = _node(1, "什么是 MCP", summary="根节点摘要")
    current = _node(2, "工具注册", parent=1, seed="工具注册")

    system, messages = build_context(
        ancestors=[(root, [])],
        current=current,
        current_messages=[],
        question="工具怎么注册？",
    )

    blob = system + str(messages)
    assert "stdio 传输" not in blob  # 这是另一条兄弟分支，压根没传进来
    assert "工具注册" in system


def test_no_summary_falls_back_to_compact():
    """祖先没摘要时，用压缩后的问答兜底，不能报错。"""
    root = _node(1, "根", summary=None)
    root_msgs = [_msg("user", "根节点的问题"), _msg("assistant", "根节点的回答")]
    current = _node(2, "子", parent=1)

    system, messages = build_context([(root, root_msgs)], current, [], "追问")
    assert messages[0]["content"] == "根节点的问题"
    assert messages[1]["content"] == "根节点的回答"
