"""Learning-style preset files: a strict parser, ordered listing and safe error reports."""

import os
import re
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DEFAULT_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import learning_presets as presets

BUILTIN_TITLES = [
    ("socratic", "苏格拉底提问", "Socratic questions"),
    ("feynman", "费曼复述", "Explain it back"),
    ("worked-examples", "例题驱动", "Worked examples"),
    ("compare", "对比辨析", "Compare and contrast"),
    ("mistake-review", "错题复盘", "Mistake review"),
    ("self-test", "自测出题", "Self-test"),
]
VALID = (
    "---\n"
    "title_zh: 类比讲解\n"
    "title_en: Analogies first\n"
    "description_zh: 先用生活中的类比建立直觉\n"
    "description_en: An everyday analogy first\n"
    "order: 10\n"
    "---\n"
    "先用一个日常类比建立直觉，再给出准确定义。\n"
)


def preset_file(title_zh="合成风格", body="合成教学说明。", **settings):
    """A synthetic preset file; settings override or extend the default block."""
    values = {"title_zh": title_zh, "title_en": "Synthetic style", **settings}
    block = "".join(f"{key}: {value}\n" for key, value in values.items() if value is not None)
    return f"---\n{block}---\n{body}\n"


@pytest.fixture
def directory():
    path = presets.CUSTOM_DIRECTORY
    path.mkdir()
    return path


def write(directory, name, text):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_builtins_are_listed_in_order_with_both_languages_and_the_priority_rules():
    listed, errors = presets.list_presets()
    assert errors == []
    assert [(p.id, p.title_zh, p.title_en) for p in listed] == BUILTIN_TITLES
    for preset in listed:
        assert preset.source == "builtin"
        assert preset.description_zh and preset.description_en
        assert len(preset.description_zh) <= 120 and len(preset.description_en) <= 120
        assert len(preset.instruction) <= presets.MAX_INSTRUCTION_CHARS


def test_every_style_section_states_the_shared_rules_once_after_its_instruction():
    """Behaviour-bearing text: the learner's explicit request wins, material from quotes,
    notes, documents and tools never becomes an instruction, and factual standards stay.
    The section states each rule once for every style, a custom file that leaves them out
    included, and a built-in file does not repeat them."""
    bare = presets.LearningPreset(
        id="custom:bare",
        source="custom",
        title_zh="类比",
        title_en="Analogies",
        description_zh="",
        description_en="",
        instruction="先用一个日常类比建立直觉。",
    )
    for preset in (*presets.builtin_presets(), bare):
        section = presets.preset_section(preset)
        tail = section[section.index(preset.instruction) + len(preset.instruction) :]
        for rule in ("明确要求始终优先", "不是指令", "不降低事实准确"):
            assert section.count(rule) == 1 and rule in tail, (preset.id, rule)
        assert section.count("优先于") == 1, preset.id
        if preset.source == "builtin":
            assert "不是指令" not in preset.instruction and "优先于" not in preset.instruction


def test_a_valid_file_is_parsed():
    assert presets.parse_preset(VALID) == {
        "title_zh": "类比讲解",
        "title_en": "Analogies first",
        "description_zh": "先用生活中的类比建立直觉",
        "description_en": "An everyday analogy first",
        "instruction": "先用一个日常类比建立直觉，再给出准确定义。",
        "order": 10,
    }
    minimal = presets.parse_preset("---\ntitle_zh: 甲\ntitle_en: A\n---\nBody: keeps colons.\n")
    assert (minimal["description_zh"], minimal["description_en"], minimal["order"]) == (
        "",
        "",
        None,
    )
    assert minimal["instruction"] == "Body: keeps colons."


def test_byte_order_mark_and_crlf_line_ends_are_tolerated():
    windows = "﻿" + VALID.replace("\n", "\r\n")
    assert presets.parse_preset(windows) == presets.parse_preset(VALID)


