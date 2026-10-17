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

const RUNNING = new Set(["pending", "streaming"]);
const FAILED = new Set(["error", "failed", "interrupted"]);

/**
 * A topic card is coloured by the mark on its last node. Pending and failed answers
 * keep their own glyphs, so the colour is left out while one is shown.
 */
export function cardUnderstanding(nodes: readonly TreeNode[]): { value: Understanding | null; className: string } {
  const mark = nodes[nodes.length - 1]?.understanding;
  const value = isUnderstanding(mark) ? mark : null;
  const busy = nodes.some(node => RUNNING.has(node.status ?? "") || FAILED.has(node.status ?? ""));
  return { value, className: value && !busy ? `u-${value}` : "" };
}
