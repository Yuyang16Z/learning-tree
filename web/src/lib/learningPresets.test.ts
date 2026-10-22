import { describe, expect, it } from "vitest";
import type { LearningPreset, LearningPresetList, Tree } from "../types";
import { nodeQuestionPreset, presetChoice, presetDescription, presetTitle, questionTag, requestedPreset, restoreTopicPreset, STYLE_TEXT, styleLabel, styleOptions, withTopicPreset } from "./learningPresets";

function preset(id: string, extra: Partial<LearningPreset> = {}): LearningPreset {
  return { id, source: id.startsWith("custom:") ? "custom" : "builtin", title_zh: `中文 ${id}`, title_en: `English ${id}`, description_zh: `说明 ${id}`, description_en: `About ${id}`, instruction: "合成说明", ...extra };
}
const list: LearningPresetList = { presets: [preset("socratic"), preset("feynman"), preset("custom:mine")], errors: [] };
const tree = (id: number, learning_preset?: string | null): Tree => ({ id, title: `Topic ${id}`, root_node_id: id * 10, archived: false, learning_preset });

describe("learning style choices", () => {
  it("tells the default way, a usable style, a vanished one and an unknown list apart", () => {
    for (const id of [null, undefined, ""]) expect(presetChoice(id, list)).toEqual({ kind: "default" });
    expect(presetChoice("feynman", list)).toEqual({ kind: "preset", preset: list.presets[1] });
    expect(presetChoice("custom:deleted", list)).toEqual({ kind: "unavailable", id: "custom:deleted" });
    expect(presetChoice("feynman", null)).toEqual({ kind: "loading", id: "feynman" });
  });

  it("lists the default way first, then the server's order, then a current choice that no longer exists", () => {
    expect(styleOptions(list, null).map(option => option.id)).toEqual([null, "socratic", "feynman", "custom:mine"]);
    expect(styleOptions(list, "feynman").map(option => option.id)).toEqual([null, "socratic", "feynman", "custom:mine"]);
    const vanished = styleOptions(list, "custom:deleted");
    expect(vanished.map(option => option.id)).toEqual([null, "socratic", "feynman", "custom:mine", "custom:deleted"]);
    expect(vanished[vanished.length - 1]).toEqual({ id: "custom:deleted", unavailable: true });
    // Before the list loads nothing is called unavailable.
    expect(styleOptions(null, "custom:deleted")).toEqual([{ id: null }]);
  });

  it("names the button by the topic's style in the current language", () => {
    expect(styleLabel(null, list, "zh-CN")).toBe("默认");
    expect(styleLabel(null, list, "en")).toBe("Default");
    expect(styleLabel("socratic", list, "zh-CN")).toBe("中文 socratic");
    expect(styleLabel("socratic", list, "en")).toBe("English socratic");
    expect(styleLabel("custom:deleted", list, "zh-CN")).toBe("已不可用");
    expect(styleLabel("custom:deleted", list, "en")).toBe("Unavailable");
    expect(styleLabel("socratic", null, "en")).toBe("…");
    expect(STYLE_TEXT.defaultOption).toEqual(["默认（直接讲解）", "Default (direct explanation)"]);
  });

  it("falls back to the other language when a custom file describes itself in one", () => {
    const english = preset("custom:english", { description_zh: "" });
    expect(presetDescription(english, "zh-CN")).toBe("About custom:english");
    expect(presetDescription(english, "en")).toBe("About custom:english");
    expect(presetDescription(preset("compare"), "zh-CN")).toBe("说明 compare");
    expect(presetTitle(preset("compare"), "en")).toBe("English compare");
  });

  it("sends the topic's style while it exists and the default way once it is gone", () => {
    expect(requestedPreset("feynman", list)).toBe("feynman");
    expect(requestedPreset("custom:mine", list)).toBe("custom:mine");
    expect(requestedPreset("custom:deleted", list)).toBeNull();
    for (const value of [null, undefined, ""]) expect(requestedPreset(value, list)).toBeNull();
    // The server checks an id the page could not look up yet.
    expect(requestedPreset("feynman", null)).toBe("feynman");
  });

  it("reads a node's style from its last row, since earlier rows of a legacy node used none", () => {
    const rows = [
      { node_id: 1, learning_preset: "feynman" },
      { node_id: 3, learning_preset: null },
      { node_id: 3, learning_preset: "socratic" },
    ];
    expect(nodeQuestionPreset(rows, 3)).toBe("socratic");
    expect(nodeQuestionPreset(rows, 1)).toBe("feynman");
    expect(nodeQuestionPreset([{ node_id: 3 }], 3)).toBeNull();
    expect(nodeQuestionPreset(rows, 9)).toBeNull();
  });

  it("tags questions asked with a style by its title, and vanished styles as unavailable", () => {
    expect(questionTag(null, list, "en")).toBeNull();
    expect(questionTag(undefined, list, "zh-CN")).toBeNull();
    expect(questionTag("socratic", list, "zh-CN")).toEqual({ text: "中文 socratic", unavailable: false });
    expect(questionTag("socratic", list, "en")).toEqual({ text: "English socratic", unavailable: false });
    expect(questionTag("custom:deleted", list, "zh-CN")).toEqual({ text: "已不可用", unavailable: true });
    expect(questionTag("custom:deleted", list, "en")).toEqual({ text: "Unavailable", unavailable: true });
    expect(questionTag("socratic", null, "en")).toBeNull();
  });
});

describe("optimistic topic style changes", () => {
  it("changes only the chosen topic and keeps the array when nothing changes", () => {
    const trees = [tree(1, "socratic"), tree(2)];
    const changed = withTopicPreset(trees, 2, "feynman");
    expect(changed.map(item => item.learning_preset)).toEqual(["socratic", "feynman"]);
    expect(changed[0]).toBe(trees[0]);
    expect(withTopicPreset(trees, 1, "socratic")).toBe(trees);
    expect(withTopicPreset(trees, 2, null)).toBe(trees);
    expect(withTopicPreset(trees, 9, "feynman")).toBe(trees);
  });

  it("rolls back a failed save unless another window changed the topic meanwhile", () => {
    const attempted = [tree(1, "feynman")];
    expect(restoreTopicPreset(attempted, 1, "feynman", "socratic")[0].learning_preset).toBe("socratic");
    expect(restoreTopicPreset([tree(1, null)], 1, null, "socratic")[0].learning_preset).toBe("socratic");
    const newer = [tree(1, "compare")];
    expect(restoreTopicPreset(newer, 1, "feynman", "socratic")).toBe(newer);
    expect(restoreTopicPreset(attempted, 2, "feynman", null)).toBe(attempted);
  });
});
