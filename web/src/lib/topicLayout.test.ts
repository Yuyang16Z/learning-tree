import { describe, expect, it } from "vitest";
import type { TreeNode } from "../types";
import { groupAncestorIds, groupTopics, layoutTopics, TOPIC_CARD } from "./topicLayout";

function node(id: number, parent_id: number | null, extra: Partial<TreeNode> & { kind?: string } = {}): TreeNode {
  return { id, parent_id, tree_id: 1, title: `问题 ${id}`, seed_text: null, has_summary: false, ...extra };
}
function branchingTree() {
  return groupTopics([node(1, null), node(2, 1), node(3, 2), node(4, 2, { seed_text: "不懂的概念" }), node(5, 4)]);
}

describe("topic grouping", () => {
  it("groups a straight conversation regardless of input order while retaining every Q/A", () => {
    const chain = groupTopics([node(3, 2), node(1, null), node(2, 1)]);
    expect(chain.groups.map((group) => group.nodes.map((item) => item.id))).toEqual([[1, 2, 3]]);
    expect(chain.nodeToGroup.get(2)).toBe(1);
  });

  it("splits a chain at its actual branch source rather than moving the branch to the final round", () => {
    const tree = branchingTree();
    expect(tree.groups.map((group) => group.nodes.map((item) => item.id))).toEqual([[1, 2], [3], [4, 5]]);
    expect(tree.byId.get(4)?.sourceNodeId).toBe(2);
    expect(tree.byId.get(4)?.parentId).toBe(1);
    expect(groupAncestorIds(tree, 5).has(1)).toBe(true);
  });

  it("keeps revisions and quoted follow-ups as separate topics even if they are the only child", () => {
    const revisions = groupTopics([node(1, null), node(2, 1, { kind: "revision" }), node(3, 2)]);
    expect(revisions.groups.map((group) => group.nodes.map((item) => item.id))).toEqual([[1], [2, 3]]);
    const quoted = groupTopics([node(1, null), node(2, 1, { seed_text: "解释这个词" })]);
    expect(quoted.groups).toHaveLength(2);
  });

  it("uses localized placeholders for empty titles without exposing the quoted answer as a title", () => {
    const nodes = [node(1, null, { title: "" }), node(2, 1, {
      kind: "branch", title: "", title_state: "empty", seed_text: "AI 领域的 harness 工程，核心是：模型…",
    })];
    const chinese = groupTopics(nodes);
    expect(chinese.groups.map(group => group.title)).toEqual(["新问题", "新分支"]);
    const english = groupTopics(nodes, "New question", "New branch");
    expect(english.groups.map(group => group.title)).toEqual(["New question", "New branch"]);
  });

  it("uses the branch question summary while retaining the original source anchor", () => {
    const branch = node(2, 1, {
      kind: "branch", title: "Eval harness 评估框架入门", title_state: "ai",
      seed_text: "AI 领域的 harness 工程，核心是：模型…", source_message_id: 9,
    });
    const topics = groupTopics([node(1, null), branch]);
    expect(topics.byId.get(2)?.title).toBe("Eval harness 评估框架入门");
    expect(topics.byId.get(2)?.nodes[0]).toBe(branch);
  });

  it("keeps cycles, orphan nodes, and self parents navigable without infinite traversal", () => {
    const malformed = groupTopics([node(1, 2), node(2, 1), node(3, 999), node(4, 4)]);
    expect(malformed.nodeToGroup.size).toBe(4);
    expect(layoutTopics(malformed).items).toHaveLength(malformed.groups.length);
  });

  it("handles a long linear history without overflowing the call stack", () => {
    const chain = groupTopics(Array.from({ length: 15000 }, (_, index) => node(index + 1, index ? index : null)));
    expect(chain.groups).toHaveLength(1);
    expect(chain.groups[0].nodes).toHaveLength(15000);
  });
});

describe("topic layout", () => {
  it("folds descendants while leaving their parent visible", () => {
    const folded = layoutTopics(branchingTree(), new Set([1]));
    expect(folded.items.map((item) => item.group.id)).toEqual([1]);
  });

  it("reserves space for expanded rounds, avoids overlaps, and connects real parent groups", () => {
    const layout = layoutTopics(branchingTree(), new Set(), new Set([4]));
    expect(layout.byId.get(4)?.height).toBe(TOPIC_CARD.height + 2 * TOPIC_CARD.roundHeight + 10);
    for (let index = 1; index < layout.items.length; index++) {
      const before = layout.items[index - 1];
      const after = layout.items[index];
      expect(after.y).toBeGreaterThanOrEqual(before.y + before.height + TOPIC_CARD.gap);
    }
    for (const { from, to } of layout.edges) expect(to.group.parentId).toBe(from.group.id);
    expect(layout.width).toBeLessThanOrEqual(320);
  });

  it("keeps existing card positions stable when a later sibling is appended", () => {
    const tree = branchingTree();
    const before = layoutTopics(tree).items.map(({ group, x, y }) => ({ id: group.id, x, y }));
    const appended = layoutTopics(groupTopics([...tree.groups.flatMap((group) => group.nodes), node(6, 2, { seed_text: "另一条分支" })]));
    expect(appended.items.slice(0, before.length).map(({ group, x, y }) => ({ id: group.id, x, y }))).toEqual(before);
  });
});
