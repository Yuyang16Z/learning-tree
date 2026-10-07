import type { ChangeNotice } from "../api";

export interface RefreshPlan { topics: boolean; models: boolean; mcp: boolean; map: boolean; thread: boolean }
interface OpenView { treeId: number | null; focusId: number | null; threadNodeIds: readonly number[] }

/** Reload only what the open view shows; a missed or unknown change reloads all of it. */
export function planRefresh(notice: ChangeNotice, view: OpenView): RefreshPlan {
  if (notice.reload)
    return { topics: true, models: true, mcp: true, map: view.treeId !== null, thread: view.focusId !== null };
  const changed = view.treeId === null ? undefined : notice.trees[String(view.treeId)];
  const shown = new Set(view.focusId === null ? view.threadNodeIds : [...view.threadNodeIds, view.focusId]);
  return {
    topics: notice.topics, models: notice.models, mcp: notice.mcp,
    map: changed !== undefined,
    thread: !!changed?.some(id => shown.has(id)),
  };
}

/** Keep the current value when a reload returns identical data, so React skips re-rendering it. */
export function keepIfSame<T>(next: T): (current: T) => T {
  const encoded = JSON.stringify(next);
  return current => current === next || JSON.stringify(current) === encoded ? current : next;
}

export interface PageLifecycle {
  visible(): boolean;
  /** Call `listener` when the page may have missed changes: shown, focused or back online. */
  onWake(listener: () => void): () => void;
}

const browserPage: PageLifecycle = {
  visible: () => document.visibilityState === "visible",
  onWake(listener) {
    const wake = () => { if (document.visibilityState === "visible") listener(); };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("focus", wake);
    window.addEventListener("online", wake);
    return () => {
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("focus", wake);
      window.removeEventListener("online", wake);
    };
  },
};

interface WatchOptions {
  load: (cursor: string, signal: AbortSignal) => Promise<ChangeNotice>;
  apply: (notice: ChangeNotice) => Promise<unknown>;
  intervalMs?: number;
  page?: PageLifecycle;
}

/** Poll for changes from other windows and devices while this page is visible. */
export function watchChanges({ load, apply, intervalMs = 2000, page = browserPage }: WatchOptions): () => void {
  const controller = new AbortController();
  let cursor = "";
  let timer: ReturnType<typeof setTimeout> | undefined;
  let running = false;
  let failures = 0;
  const schedule = (ms: number) => {
    clearTimeout(timer);
    if (!controller.signal.aborted) timer = setTimeout(poll, ms);
  };
  async function poll() {
    // A hidden page stops polling; waking it polls at once.
    if (controller.signal.aborted || running || !page.visible()) return;
    running = true;
    try {
      const notice = await load(cursor, controller.signal);
      await apply(notice);
      // Advance only after the view caught up, so a failed reload is retried.
      cursor = notice.cursor;
      failures = 0;
    } catch {
      failures += 1;
    } finally {
      running = false;
    }
    schedule(failures ? Math.min(30_000, intervalMs * 2 ** failures) : intervalMs);
  }
  const stopWaking = page.onWake(() => schedule(0));
  schedule(0);
  return () => { controller.abort(); clearTimeout(timer); stopWaking(); };
}
