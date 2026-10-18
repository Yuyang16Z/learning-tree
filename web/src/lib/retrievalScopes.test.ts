import { describe, expect, it, vi } from "vitest";
import type { RetrievalSettings } from "../types";
import { createScopeStore } from "./retrievalScopes";

const initial: RetrievalSettings = { include_siblings: false, include_other_trees: false, revision: "r1" };
const elsewhere: RetrievalSettings = { include_siblings: true, include_other_trees: false, revision: "r2" };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
const failure = (status: number, message: string) => Object.assign(new Error(message), { status });

describe("retrieval scope switches in Settings", () => {
  it("reports a save as running and keeps its failure for when the panel is shown again", async () => {
    const saved = deferred<RetrievalSettings>();
    const save = vi.fn(() => saved.promise);
    const store = createScopeStore({ load: async () => initial, save });
    await store.load();
    const pending = store.update({ include_other_trees: true });
    expect(save).toHaveBeenCalledWith({ ...initial, include_other_trees: true });
    expect(store.get()).toMatchObject({ saving: true, error: null });
    saved.reject(failure(500, "请求失败（500），请重试。"));
    await pending;
    expect(store.get()).toEqual({ settings: initial, saving: false, conflict: null, error: "请求失败（500），请重试。" });
  });

  it("says a conflicting save is reloading until the latest settings arrive, then that it was not saved", async () => {
    const latest = deferred<RetrievalSettings>();
    let loads = 0;
    const store = createScopeStore({
      load: () => (++loads === 1 ? Promise.resolve(initial) : latest.promise),
      save: async () => { throw failure(409, "检索范围设置已在其他窗口更新，请重新载入。"); },
    });
    await store.load();
    const pending = store.update({ include_other_trees: true });
    await vi.waitFor(() => expect(store.get().conflict).toBe("reloading"));
    expect(store.get()).toMatchObject({ saving: true, settings: initial });
    latest.resolve(elsewhere);
    await pending;
    expect(store.get()).toEqual({ settings: elsewhere, saving: false, conflict: "reloaded", error: null });
  });

  it("reports a reload that fails after a conflict", async () => {
    let loads = 0;
    const store = createScopeStore({
      load: async () => { if (++loads === 1) return initial; throw new TypeError("Failed to fetch"); },
      save: async () => { throw failure(409, "conflict"); },
    });
    await store.load();
    await store.update({ include_siblings: true });
    expect(store.get()).toEqual({ settings: initial, saving: false, conflict: null, error: "Failed to fetch" });
  });

  it("ignores changes before the switches load and while a save is running", async () => {
    const saved = deferred<RetrievalSettings>();
    const save = vi.fn(() => saved.promise);
    const store = createScopeStore({ load: async () => initial, save });
    await store.update({ include_siblings: true });
    expect(save).not.toHaveBeenCalled();
    await store.load();
    const first = store.update({ include_siblings: true });
    await store.update({ include_other_trees: true });
    expect(save).toHaveBeenCalledTimes(1);
    saved.resolve({ ...initial, include_siblings: true, revision: "r3" });
    await first;
    expect(store.get()).toEqual({ settings: { ...initial, include_siblings: true, revision: "r3" }, saving: false, conflict: null, error: null });
  });

  it("notifies the panel of each change and ignores late results once Settings closes", async () => {
    const listener = vi.fn();
    const open = createScopeStore({ load: async () => initial, save: async settings => ({ ...settings, revision: "r3" }) });
    open.subscribe(listener);
    await open.load();
    expect(listener).toHaveBeenCalled();
    const loaded = listener.mock.calls.length;
    await open.update({ include_siblings: true });
    expect(listener.mock.calls.length).toBeGreaterThan(loaded);

    const read = deferred<RetrievalSettings>();
    const closing = createScopeStore({ load: () => read.promise, save: async settings => settings });
    const late = vi.fn();
    closing.subscribe(late);
    const loading = closing.load();
    const before = late.mock.calls.length;
    closing.close();
    read.resolve(initial);
    await loading;
    expect(closing.get().settings).toBeNull();
    expect(late).toHaveBeenCalledTimes(before);
  });
});
