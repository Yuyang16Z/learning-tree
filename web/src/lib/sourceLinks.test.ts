import { describe, expect, it, vi } from "vitest";
import type { AnswerSource } from "../types";
import { locateSource, parseSources, sourceLink } from "./sourceLinks";

function source(memory_id: number, tree_id: number | null, source_node_id: number | null, scope: AnswerSource["scope"] = "path"): AnswerSource {
  return { memory_id, tree_id, source_node_id, tree_title: "主题", scope };
}

describe("Referenced chips", () => {
  const view = { activeTreeId: 1, topicIds: new Set([1, 2]), nodeIds: new Set([10, 11]), canOpenTopics: true };

  it("open nodes of this topic and other topics that still exist", () => {
    expect(sourceLink(source(1, 1, 11), view)).toEqual({ kind: "open", treeId: 1, nodeId: 11 });
    expect(sourceLink(source(2, 2, 20, "other"), view)).toEqual({ kind: "open", treeId: 2, nodeId: 20 });
  });

  it("disable sources whose topic or node was deleted", () => {
    expect(sourceLink(source(3, 3, 30, "other"), view)).toEqual({ kind: "deleted" });
    expect(sourceLink(source(4, 1, 12, "tree"), view)).toEqual({ kind: "deleted" });
  });

  it("cannot open a source without a node, or another topic where no topic can be opened", () => {
    expect(sourceLink(source(5, 1, null), view)).toEqual({ kind: "none" });
    expect(sourceLink(source(6, null, 10), view)).toEqual({ kind: "none" });
    expect(sourceLink(source(7, 2, 20, "other"), { ...view, canOpenTopics: false })).toEqual({ kind: "none" });
  });

  it("does not call a source deleted before the topic list or the map has loaded", () => {
    expect(sourceLink(source(8, 3, 30, "other"), { ...view, topicIds: new Set() })).toEqual({ kind: "open", treeId: 3, nodeId: 30 });
    expect(sourceLink(source(9, 1, 12), { ...view, nodeIds: new Set() })).toEqual({ kind: "open", treeId: 1, nodeId: 12 });
  });
});

describe("checking a source before leaving the current view", () => {
  it("finds a node that still exists in its topic", async () => {
    const load = vi.fn(async () => [{ id: 10 }, { id: 11 }]);
    expect(await locateSource({ treeId: 1, nodeId: 11 }, load)).toBe("found");
    expect(await locateSource({ treeId: 1, nodeId: null }, load)).toBe("found");
    expect(load).toHaveBeenCalledWith(1);
  });

  it("reports a deleted topic or node instead of navigating", async () => {
    const missingTopic = vi.fn(async () => { throw Object.assign(new Error("Learning tree not found"), { status: 404 }); });
    expect(await locateSource({ treeId: 3, nodeId: 30 }, missingTopic)).toBe("deleted");
    expect(await locateSource({ treeId: 1, nodeId: 12 }, async () => [{ id: 10 }])).toBe("deleted");
  });

  it("passes other failures on, so they are reported as errors", async () => {
    const offline = async () => { throw new TypeError("Failed to fetch"); };
    await expect(locateSource({ treeId: 1, nodeId: 10 }, offline)).rejects.toThrow("Failed to fetch");
  });
});

describe("reading stored or streamed sources", () => {
  const valid = { memory_id: 7, source_node_id: 3, tree_id: 1, tree_title: "主题", scope: "path" };

  it("keeps well-formed entries only", () => {
    const parsed = parseSources([
      valid, null, "7", [], { ...valid, memory_id: "8" }, { ...valid, memory_id: 9, scope: "everywhere" },
      { ...valid, memory_id: 10, tree_id: "1" }, { ...valid, memory_id: 11, source_node_id: 2.5 }, { ...valid, memory_id: 12, tree_title: null },
      { memory_id: 13, source_node_id: null, tree_id: null, tree_title: "", scope: "other", extra: "ignored" }, { ...valid },
    ]);
    expect(parsed).toEqual([valid, { memory_id: 13, source_node_id: null, tree_id: null, tree_title: "", scope: "other" }]);
  });

  it("gives nothing when no entry survives or the value is not a list", () => {
    for (const value of [[null], [{ memory_id: "x" }], [], "sources", null, undefined, { 0: valid }]) expect(parseSources(value)).toBeNull();
  });
});