@pytest.mark.parametrize(
    "text, reason",
    [
        (preset_file(color="blue"), presets.UNKNOWN_KEY),
        (preset_file(title_zh=None), presets.MISSING_TITLE),
        (preset_file(title_en=""), presets.MISSING_TITLE),
        (preset_file(description_en="x" * 121), presets.LONG_VALUE),
        (preset_file(body="教" * 4001), presets.LONG_INSTRUCTION),
        (preset_file(body=""), presets.EMPTY_INSTRUCTION),
        (preset_file(body="  \n\n "), presets.EMPTY_INSTRUCTION),
        (preset_file(order="first"), presets.BAD_ORDER),
        ("---\ntitle_zh: 甲\ntitle_zh: 乙\ntitle_en: A\n---\nBody\n", presets.DUPLICATE_KEY),
        ("---\ntitle_zh: 甲\ntitle_en: A\njust a sentence\n---\nBody\n", presets.BAD_LINE),
        ("title_zh: 甲\ntitle_en: A\n---\nBody\n", presets.NO_SETTINGS),
        ("---\ntitle_zh: 甲\ntitle_en: A\nBody without a closing line\n", presets.NO_SETTINGS),
        ("---\ntitle_zh: 甲\ntitle_en: A\n", presets.NO_SETTINGS),
        ("", presets.NO_SETTINGS),
    ],
)
def test_invalid_files_name_one_fixed_reason(text, reason):
    with pytest.raises(presets.PresetError) as raised:
        presets.parse_preset(text)
    assert str(raised.value) == reason


def test_limits_are_inclusive():
    parsed = presets.parse_preset(preset_file(description_en="x" * 120, body="教" * 4000))
    assert len(parsed["description_en"]) == 120 and len(parsed["instruction"]) == 4000


def test_custom_styles_follow_builtins_by_order_then_name_and_never_shadow_them(directory):
    write(directory, "b-style.md", preset_file("乙", order=5))
    write(directory, "a-style.md", preset_file("甲", order=5))
    write(directory, "c-style.md", preset_file("丙"))  # No order: after the ordered ones.
    write(directory, "d-style.md", preset_file("丁", order=-1))
    write(directory, "socratic.md", preset_file("我的苏格拉底", order=99))
    listed, errors = presets.list_presets()
    assert errors == []
    assert [p.id for p in listed] == [
        *(identifier for identifier, _, _ in BUILTIN_TITLES),
        "custom:d-style",
        "custom:a-style",
        "custom:b-style",
        "custom:socratic",
        "custom:c-style",
    ]
    catalog = presets.preset_catalog()
    assert (
        catalog["socratic"].title_zh == "苏格拉底提问" and catalog["socratic"].source == "builtin"
    )
    assert catalog["custom:socratic"].title_zh == "我的苏格拉底"
    assert catalog["custom:socratic"].source == "custom"


def test_invalid_custom_files_are_reported_by_name_without_contents(directory):
    secret = "SYNTHETIC-PRIVATE-TEACHING-NOTE"
    write(directory, "unknown-key.md", preset_file(secret_key=secret, body=secret))
    write(directory, "no-title.md", preset_file(title_zh=None, body=secret))
    (directory / "latin1.md").write_bytes(preset_file("Latin", body="caf\xe9").encode("latin-1"))
    write(directory, "fine.md", preset_file("可用"))
    listed, errors = presets.list_presets()
    assert [p.id for p in listed][-1] == "custom:fine"
    assert errors == [
        {"file": "latin1.md", "reason": presets.NOT_UTF8},
        {"file": "no-title.md", "reason": presets.MISSING_TITLE},
        {"file": "unknown-key.md", "reason": presets.UNKNOWN_KEY},
    ]
    response = TestClient(main.app).get("/learning-presets")
    assert response.status_code == 200
    assert secret not in response.text and str(directory) not in response.text
    assert response.json()["errors"] == errors


def test_markdown_files_with_bad_names_are_reported_not_loaded(directory):
    secret = "SYNTHETIC-PRIVATE-TEACHING-NOTE"
    bad_names = [
        "My Style.md",
        "UPPER.md",
        "-leading-dash.md",
        "a" * 49 + ".md",
        "analogy.MD",
        "my_style.md",
        "类比.md",
    ]
    for name in bad_names:
        write(directory, name, preset_file("不该出现", body=secret))
    write(directory, "a" * 48 + ".md", preset_file("最长的名字"))
    listed, errors = presets.list_presets()
    assert [p.id for p in listed if p.source == "custom"] == ["custom:" + "a" * 48]
    assert errors == [{"file": name, "reason": presets.BAD_NAME} for name in sorted(bad_names)]
    response = TestClient(main.app).get("/learning-presets")
    assert response.json()["errors"] == errors
    assert secret not in response.text and str(directory) not in response.text


