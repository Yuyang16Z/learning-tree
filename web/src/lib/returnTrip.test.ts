import { describe, expect, it } from "vitest";
import type { ReturnSummary } from "../types";
import { followReturnTrip, hasReturnContent, returnTitle, type ReturnTrip } from "./returnTrip";

const trip: ReturnTrip = { nonce: 1, treeId: 7, fromNodeId: 12, branchNodeId: 10, sourceNodeId: 3, arrived: false };
function summary(extra: Partial<ReturnSummary> = {}): ReturnSummary {
  return {
    branch_node_id: 10, branch_title: "基例是什么", understanding: null, note: null, excerpts: [],
    source: { node_id: 3, message_id: 4, start: 0, end: 5, text: "更小的同类问题" }, ...extra,
  };
}

describe("return trips", () => {
  it("waits on the branch, arrives at the source and then stays there", () => {
    expect(followReturnTrip(trip, { treeId: 7, nodeId: 12 })).toBe(trip);
    const arrived = followReturnTrip(trip, { treeId: 7, nodeId: 3 });
    expect(arrived).toEqual({ ...trip, arrived: true });
    expect(followReturnTrip(arrived, { treeId: 7, nodeId: 3 })).toBe(arrived);
  });

  it("ends when the learner navigates anywhere else, including back into the branch", () => {
    const arrived = followReturnTrip(trip, { treeId: 7, nodeId: 3 });
    expect(followReturnTrip(arrived, { treeId: 7, nodeId: 12 })).toBeNull();
    expect(followReturnTrip(arrived, { treeId: 7, nodeId: 99 })).toBeNull();
    expect(followReturnTrip(arrived, { treeId: 7, nodeId: null })).toBeNull();
    expect(followReturnTrip(trip, { treeId: 7, nodeId: 99 })).toBeNull();
    expect(followReturnTrip(trip, { treeId: 8, nodeId: 3 })).toBeNull();
    expect(followReturnTrip(null, { treeId: 7, nodeId: 3 })).toBeNull();
  });

  it("shows a card only when the branch left excerpts, a note or a mark", () => {
    expect(hasReturnContent(summary())).toBe(false);
    expect(hasReturnContent(summary({ note: "   " }))).toBe(false);
    expect(hasReturnContent(null)).toBe(false);
    expect(hasReturnContent(summary({ note: "我的理解" }))).toBe(true);
    expect(hasReturnContent(summary({ understanding: "open" }))).toBe(true);
    expect(hasReturnContent(summary({ excerpts: [{ category: "condition", text: "只有……才……", node_id: 10, message_id: 11 }] }))).toBe(true);
  });

  it("names the branch by its title, else its quote, else the fallback", () => {
    expect(returnTitle(summary(), "New branch")).toBe("基例是什么");
    expect(returnTitle(summary({ branch_title: "  " }), "New branch")).toBe("更小的同类问题");
    const long = summary({ branch_title: "", source: { ...summary().source, text: "a\nlong   quoted passage that keeps going past the limit" } });
    expect(returnTitle(long, "New branch", 10)).toBe("a long quo…");
    expect(returnTitle(summary({ branch_title: "", source: { ...summary().source, text: null } }), "New branch")).toBe("New branch");
  });
});
