import type { NavigationAnchor, SearchHit, TreeNode } from "../types";

export type SearchAnchor = Omit<NavigationAnchor, "nodeId" | "nonce">;

/** The map shows at most this many results across both groups. */
export const MAP_SEARCH_CAP = 30;

/** Instant client-side title/quote matches; the first result group. */
export function localMatches(nodes: readonly TreeNode[], query: string): TreeNode[] {
  const needle = query.trim().toLocaleLowerCase();
  return needle ? nodes.filter(node => `${node.title}\n${node.seed_text ?? ""}`.toLocaleLowerCase().includes(needle)) : [];
}

/**
 * Server hits form the second group: title hits already shown locally are dropped,
 * hits for nodes this map no longer knows are dropped, and the cap covers both groups.
 */
export function mergeSearchResults(local: readonly TreeNode[], hits: readonly SearchHit[], nodes: readonly TreeNode[], cap = MAP_SEARCH_CAP): { local: TreeNode[]; remote: SearchHit[] } {
  const known = new Set(nodes.map(node => node.id));
  const shown = new Set(local.map(node => node.id));
  const first = local.slice(0, cap);
  const seen = new Set<string>();
  const remote: SearchHit[] = [];
  for (const hit of hits) {
    if (remote.length + first.length >= cap) break;
    if (!known.has(hit.node_id)) continue;
    if (hit.kind === "title" && shown.has(hit.node_id)) continue;
    const key = `${hit.kind}:${hit.node_id}:${hit.message_id ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    remote.push(hit);
  }
  return { local: first, remote };
}

/** Message hits navigate to the passage; the anchor text must be verbatim, so ellipses are dropped. */
export function searchAnchor(hit: SearchHit): SearchAnchor | undefined {
  if (hit.kind !== "message" || hit.message_id == null) return undefined;
  const text = hit.snippet.replace(/^…+/, "").replace(/…+$/, "").trim();
  return { messageId: hit.message_id, ...(text ? { text } : {}) };
}

interface SearcherOptions {
  search: (treeId: number, query: string, signal: AbortSignal) => Promise<SearchHit[]>;
  delayMs?: number;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

export interface MapSearcher {
  /** Debounce `query` for `treeId`; `deliver` only runs while this request is still current. */
  run(treeId: number, query: string, deliver: (hits: SearchHit[]) => void): void;
  /** Drop the pending request and ignore any in-flight response (tree change, close). */
  cancel(): void;
}

/** Debounced, abortable topic search; stale responses never reach the map. */
export function createMapSearcher({ search, delayMs = 250, setTimer = (callback, ms) => setTimeout(callback, ms), clearTimer = handle => clearTimeout(handle as ReturnType<typeof setTimeout>) }: SearcherOptions): MapSearcher {
  let timer: unknown = null;
  let controller: AbortController | null = null;
  let ticket = 0;
  const cancel = () => {
    if (timer !== null) { clearTimer(timer); timer = null; }
    controller?.abort();
    controller = null;
    ticket += 1;
  };
  return {
    run(treeId, query, deliver) {
      cancel();
      const needle = query.trim();
      if (!needle) return;
      const current = ticket;
      timer = setTimer(() => {
        timer = null;
        const ctrl = new AbortController();
        controller = ctrl;
        search(treeId, needle, ctrl.signal).then(hits => {
          if (current === ticket && !ctrl.signal.aborted) deliver(hits);
        }, () => { /* Aborted or failed searches leave the instant group in place. */ });
      }, delayMs);
    },
    cancel,
  };
}
