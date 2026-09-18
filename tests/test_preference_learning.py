"""User-evidenced preference learning and stale-background-write safety, offline only."""

import json
from uuid import uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import llm, service
from app.llm import LLMSpec
from app.models import (
    KnowledgeTree,
    Memory,
    Message,
    Node,
    PreferenceExtraction,
    PreferenceLearningState,
    PreferenceProfile,
    PreferenceSupplement,
)
from app.preference_learning import has_user_evidence

SPEC = LLMSpec(
    label="offline", base_url="https://example.invalid", llm_model="test", api_key="test"
)
QUESTION = "以后默认用中文解释。"


def preference(evidence=QUESTION, content="默认使用中文解释", **changes):
    return {
        "content": content,
        "scope": "global",
        "evidence": evidence,
        "action": "add",
        "replace_id": None,
        "conflicts_manual": False,
    } | changes


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'learning.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(service, "engine", engine)
    with Session(engine) as s:
        s.add_all([KnowledgeTree(id=1, title="数学"), KnowledgeTree(id=2, title="另一话题")])
        s.add_all(
            [
                Node(id=1, tree_id=1, title="首轮", status="complete", request_id="first"),
                Node(id=2, tree_id=1, title="下一轮", status="complete"),
                Node(id=3, tree_id=2, title="别的话题", status="complete"),
            ]
        )
        s.add(Message(id=1, node_id=1, role="user", content=QUESTION))
        s.commit()
    yield engine
    engine.dispose()


def rows(engine, model=PreferenceSupplement):
    with Session(engine) as s:
        return list(s.exec(select(model)).all())


def extract(monkeypatch, result):
    calls = []

    def implementation(*args):
        calls.append(args)
        return result

    monkeypatch.setattr(service, "extract_memories", implementation)
    return calls


def add_supplement(engine, **changes):
    with Session(engine) as s:
        row = PreferenceSupplement(
            **(
                {
                    "content": "默认使用英文",
                    "evidence": "以后默认使用英文",
                    "source_node_id": 2,
                    "source_tree_id": 1,
                }
                | changes
            )
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


@pytest.mark.parametrize(
    "question,evidence",
    [
        (QUESTION, QUESTION),
        ("我更喜欢先举例，再讲概念。", "我更喜欢先举例，再讲概念"),
        ("I prefer examples before equations.", "I prefer examples before equations."),
        ("学数学时请始终保留推导。", "学数学时请始终保留推导"),
    ],
)
def test_explicit_durable_preferences_are_supported(question, evidence):
    assert has_user_evidence(question, evidence)


@pytest.mark.parametrize(
    "question,evidence",
    [
        ("这次请详细讲解。", "这次请详细讲解"),
        ("这次我喜欢简洁点", "我喜欢简洁点"),
        ("解释这个名词", "我偏好简洁"),
        ("模型说：“以后默认用中文解释。”这是什么意思？", QUESTION),
        ("请翻译：I prefer examples.", "I prefer examples."),
        ("> I prefer examples.\n这句是什么意思？", "I prefer examples."),
        ("`I prefer examples.` 是原文", "I prefer examples."),
        ("```\nI prefer examples.\n```", "I prefer examples."),
        ("他说以后默认用中文解释。", "以后默认用中文解释。"),
        ("He says 'I prefer examples.'", "I prefer examples."),
        ("比如我喜欢英文", "我喜欢英文"),
        ("今天我更喜欢先举例", "今天我更喜欢先举例"),
    ],
)
def test_temporary_quoted_reported_or_unowned_text_is_not_preference(question, evidence):
    assert not has_user_evidence(question, evidence)


def test_one_call_creates_provenanced_supplements_and_facts_without_legacy_writes(
    store, monkeypatch
):
    calls = extract(monkeypatch, {"preferences": [preference()], "facts": ["话题结论"]})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "模型的回答")
    assert len(calls) == 1
    row = rows(store)[0]
    assert (row.scope, row.tree_id, row.source_node_id, row.source_tree_id) == (
        "global",
        None,
        1,
        1,
    )
    assert row.evidence == QUESTION and row.status == "active" and not row.user_edited
    assert [(row.kind, row.content) for row in rows(store, Memory)] == [("fact", "话题结论")]
    assert len(rows(store, PreferenceExtraction)) == 1


