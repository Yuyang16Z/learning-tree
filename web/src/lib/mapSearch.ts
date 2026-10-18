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

// Snippets are windows of stored Markdown. These characters may become markup when the answer
// is rendered, and line breaks may collapse, so an anchor never crosses one of them.
const MARKUP = /[*_`#>[\]()|~<&!\\\r\n]/u;
const MARKUP_RUNS = /[*_`#>[\]()|~<&!\\\r\n]+/gu;
// A list item's marker at the start of its line is not part of the text it renders.
const LINE_MARKER = /^\s*(?:[-+]|\d{1,9}[.)])\s+/u;

const withoutEllipses = (snippet: string) => snippet.replace(/^…+/, "").replace(/…+$/, "");

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The verbatim stretch around [start, end) that crosses no markup character or line break. */
function plainRun(text: string, start: number, end: number): string {
  let left = start;
  let right = end;
  while (left > 0 && !MARKUP.test(text[left - 1])) left -= 1;
  while (right < text.length && !MARKUP.test(text[right])) right += 1;
  const marker = left === 0 || text[left - 1] === "\n" || text[left - 1] === "\r" ? LINE_MARKER.exec(text.slice(left, start)) : null;
  return text.slice(left + (marker?.[0].length ?? 0), right).trim();
}

/** The longest stretch of a snippet without markup or line breaks, without its list marker. */
function longestPlainRun(text: string): string {
  return text.split(MARKUP_RUNS).map(part => part.replace(LINE_MARKER, "").trim())
    .reduce((longest, part) => part.length > longest.length ? part : longest, "");
}

/**
 * Message hits navigate to the passage. The anchor must be text the rendered answer contains, so it
 * is the match for `query` (case-insensitive) widened only across plain characters; when the match is
 * not in the snippet verbatim (accents folded by the index), the snippet's longest plain stretch.
 */
export function searchAnchor(hit: SearchHit, query = ""): SearchAnchor | undefined {
  if (hit.kind !== "message" || hit.message_id == null) return undefined;
  const snippet = withoutEllipses(hit.snippet);
  const needle = query.trim();
  const match = needle ? new RegExp(escapeRegExp(needle), "iu").exec(snippet) : null;
  const text = match ? plainRun(snippet, match.index, match.index + match[0].length) : longestPlainRun(snippet);
  return { messageId: hit.message_id, ...(text ? { text } : {}) };
}

/** A snippet of stored Markdown as display text: markers dropped, link text kept, lines joined. */
export function plainSnippet(snippet: string): string {
  return snippet
    .replace(/!?\[([^\]\n]*)\]\([^)\n]*\)/gu, "$1")
    .replace(/^[ \t]*(?:#{1,6}|>+|[-+*]|\d{1,9}[.)])[ \t]+/gmu, "")
    .replace(/\*\*|__|~~|`+/gu, "")
    .replace(/(?<![\p{L}\p{N}*_])[*_](?=[^\s*_])|(?<=[^\s*_])[*_](?![\p{L}\p{N}*_])/gu, "")
    .replace(/\s+/gu, " ")
    .trim();
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
