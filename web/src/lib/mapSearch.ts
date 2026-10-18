import type { NavigationAnchor, SearchHit, TreeNode } from "../types";

export type SearchAnchor = Omit<NavigationAnchor, "nodeId" | "nonce">;

/** The map shows at most this many results across both groups. */
export const MAP_SEARCH_CAP = 30;
/** The search API rejects longer queries. */
export const MAP_SEARCH_MAX_QUERY = 300;

/** Server hits as delivered for one topic and trimmed query; a failed search delivers none. */
export interface RemoteHits { treeId: number; query: string; hits: SearchHit[]; failed?: boolean }

const answers = (remote: RemoteHits | null, treeId: number | null, query: string) =>
  !!remote && remote.treeId === treeId && remote.query === query.trim();

/** Server hits count only while they answer the topic and query now shown, so none outlive a keystroke. */
export function currentHits(remote: RemoteHits | null, treeId: number | null, query: string): SearchHit[] {
  return answers(remote, treeId, query) ? remote!.hits : [];
}

/** The server has not yet answered (or failed) the search for the topic and query now shown. */
export function searchPending(remote: RemoteHits | null, treeId: number | null, query: string): boolean {
  return !!query.trim() && treeId !== null && !answers(remote, treeId, query);
}

/** Instant client-side title/quote matches; the first result group. */
export function localMatches(nodes: readonly TreeNode[], query: string): TreeNode[] {
  const needle = query.trim().toLocaleLowerCase();
  return needle ? nodes.filter(node => `${node.title}\n${node.seed_text ?? ""}`.toLocaleLowerCase().includes(needle)) : [];
}

/**
 * Server hits form the second group: title hits already shown locally are dropped,
 * hits for nodes this map no longer knows are dropped, and the cap covers both groups.
 * `more` says the lists leave matches out: a group was cut by the cap, or the server sent
 * as many hits as the map asks for (MAP_SEARCH_CAP), so it may hold more.
 */
export function mergeSearchResults(local: readonly TreeNode[], hits: readonly SearchHit[], nodes: readonly TreeNode[], cap = MAP_SEARCH_CAP): { local: TreeNode[]; remote: SearchHit[]; more: boolean } {
  const known = new Set(nodes.map(node => node.id));
  const shown = new Set(local.map(node => node.id));
  const first = local.slice(0, cap);
  const seen = new Set<string>();
  const remote: SearchHit[] = [];
  let more = local.length > cap || hits.length >= MAP_SEARCH_CAP;
  for (const hit of hits) {
    if (!known.has(hit.node_id)) continue;
    if (hit.kind === "title" && shown.has(hit.node_id)) continue;
    const key = `${hit.kind}:${hit.node_id}:${hit.message_id ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (remote.length + first.length >= cap) { more = true; break; }
    remote.push(hit);
  }
  return { local: first, remote, more };
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
 * Question and answer hits navigate to the passage. The anchor must be text the rendered message
 * contains, so it is the match for `query` (case-insensitive) widened only across plain characters;
 * when the match is not in the snippet verbatim (accents folded by the index), the snippet's
 * longest plain stretch.
 */
export function searchAnchor(hit: SearchHit, query = ""): SearchAnchor | undefined {
  if ((hit.kind !== "message" && hit.kind !== "question") || hit.message_id == null) return undefined;
  const snippet = withoutEllipses(hit.snippet);
  const needle = query.trim();
  const match = needle ? new RegExp(escapeRegExp(needle), "iu").exec(snippet) : null;
  const text = match ? plainRun(snippet, match.index, match.index + match[0].length) : longestPlainRun(snippet);
  return { messageId: hit.message_id, ...(text ? { text } : {}) };
}

type Translate = (zh: string, en: string) => string;

/** What kind of record a history hit matched. */
export function hitKindLabel(kind: SearchHit["kind"], t: Translate): string {
  switch (kind) {
    case "title": return t("标题", "Title");
    case "question": return t("问题", "Question");
    case "message": return t("回答", "Answer");
    case "note": return t("笔记", "Note");
    case "memory": return t("记忆", "Memory");
    default: return t("记录", "Record");
  }
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
  /** Debounce `query` for `treeId`; `deliver` or `fail` only runs while this request is still current. */
  run(treeId: number, query: string, deliver: (hits: SearchHit[]) => void, fail?: () => void): void;
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
    run(treeId, query, deliver, fail) {
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
        }, () => {
          // A failed search leaves the instant group in place; an aborted one is not reported.
          if (current === ticket && !ctrl.signal.aborted) fail?.();
        });
      }, delayMs);
    },
    cancel,
  };
}