def test_handwritten_profile_allows_nonconflicting_new_supplements(store, monkeypatch):
    with Session(store) as s:
        s.add(PreferenceProfile(content="先给生活化例子"))
        s.commit()
    calls = extract(monkeypatch, {"preferences": [preference()], "facts": []})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    assert rows(store)[0].status == "active"
    assert rows(store, PreferenceProfile)[0].content == "先给生活化例子"
    assert calls[0][3]["manual_profile"] == "先给生活化例子"


@pytest.mark.parametrize(
    "item",
    [
        "以前的无证据字符串",
        preference(evidence="模型认为用户喜欢中文"),
        preference(conflicts_manual="false"),
        preference(scope="math"),
        preference(replace_id=True),
        preference(content=["中文"]),
    ],
)
def test_unstructured_or_invalid_preferences_never_write(store, monkeypatch, item):
    extract(monkeypatch, {"preferences": [item], "facts": [True, {"bad": 1}, "事实"]})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "模型认为用户喜欢中文")
    assert rows(store) == []
    assert [m.content for m in rows(store, Memory)] == ["事实"]


def test_same_scope_automatic_preference_can_update(store, monkeypatch):
    old = add_supplement(store)
    extract(
        monkeypatch, {"preferences": [preference(action="update", replace_id=old.id)], "facts": []}
    )
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    row = rows(store)[0]
    assert len(rows(store)) == 1 and row.id == old.id
    assert row.content == "默认使用中文解释" and row.revision != old.revision
    assert row.source_node_id == 1 and row.evidence == QUESTION


@pytest.mark.parametrize(
    "changes,pref_changes",
    [
        ({"user_edited": True}, {}),
        ({"scope": "topic", "tree_id": 1}, {}),
        ({"scope": "topic", "tree_id": 2, "source_node_id": 3, "source_tree_id": 2}, {}),
        ({"status": "pending"}, {}),
        ({}, {"conflicts_manual": True}),
    ],
)
def test_locked_wrong_scope_or_conflicting_update_is_pending(
    store, monkeypatch, changes, pref_changes
):
    old = add_supplement(store, **changes)
    candidate = preference(action="update", replace_id=old.id, **pref_changes)
    extract(monkeypatch, {"preferences": [candidate], "facts": []})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    current = rows(store)
    assert len(current) == 2
    assert current[0].content == old.content and current[0].revision == old.revision
    assert current[1].status == "pending"


def test_topic_preference_remains_owned_by_source_tree(store, monkeypatch):
    question = "学数学时请始终保留推导。"
    extract(
        monkeypatch, {"preferences": [preference(evidence=question, scope="topic")], "facts": []}
    )
    service.extract_and_save(SPEC, 1, 1, question, "回答")
    assert (rows(store)[0].scope, rows(store)[0].tree_id) == ("topic", 1)


def test_normalized_duplicate_is_not_readded(store, monkeypatch):
    add_supplement(store, content=" 默认使用中文解释 ")
    extract(monkeypatch, {"preferences": [preference()], "facts": []})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    assert len(rows(store)) == 1


def test_disabled_learning_still_extracts_facts_and_hides_profile_from_extractor(
    store, monkeypatch
):
    with Session(store) as s:
        s.add(PreferenceLearningState(enabled=False))
        s.add(PreferenceProfile(content="手写内容"))
        s.commit()
    calls = extract(monkeypatch, {"preferences": [preference()], "facts": ["事实"]})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    assert rows(store) == [] and len(rows(store, Memory)) == 1
    assert calls[0][3] == {"enabled": False, "manual_profile": "", "existing": []}


@pytest.mark.parametrize("change", ["profile", "setting", "delete", "edit", "add", "source"])
def test_concurrent_preference_changes_suppress_stale_writes_but_keep_facts(
    store, monkeypatch, change
):
    old = add_supplement(store)

    def concurrent(*args):
        with Session(store) as s:
            row = s.get(PreferenceSupplement, old.id)
            if change == "profile":
                s.add(PreferenceProfile(content="新的手写偏好"))
            elif change == "setting":
                s.add(PreferenceLearningState(enabled=False))
            elif change == "delete":
                s.delete(row)
            elif change == "edit":
                row.content = "用户亲自修改的内容"
                row.user_edited = True
                row.revision = uuid4().hex
                s.add(row)
            elif change == "source":
                source = s.get(Node, row.source_node_id)
                source.status = "error"
                s.add(source)
            else:
                s.add(
                    PreferenceSupplement(
                        content="另一轮新增",
                        evidence="以后记住",
                        source_node_id=2,
                        source_tree_id=1,
                    )
                )
            s.commit()
        return {"preferences": [preference(action="update", replace_id=old.id)], "facts": ["事实"]}

    monkeypatch.setattr(service, "extract_memories", concurrent)
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    assert all(row.content != "默认使用中文解释" for row in rows(store))
    assert [m.content for m in rows(store, Memory)] == ["事实"]


