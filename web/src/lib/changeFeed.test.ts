import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChangeNotice } from "../api";
import { keepIfSame, planRefresh, watchChanges, type PageLifecycle } from "./changeFeed";

function notice(extra: Partial<ChangeNotice> = {}): ChangeNotice {
  return { cursor: "a:1", reload: false, topics: false, models: false, mcp: false, memory: false, trees: {}, ...extra };
}

function fakePage() {
  const listeners = new Set<() => void>();
  const state = { visible: true };
  const page: PageLifecycle = {
    visible: () => state.visible,
    onWake: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
  return { page, state, listeners, wake: () => listeners.forEach(listener => listener()) };
}

describe("refresh planning", () => {
  const view = { treeId: 3, focusId: 12, threadNodeIds: [10, 11, 12] };

  it("reloads the conversation only when a node it shows changed", () => {
    expect(planRefresh(notice({ trees: { "3": [13] } }), view)).toEqual({ topics: false, models: false, mcp: false, map: true, thread: false });
    expect(planRefresh(notice({ trees: { "3": [11, 13] } }), view).thread).toBe(true);
    expect(planRefresh(notice({ trees: { "4": [12] } }), view)).toMatchObject({ map: false, thread: false });
  });

  it("passes settings and topic list changes through", () => {
    expect(planRefresh(notice({ topics: true, models: true, mcp: true }), view)).toEqual({ topics: true, models: true, mcp: true, map: false, thread: false });
  });

  it("reloads everything visible after missed changes", () => {
    expect(planRefresh(notice({ reload: true }), view)).toEqual({ topics: true, models: true, mcp: true, map: true, thread: true });
    expect(planRefresh(notice({ reload: true }), { treeId: null, focusId: null, threadNodeIds: [] })).toMatchObject({ map: false, thread: false });
  });
});

describe("identical reloads", () => {
  it("keep the current reference so unchanged views do not re-render", () => {
    const current = [{ id: 1, title: "A" }];
    expect(keepIfSame([{ id: 1, title: "A" }])(current)).toBe(current);
    const next = [{ id: 1, title: "B" }];
    expect(keepIfSame(next)(current)).toBe(next);
  });
});

describe("change watching", () => {
  afterEach(() => vi.useRealTimers());

  it("polls with the last applied cursor and stops cleanly", async () => {
    vi.useFakeTimers();
    const { page, listeners } = fakePage();
    const cursors: string[] = [];
    const load = vi.fn(async (cursor: string) => { cursors.push(cursor); return notice({ cursor: `a:${cursors.length}` }); });
    const stop = watchChanges({ load, apply: async () => {}, intervalMs: 1000, page });
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(2000);
    expect(cursors).toEqual(["", "a:1", "a:2"]);
    stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(load).toHaveBeenCalledTimes(3);
    expect(listeners.size).toBe(0);
  });

  it("keeps the cursor and backs off when the view cannot catch up", async () => {
    vi.useFakeTimers();
    const { page } = fakePage();
    const cursors: string[] = [];
    let offline = true;
    const stop = watchChanges({
      load: async cursor => { cursors.push(cursor); return notice({ cursor: "a:9" }); },
      apply: async () => { if (offline) throw new Error("offline"); },
      intervalMs: 1000,
      page,
    });
    await vi.advanceTimersByTimeAsync(1999);
    expect(cursors).toEqual([""]);
    offline = false;
    await vi.advanceTimersByTimeAsync(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(cursors).toEqual(["", "", "a:9"]);
    stop();
  });

  it("pauses while hidden and polls at once when the page returns", async () => {
    vi.useFakeTimers();
    const { page, state, wake } = fakePage();
    const load = vi.fn(async () => notice());
    const stop = watchChanges({ load, apply: async () => {}, intervalMs: 1000, page });
    await vi.advanceTimersByTimeAsync(0);
    state.visible = false;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(load).toHaveBeenCalledTimes(1);
    state.visible = true;
    wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(load).toHaveBeenCalledTimes(2);
    stop();
  });
});