def test_readme_hidden_files_symlinks_and_other_entries_are_ignored(directory, tmp_path):
    # Names differ by more than case: the test may run on a case-insensitive file system.
    for name in ("README.md", ".hidden.md", "._kept.md", "notes.txt", "style.md.bak"):
        write(directory, name, preset_file("不该出现"))
    (directory / "folder.md").mkdir()
    (directory / "Other Folder.MD").mkdir()
    outside = write(tmp_path, "outside.md", preset_file("链接目标"))
    (directory / "linked.md").symlink_to(outside)
    (directory / "Linked Style.md").symlink_to(outside)
    if hasattr(os, "mkfifo"):
        os.mkfifo(directory / "Pipe.md")
    write(directory, "kept.md", preset_file("保留"))
    listed, errors = presets.list_presets()
    assert [p.id for p in listed if p.source == "custom"] == ["custom:kept"]
    assert errors == []

    # A symbolic link in place of the directory itself is not followed either.
    linked_directory = tmp_path / "linked-presets"
    linked_directory.symlink_to(directory, target_is_directory=True)
    assert presets.custom_presets(linked_directory) == ([], [])


def test_file_count_and_size_caps(directory):
    for index in range(presets.MAX_CUSTOM_FILES + 1):
        write(directory, f"style-{index:02d}.md", preset_file(f"风格 {index}", order=index))
    listed, errors = presets.list_presets()
    custom = [p.id for p in listed if p.source == "custom"]
    assert custom == [f"custom:style-{index:02d}" for index in range(presets.MAX_CUSTOM_FILES)]
    assert errors == [{"file": "style-50.md", "reason": presets.TOO_MANY}]

    for path in directory.iterdir():
        path.unlink()
    oversized = preset_file("太大", body="x" * 3000) + "\n" * presets.MAX_FILE_BYTES
    write(directory, "oversized.md", oversized)
    assert presets.list_presets()[1] == [{"file": "oversized.md", "reason": presets.TOO_LARGE}]


