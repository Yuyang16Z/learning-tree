import type { TreeNode, Understanding } from "../types";

/** The learner's own marks, in the order the controls show them. */
export const UNDERSTANDING_LEVELS: readonly Understanding[] = ["open", "partial", "clear"];

/** Interface text as [zh, en] pairs for `t(...)`. */
export const UNDERSTANDING_TEXT: Record<Understanding, [string, string]> = {
  open: ["还没懂", "Not yet"],
  partial: ["大致懂了", "Roughly"],
  clear: ["能讲清楚", "Can explain"],
};

export function isUnderstanding(value: unknown): value is Understanding {
  return typeof value === "string" && (UNDERSTANDING_LEVELS as readonly string[]).includes(value);
}

/** Choosing the active mark again clears it; any other choice replaces it. */
export function toggleUnderstanding(current: Understanding | null | undefined, chosen: Understanding): Understanding | null {
  return current === chosen ? null : chosen;
}

type Marked = { understanding?: Understanding | null };

/** Optimistic update: the same array comes back when nothing changes, so React can skip work. */
export function withUnderstanding<T extends Marked>(items: readonly T[], isTarget: (item: T) => boolean, value: Understanding | null): T[] {
  let changed = false;
  const next = items.map(item => {
    if (!isTarget(item) || (item.understanding ?? null) === value) return item;
    changed = true;
    return { ...item, understanding: value };
  });
  return changed ? next : (items as T[]);
}

/**
 * Roll back a failed save. Only rows still showing the attempted mark revert, so a
 * newer value from another window that arrived meanwhile is kept.
 */
export function restoreUnderstanding<T extends Marked>(items: readonly T[], isTarget: (item: T) => boolean, attempted: Understanding | null, previous: Understanding | null): T[] {
  return withUnderstanding(items, item => isTarget(item) && (item.understanding ?? null) === attempted, previous);
}

/** The map lists at most this many unresolved questions, like its search results. */
export const UNRESOLVED_CAP = 30;

/**
 * Listings report "idle" only for a node without messages. Marks belong to answers, so
 * one on such a node (set through the API or an imported backup) does not count.
 */
function answered(node: TreeNode): boolean {
  return node.status !== "idle";
}

/** Answered questions the learner marked "Not yet" in this topic, newest first; `total` counts them all. */
export function unresolvedNodes(nodes: readonly TreeNode[], cap = UNRESOLVED_CAP): { items: TreeNode[]; total: number } {
  const marked = [...new Map(nodes.map(node => [node.id, node])).values()]
    .filter(node => node.understanding === "open" && answered(node))
    .sort((a, b) => b.id - a.id);
  return { items: marked.slice(0, Math.max(0, cap)), total: marked.length };
}

const RUNNING = new Set(["pending", "streaming"]);
const FAILED = new Set(["error", "failed", "interrupted"]);

/**
 * A topic card is coloured by the mark on its last node, when that node has an answer.
 * Pending and failed answers keep their own glyphs, so the colour is left out while one is shown.
 */
export function cardUnderstanding(nodes: readonly TreeNode[]): { value: Understanding | null; className: string } {
  const last = nodes[nodes.length - 1];
  const mark = last && answered(last) ? last.understanding : null;
  const value = isUnderstanding(mark) ? mark : null;
  const busy = nodes.some(node => RUNNING.has(node.status ?? "") || FAILED.has(node.status ?? ""));
  return { value, className: value && !busy ? `u-${value}` : "" };
}
