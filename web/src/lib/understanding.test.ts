import { describe, expect, it } from "vitest";
import type { ThreadNode, TreeNode } from "../types";
import { cardUnderstanding, isUnderstanding, restoreUnderstanding, toggleUnderstanding, UNDERSTANDING_LEVELS, UNDERSTANDING_TEXT, UNRESOLVED_CAP, unresolvedNodes, withUnderstanding } from "./understanding";

function node(id: number, extra: Partial<TreeNode> = {}): TreeNode {
  return { id, parent_id: id === 1 ? null : id - 1, tree_id: 1, title: `问题 ${id}`, seed_text: null, has_summary: false, ...extra };
}
function row(node_id: number, extra: Partial<ThreadNode> = {}): ThreadNode {
  return { node_id, parent_id: null, title: "", seed_text: null, question: "Q", images: null, answer: "A", reasoning: null, steps: null, answered_by: "Demo", ...extra };
}

describe("understanding marks", () => {
  it("offers three levels with bilingual labels and recognizes only those values", () => {
    expect(UNDERSTANDING_LEVELS).toEqual(["open", "partial", "clear"]);
    expect(UNDERSTANDING_TEXT.open).toEqual(["还没懂", "Not yet"]);
    expect(UNDERSTANDING_TEXT.partial).toEqual(["大致懂了", "Roughly"]);
    expect(UNDERSTANDING_TEXT.clear).toEqual(["能讲清楚", "Can explain"]);
    for (const value of UNDERSTANDING_LEVELS) expect(isUnderstanding(value)).toBe(true);
    for (const value of [null, undefined, "", "Open", "unknown", 1]) expect(isUnderstanding(value)).toBe(false);
  });

  it("clears the active mark when it is chosen again", () => {
    expect(toggleUnderstanding(null, "open")).toBe("open");
    expect(toggleUnderstanding(undefined, "clear")).toBe("clear");
    expect(toggleUnderstanding("open", "partial")).toBe("partial");
    expect(toggleUnderstanding("partial", "partial")).toBeNull();
  });

  it("updates only the targeted rows and keeps the array when nothing changes", () => {
    const rows = [row(1), row(2), row(2, { question: "second turn" })];
    const marked = withUnderstanding(rows, item => item.node_id === 2, "open");
    expect(marked.map(item => item.understanding ?? null)).toEqual([null, "open", "open"]);
    expect(marked[0]).toBe(rows[0]);
    expect(rows[1].understanding).toBeUndefined();
    expect(withUnderstanding(marked, item => item.node_id === 2, "open")).toBe(marked);
    expect(withUnderstanding(marked, item => item.node_id === 3, "clear")).toBe(marked);
    expect(withUnderstanding(marked, item => item.node_id === 2, null).map(item => item.understanding)).toEqual([undefined, null, null]);
  });

  it("rolls back a failed save without overwriting a newer mark", () => {
    const nodes = [node(1, { understanding: "partial" }), node(2, { understanding: "clear" })];
    const attempted = withUnderstanding(nodes, item => item.id === 1, "open");
    expect(restoreUnderstanding(attempted, item => item.id === 1, "open", "partial")[0].understanding).toBe("partial");
    // Another window changed the mark before this save failed: keep that newer value.
    const newer = withUnderstanding(attempted, item => item.id === 1, "clear");
    expect(restoreUnderstanding(newer, item => item.id === 1, "open", "partial")).toBe(newer);
    // Clearing failed: the earlier mark returns.
    const cleared = withUnderstanding(nodes, item => item.id === 2, null);
    expect(restoreUnderstanding(cleared, item => item.id === 2, null, "clear")[1].understanding).toBe("clear");
  });

  it("colours a card by its last node unless a pending or failed answer shows its glyph", () => {
    expect(cardUnderstanding([node(1, { understanding: "open" }), node(2, { understanding: "clear" })])).toEqual({ value: "clear", className: "u-clear" });
    expect(cardUnderstanding([node(1, { understanding: "open" }), node(2)])).toEqual({ value: null, className: "" });
    expect(cardUnderstanding([node(1, { understanding: "partial", status: "complete" })])).toEqual({ value: "partial", className: "u-partial" });
    for (const status of ["pending", "error", "interrupted"] as const) {
      expect(cardUnderstanding([node(1, { status }), node(2, { understanding: "open" })])).toEqual({ value: "open", className: "" });
    }
    expect(cardUnderstanding([node(1, { understanding: "someday" as never })])).toEqual({ value: null, className: "" });
    expect(cardUnderstanding([])).toEqual({ value: null, className: "" });
  });
});

describe("unresolved questions", () => {
  it("lists only nodes marked Not yet, newest first", () => {
    const nodes = [node(1, { understanding: "open" }), node(2, { understanding: "partial" }), node(3), node(4, { understanding: "open" }), node(5, { understanding: "clear" }), node(6, { understanding: null })];
    const { items, total } = unresolvedNodes(nodes);
    expect(items.map(item => item.id)).toEqual([4, 1]);
    expect(total).toBe(2);
  });

  it("follows the map's current nodes, including duplicates and an empty topic", () => {
    const first = [node(1, { understanding: "open" }), node(2, { understanding: "open" })];
    expect(unresolvedNodes(first).total).toBe(2);
    // Marked elsewhere, cleared, or deleted: the next node list is the only truth.
    const next = [node(1, { understanding: "clear" }), node(3, { understanding: "open" }), node(3, { understanding: "open" })];
    expect(unresolvedNodes(next)).toEqual({ items: [next[1]], total: 1 });
    expect(unresolvedNodes([])).toEqual({ items: [], total: 0 });
  });

  it("caps the list like search results while counting everything", () => {
    const many = Array.from({ length: 45 }, (_, index) => node(index + 1, { understanding: "open" }));
    const { items, total } = unresolvedNodes(many);
    expect(UNRESOLVED_CAP).toBe(30);
    expect(items).toHaveLength(30);
    expect(items[0].id).toBe(45);
    expect(items[29].id).toBe(16);
    expect(total).toBe(45);
    expect(unresolvedNodes(many, 2).items.map(item => item.id)).toEqual([45, 44]);
    expect(unresolvedNodes(many, -1).items).toEqual([]);
  });
});