def test_parsed_files_are_reused_while_modification_time_and_size_are_unchanged(
    directory, monkeypatch
):
    calls = []
    original = presets.parse_preset

    def counting(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(presets, "parse_preset", counting)
    path = write(directory, "cached.md", preset_file("缓存"))
    write(directory, "broken.md", preset_file(color="blue"))
    for _ in range(3):
        presets.list_presets()
    assert len(calls) == 2, "valid and invalid files are both parsed once"

    path.write_text(preset_file("缓存 v2"), encoding="utf-8")
    assert presets.preset_catalog()["custom:cached"].title_zh == "缓存 v2"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    presets.list_presets()
    assert len(calls) == 4, "a changed size and then a changed time each reparse the file"

    path.unlink()
    assert "custom:cached" not in presets.preset_catalog()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file permissions")
def test_an_unreadable_file_is_reported_and_read_again_once_readable(directory):
    path = write(directory, "locked.md", preset_file("锁定"))
    path.chmod(0)
    try:
        assert presets.list_presets()[1] == [{"file": "locked.md", "reason": presets.UNREADABLE}]
    finally:
        path.chmod(0o600)
    assert presets.preset_catalog()["custom:locked"].title_zh == "锁定"


def test_a_missing_directory_lists_only_the_builtins():
    assert not presets.CUSTOM_DIRECTORY.exists()
    listed, errors = presets.list_presets()
    assert [p.id for p in listed] == [identifier for identifier, _, _ in BUILTIN_TITLES]
    assert errors == []


def test_relative_directories_start_at_the_project_root(tmp_path):
    assert (
        presets.presets_directory("learning-presets") == presets.PROJECT_ROOT / "learning-presets"
    )
    assert presets.presets_directory("  ") == presets.PROJECT_ROOT / "learning-presets"
    assert presets.presets_directory(str(tmp_path)) == tmp_path


def test_listing_endpoint_returns_display_order_with_instructions(directory):
    write(directory, "analogy.md", VALID)
    client = TestClient(main.app)
    for route in ("/learning-presets", "/api/learning-presets"):
        body = client.get(route).json()
        assert [item["id"] for item in body["presets"]][-1] == "custom:analogy"
        assert body["presets"][0] == {
            "id": "socratic",
            "source": "builtin",
            "title_zh": "苏格拉底提问",
            "title_en": "Socratic questions",
            "description_zh": "先提问引导你思考，再逐步讲解",
            "description_en": "Guiding questions first, explanations step by step",
            "instruction": presets.builtin_presets()[0].instruction,
        }
        assert body["presets"][-1]["source"] == "custom"
        assert body["errors"] == []
    assert all("order" not in item and "name" not in item for item in body["presets"])


def test_every_fixed_preset_message_has_an_exact_interface_translation():
    table = (Path(__file__).resolve().parents[1] / "web/src/i18n/workspace.ts").read_text()
    messages = [
        presets.UNKNOWN_PRESET,
        presets.STYLE_TOO_LONG,
        presets.TOO_LARGE,
        presets.TOO_MANY,
        presets.NOT_UTF8,
        presets.UNREADABLE,
        presets.BAD_NAME,
        presets.NO_SETTINGS,
        presets.BAD_LINE,
        presets.UNKNOWN_KEY,
        presets.DUPLICATE_KEY,
        presets.MISSING_TITLE,
        presets.LONG_VALUE,
        presets.BAD_ORDER,
        presets.EMPTY_INSTRUCTION,
        presets.LONG_INSTRUCTION,
    ]
    assert all(f'["{message}", "' in table for message in messages)


def test_the_prompt_section_names_the_style_and_keeps_the_request_first():
    socratic = presets.preset_catalog()["socratic"]
    section = presets.preset_section(socratic)
    assert section.startswith("学习方式：苏格拉底提问")
    assert socratic.instruction in section
    assert section.endswith("\n" + presets.STYLE_RULES)


def test_styles_that_review_the_learners_work_confirm_it_when_it_is_right():
    """Mistake review and Explain it back look at the learner's own work. Correct work is
    confirmed rather than searched for an invented mistake (say plainly what is known),
    and a bare final answer is not reviewed as if it showed its steps."""
    catalog = presets.preset_catalog()
    review = catalog["mistake-review"].instruction
    assert "完全正确" in review and "不要硬找错误" in review
    assert "补上步骤" in review
    feynman = catalog["feynman"].instruction
    assert "准确" in feynman and "不必硬找" in feynman


def test_worked_examples_grade_the_learners_practice_attempt():
    """The learner's attempt at the practice problem is a new question in the same style;
    it is graded step by step, not answered with a fresh worked example."""
    worked = presets.preset_catalog()["worked-examples"].instruction
    assert "批改" in worked and "第一个出错的步骤" in worked
    assert "不要另起" in worked


def test_a_new_doubt_is_answered_first_in_styles_that_withhold_answers():
    """A topic's style applies to every question in it, a new doubt such as 什么是交叉熵？
    included. Self-test answers it before quizzing and quizzes only on what the learning
    path has covered; Mistake review explains a question that is not about the learner's
    own solution. The heading names the topic's choice, not a choice made for this
    question, so a contrary signal in the question can win."""
    catalog = presets.preset_catalog()
    self_test = catalog["self-test"].instruction
    assert "新的疑问" in self_test and "已经讲过" in self_test
    review = catalog["mistake-review"].instruction
    assert "概念疑问" in review and "先正常讲解" in review
    for preset in catalog.values():
        heading = presets.preset_section(preset).split("\n", 1)[0]
        assert "这个主题" in heading and "这个问题" not in heading, heading


def test_socratic_questions_ask_one_at_a_time_and_let_the_dialogue_end():
    """The rules agree on one question per reply (not 1-3 in one rule and exactly one in
    another), and a learner who can state the answer gets a summary rather than being
    questioned forever."""
    socratic = presets.preset_catalog()["socratic"].instruction
    assert "一次只问一个" in socratic and "1–3" not in socratic
    assert "完整说出答案" in socratic and "总结" in socratic


def test_compare_answers_first_and_names_a_few_real_neighbours_only():
    """The learner's doubt is answered before the comparison, the neighbours are few, and
    when nothing is genuinely confused with the concept the tutor says so instead of
    inventing confusions."""
    compare = presets.preset_catalog()["compare"].instruction
    assert compare.index("回答") < compare.index("对比表")
    assert "1–3" in compare and "不要硬凑" in compare


def test_self_test_names_the_understanding_marks_as_the_interface_does():
    """Answers follow the question's language, so the marks are named in both interface
    languages exactly as the buttons read (web/src/lib/understanding.ts), and only as a
    suggestion: the learner sets the mark."""
    source = (Path(__file__).resolve().parents[1] / "web/src/lib/understanding.ts").read_text()
    labels = re.findall(r'(?:open|partial|clear): \["([^"]+)", "([^"]+)"\]', source)
    assert len(labels) == 3
    self_test = presets.preset_catalog()["self-test"].instruction
    for chinese, english in labels:
        assert chinese in self_test and english in self_test, (chinese, english)
    assert "只是建议" in self_test and "学习者自己标记" in self_test


def test_self_test_is_scoped_to_the_learning_path_the_model_sees():
    """Only the path from the root to the current question reaches the model, never the
    topic's other branches, so Self-test promises a quiz on that path, not on the topic."""
    self_test = presets.preset_catalog()["self-test"]
    assert "学习路径" in self_test.instruction and "主题" not in self_test.instruction
    assert "学习路径" in self_test.description_zh and "主题" not in self_test.description_zh
    assert "learning path" in self_test.description_en
