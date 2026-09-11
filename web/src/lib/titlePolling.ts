import type { TreeNode } from "../types";

/** Update titles only: polling must not replace newer tree structure or answer state. */
export function mergePendingTitles(current: TreeNode[], incoming: TreeNode[]): TreeNode[] {
  const byId = new Map(incoming.map(node => [node.id, node]));
  let changed = false;
  const next = current.map(node => {
    if (node.title_state !== "pending") return node;
    const received = byId.get(node.id);
    if (!received || received.tree_id !== node.tree_id || !received.title_state) return node;
    if (received.title === node.title && received.title_state === node.title_state) return node;
    changed = true;
    return { ...node, title: received.title, title_state: received.title_state };
  });
  // Keeping the same reference avoids rebuilding and repositioning an unchanged map.
  return changed ? next : current;
}

interface PollOptions {
  treeId: number;
  load: (treeId: number, signal: AbortSignal) => Promise<TreeNode[]>;
  revision: () => number;
  isActive: (treeId: number) => boolean;
  commit: (nodes: TreeNode[], revision: number) => void;
  intervalMs?: number;
  timeoutMs?: number;
}

/** Poll just while a title is being summarized; navigation cancels pending work. */
export function pollPendingTitles({
  treeId, load, revision, isActive, commit, intervalMs = 1500, timeoutMs = 30000,
}: PollOptions): () => void {
  const controller = new AbortController();
  let stopped = false;
  let next: ReturnType<typeof setTimeout> | undefined;
  const stop = () => {
    stopped = true;
    clearTimeout(next);
    clearTimeout(deadline);
    controller.abort();
  };
  const deadline = setTimeout(stop, timeoutMs);
  const current = (ticket: number) => !stopped && isActive(treeId) && revision() === ticket;
  async function poll() {
    if (stopped || !isActive(treeId)) { stop(); return; }
    const ticket = revision();
    try {
      const nodes = await load(treeId, controller.signal);
      if (current(ticket)) {
        commit(nodes, ticket);
        if (!nodes.some(node => node.title_state === "pending")) { stop(); return; }
      }
    } catch {
      // Title refresh is best effort; a network hiccup must not interrupt the answer.
    }
    if (!stopped) next = setTimeout(poll, intervalMs);
  }
  next = setTimeout(poll, intervalMs);
  return stop;
}
