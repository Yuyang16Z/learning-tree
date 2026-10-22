"""Local selection rules for compacted history: synthetic sources, no model or database."""

import pytest

from app import semantic_models
from app.context_compaction import extract, summarize_sources
from app.models import Message, Node

_IDS = iter(range(10_000, 99_999))


def source(*answers: str, question: str = "继续讲。") -> tuple[Node, list[Message]]:
    """One synthetic path node: a question and complete assistant answers."""
    node_id = next(_IDS)
    node = Node(id=node_id, tree_id=1, title=f"合成节点 {node_id}", status="complete")
    messages = [Message(id=next(_IDS), node_id=node_id, role="user", content=question)]
    messages += [
        Message(id=next(_IDS), node_id=node_id, role="assistant", content=text) for text in answers
    ]
    return node, messages


def cost(item: tuple[Node, list[Message]], text: str) -> int:
    """Bytes one rendered excerpt line takes in the excerpt budget."""
    node, messages = item
    excerpt = next(e for e in extract(node, messages) if e.text == text)
    return len(excerpt.render().encode("utf-8")) + 1


def selected(text: str) -> list[str]:
    return [line.split("] ", 1)[1] for line in text.splitlines() if "] " in line]


@pytest.fixture(autouse=True)
def no_semantic_model(monkeypatch):
    """Sentence scoring is lexical only: it must never reach the embedding model."""

    def refuse():
        raise AssertionError("sentence selection must not call the semantic backend")

    monkeypatch.setattr(semantic_models, "get_backend", refuse)


def test_common_words_and_long_sentences_do_not_outrank_a_specific_match():
    query = "为什么 softmax 的分母要用指数和？"
    specific = "softmax 的分母是所有指数之和：分母把每个指数和加起来，用于归一化。"
    filler = (
        "我们的分析里这个数和那个数都要用到，为什么不直接用指定的写法，"
        "什么时候要用它其实要看具体情况，我们的分工也要用同样的思路来理解。"
    )
    background = ["后面的分析也要用到这些记号。", "我们的分工要用表格记下来。"]
    first, second = source(specific, *background), source(filler)
    budget = max(cost(first, specific), cost(second, filler)) + 5
    chosen = selected(summarize_sources([first, second], query, budget))
    assert specific in chosen, chosen
    assert filler not in chosen, "a long sentence of common words must not win on shared filler"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("但它在实践中很直观。", "历史解释（未经验证）"),
        ("但这只适用于离散分布。", "适用条件"),
        ("但如果分母为零，结果就没有定义。", "适用条件"),
        ("只有分母不为零时才成立。", "适用条件"),
        ("However, it is easy to compute.", "历史解释（未经验证）"),
    ],
)
def test_a_bare_contrast_is_not_an_applicable_condition(text, expected):
    node, messages = source(text)
    categories = [e.category for e in extract(node, messages) if e.role == "assistant"]
    assert categories == [expected]


def test_conditions_cannot_crowd_out_relevant_explanations():
    query = "交叉熵损失如何衡量两个分布的差异？"
    conditions = [f"只有满足第{n}号前提时，这一步才可以执行。" for n in range(1, 7)]
    explanations = [
        "交叉熵衡量的是两个分布之间的差异大小。",
        "交叉熵越小，预测分布越接近真实分布。",
        "交叉熵损失常和 softmax 一起使用。",
    ]
    item = source(*conditions, *explanations)
    budget = sum(cost(item, text) for text in conditions[:4])
    chosen = selected(summarize_sources([item], query, budget))
    condition_bytes = sum(cost(item, text) for text in chosen if text in conditions)
    assert condition_bytes <= 0.4 * budget, (condition_bytes, budget, chosen)
    assert sum(text in explanations for text in chosen) >= 2, chosen


def test_condition_quota_leaves_no_budget_unused_when_nothing_else_competes():
    conditions = [f"只有满足第{n}号前提时，这一步才可以执行。" for n in range(1, 7)]
    item = source(*conditions)
    budget = sum(cost(item, text) for text in conditions[:4])
    chosen = selected(summarize_sources([item], "交叉熵是什么？", budget))
    assert sum(cost(item, text) for text in chosen) > 0.4 * budget
    assert len(chosen) == 4


def test_a_highly_relevant_explanation_outranks_an_unrelated_condition():
    query = "梯度消失为什么会让深层网络难以训练？"
    condition = "只有在拿到书面授权以后，才可以删除生产数据库。"
    explanation = "梯度消失时，深层网络前几层的梯度接近零，参数几乎不更新，所以难以训练。"
    first, second = source(condition), source(explanation)
    budget = max(cost(first, condition), cost(second, explanation)) + 5
    assert selected(summarize_sources([first, second], query, budget)) == [explanation]


def test_corrections_stay_first_even_against_a_relevant_condition():
    query = "学习率需要满足什么条件才能收敛？"
    correction = "更正：前面把批大小写成了 64，实际实验用的是 32。"
    condition = "只有学习率足够小，梯度下降才能保证收敛。"
    first, second = source(correction), source(condition)
    budget = max(cost(first, correction), cost(second, condition)) + 5
    assert selected(summarize_sources([first, second], query, budget)) == [correction]


def test_selection_is_deterministic_and_keeps_whole_cited_sentences():
    query = "softmax 为什么要减去最大值？"
    item = source(
        "softmax 先减去最大值，避免指数溢出。",
        "只有数值稳定时，结果才可靠。",
        "更正：上一段的公式漏了括号。",
        "这里再举一个例子。",
    )
    runs = {summarize_sources([item], query, 400) for _ in range(5)}
    assert len(runs) == 1
    (text,) = runs
    node, messages = item
    originals = {m.id: m.content for m in messages}
    for line in text.splitlines():
        excerpt = next(e for e in extract(node, messages) if e.render() == line)
        assert excerpt.text == originals[excerpt.message_id][excerpt.start : excerpt.end]
