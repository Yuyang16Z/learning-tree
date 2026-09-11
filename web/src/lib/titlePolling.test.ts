import { afterEach, describe, expect, it, vi } from "vitest";
import type { TreeNode } from "../types";
import { mergePendingTitles, pollPendingTitles } from "./titlePolling";

function node(extra: Partial<TreeNode> = {}): TreeNode {
  return {
    id: 2, tree_id: 1, parent_id: 1, title: "能详细讲一下 Eval harness 吗？", title_state: "pending",
    kind: "branch", seed_text: "AI 领域的 harness 工程，核心是…", has_summary: false, ...extra,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

describe("title metadata merging", () => {
  it("keeps unchanged nodes and array references stable", () => {
    const current = [node()];
    expect(mergePendingTitles(current, [node()])).toBe(current);
    expect(mergePendingTitles(current, [])).toBe(current);
  });

  it("updates only pending titles without reverting newer answer state or restoring deleted nodes", () => {
    const pending = node({ status: "complete" });
    const manual = node({ id: 3, title: "My title", title_state: "manual" });
    const current = [pending, manual];
    const result = mergePendingTitles(current, [
      node({ title: "Eval harness 入门", title_state: "ai", status: "pending", seed_text: "stale source" }),
      node({ id: 3, title: "Stale generated title", title_state: "ai" }),
      node({ id: 4, title: "Deleted branch", title_state: "ai" }),
    ]);
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ ...pending, title: "Eval harness 入门", title_state: "ai" });
    expect(result[1]).toBe(manual);
    expect(current[0].title_state).toBe("pending");
  });

  it("rejects unrelated trees and missing title states", () => {
    const current = [node()];
    expect(mergePendingTitles(current, [node({ tree_id: 8, title_state: "ai" })])).toBe(current);
    expect(mergePendingTitles(current, [node({ title_state: undefined })])).toBe(current);
  });
});

describe("bounded title polling", () => {
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

  it("polls until a final title arrives then stops", async () => {
    vi.useFakeTimers();
    const final = [node({ title: "Eval harness 入门", title_state: "ai" })];
    const load = vi.fn().mockResolvedValueOnce([node()]).mockResolvedValueOnce(final);
    const commit = vi.fn();
    pollPendingTitles({ treeId: 1, load, revision: () => 1, isActive: () => true, commit });
    await vi.advanceTimersByTimeAsync(3000);
    expect(commit).toHaveBeenLastCalledWith(final, 1);
    await vi.advanceTimersByTimeAsync(30000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("discards a snapshot made stale by a newer map refresh and retries", async () => {
    vi.useFakeTimers();
    let revision = 1;
    const stale = deferred<TreeNode[]>();
    const final = [node({ title_state: "ai" })];
    const load = vi.fn().mockReturnValueOnce(stale.promise).mockResolvedValueOnce(final);
    const commit = vi.fn();
    pollPendingTitles({ treeId: 1, load, revision: () => revision, isActive: () => true, commit });
    await vi.advanceTimersByTimeAsync(1500);
    revision++;
    stale.resolve(final);
    await vi.advanceTimersByTimeAsync(0);
    expect(commit).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1500);
    expect(commit).toHaveBeenCalledExactlyOnceWith(final, 2);
  });

  it("cancels in-flight work on navigation even when the response ignores abort", async () => {
    vi.useFakeTimers();
    const pending = deferred<TreeNode[]>();
    const load = vi.fn().mockReturnValue(pending.promise);
    const commit = vi.fn();
    const cancel = pollPendingTitles({ treeId: 1, load, revision: () => 1, isActive: () => true, commit });
    await vi.advanceTimersByTimeAsync(1500);
    const signal: AbortSignal = load.mock.calls[0][1];
    cancel();
    expect(signal.aborted).toBe(true);
    pending.resolve([node({ title_state: "ai" })]);
    await vi.advanceTimersByTimeAsync(30000);
    expect(commit).not.toHaveBeenCalled();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("ignores a response after switching trees before effect cleanup", async () => {
    vi.useFakeTimers();
    let active = 1;
    const pending = deferred<TreeNode[]>();
    const commit = vi.fn();
    const load = vi.fn().mockReturnValue(pending.promise);
    pollPendingTitles({ treeId: 1, load, revision: () => 1, isActive: id => active === id, commit });
    await vi.advanceTimersByTimeAsync(1500);
    active = 2;
    pending.resolve([node({ title_state: "ai" })]);
    await vi.advanceTimersByTimeAsync(3000);
    expect(commit).not.toHaveBeenCalled();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("recovers from a temporary network failure without overlapping requests", async () => {
    vi.useFakeTimers();
    const final = [node({ title_state: "fallback" })];
    const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(final);
    const commit = vi.fn();
    pollPendingTitles({ treeId: 1, load, revision: () => 1, isActive: () => true, commit });
    await vi.advanceTimersByTimeAsync(3000);
    expect(commit).toHaveBeenCalledExactlyOnceWith(final, 1);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("aborts a stalled request at the deadline and does not commit a late response", async () => {
    vi.useFakeTimers();
    const pending = deferred<TreeNode[]>();
    const load = vi.fn().mockReturnValue(pending.promise);
    const commit = vi.fn();
    pollPendingTitles({ treeId: 1, load, revision: () => 1, isActive: () => true, commit });
    await vi.advanceTimersByTimeAsync(30000);
    expect(load.mock.calls[0][1].aborted).toBe(true);
    pending.resolve([node({ title_state: "ai" })]);
    await vi.advanceTimersByTimeAsync(30000);
    expect(commit).not.toHaveBeenCalled();
    expect(load).toHaveBeenCalledTimes(1);
  });
});