@pytest.mark.parametrize("change", ["delete_source", "request", "message", "clear"])
def test_invalid_source_or_reset_discards_all_inflight_results(store, monkeypatch, change):
    def concurrent(*args):
        with Session(store) as s:
            source = s.get(Node, 1)
            if change == "delete_source":
                s.delete(source)
            elif change == "request":
                source.request_id = "replacement"
                s.add(source)
            elif change == "message":
                message = s.get(Message, 1)
                message.content = "用户编辑了问题"
                s.add(message)
            else:
                s.add(PreferenceProfile(reset_revision=uuid4().hex))
            s.commit()
        return {"preferences": [preference()], "facts": ["不能恢复的事实"]}

    monkeypatch.setattr(service, "extract_memories", concurrent)
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    assert rows(store) == [] and rows(store, Memory) == []


def test_deleted_preference_cannot_return_on_answer_regeneration(store, monkeypatch):
    calls = extract(monkeypatch, {"preferences": [preference()], "facts": []})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "第一份回答")
    with Session(store) as s:
        s.delete(s.exec(select(PreferenceSupplement)).one())
        node = s.get(Node, 1)
        node.request_id = "regenerate"
        s.add(node)
        s.commit()
    service.extract_and_save(SPEC, 1, 1, QUESTION, "第二份不同的回答")
    assert len(calls) == 1 and rows(store) == []
    # A fresh user turn may explicitly state it again.
    with Session(store) as s:
        s.add(Message(id=2, node_id=1, role="user", content=QUESTION))
        s.commit()
    service.extract_and_save(SPEC, 1, 1, QUESTION, "新的用户提问")
    assert len(calls) == 2 and len(rows(store)) == 1


def test_failed_provider_is_best_effort_and_does_not_replay(store, monkeypatch):
    calls = []

    def fail(*args):
        calls.append(True)
        raise RuntimeError("offline provider failure")

    monkeypatch.setattr(service, "extract_memories", fail)
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    service.extract_and_save(SPEC, 1, 1, QUESTION, "重试的回答")
    assert calls == [True] and rows(store) == []


def test_extractor_sends_separated_data_and_strictly_filters_collections(monkeypatch):
    captured = []

    def complete(spec, system, messages):
        captured.append((system, json.loads(messages[0]["content"])))
        return json.dumps(
            {"preferences": [preference(), 4], "facts": [False, {"x": 1}, "  事实  "]}
        )

    monkeypatch.setattr(llm, "complete", complete)
    context = {"enabled": True, "manual_profile": "手写内容", "existing": []}
    result = llm.extract_memories(SPEC, QUESTION, "模型自称用户偏好不应成为证据", context)
    assert result == {"preferences": [preference()], "facts": ["事实"]}
    assert captured[0][1] == {
        "question": QUESTION,
        "answer": "模型自称用户偏好不应成为证据",
        "preference_context": context,
    }
    assert "answer、引用、代码、转述" in captured[0][0]


def test_locked_context_is_prioritized_and_overflow_requires_review(store, monkeypatch):
    for number in range(24):
        add_supplement(store, content=f"手写偏好 {number}", user_edited=True)
    add_supplement(store, content="与当前中文提问高度相关的自动内容")
    calls = extract(monkeypatch, {"preferences": [preference()], "facts": []})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    context = calls[0][3]
    assert len(context["existing"]) == 20
    assert all(item["user_edited"] for item in context["existing"])
    assert context["manual_context_incomplete"] and context["existing_truncated"]
    assert rows(store)[-1].status == "pending"


def test_cjk_relevance_keeps_older_related_automatic_preferences_in_prompt(store, monkeypatch):
    related = add_supplement(store, content="默认使用中文")
    for number in range(24):
        add_supplement(store, content=f"其他偏好 {number}")
    calls = extract(monkeypatch, {"preferences": [], "facts": []})
    service.extract_and_save(SPEC, 1, 1, QUESTION, "回答")
    context = calls[0][3]
    assert context["existing"][0]["id"] == related.id
    assert context["existing_truncated"] and not context["manual_context_incomplete"]
