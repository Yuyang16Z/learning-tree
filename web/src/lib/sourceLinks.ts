import type { AnswerSource } from "../types";

const SCOPES: readonly unknown[] = ["path", "tree", "other"];
const isId = (value: unknown) => Number.isSafeInteger(value) && (value as number) > 0;
const isOptionalId = (value: unknown) => value === null || isId(value);

/**
 * Sources as streamed or stored with an answer, keeping only well-formed entries (once per memory),
 * so one malformed entry cannot break rendering. Null when none survive.
 */
export function parseSources(value: unknown): AnswerSource[] | null {
  if (!Array.isArray(value)) return null;
  const seen = new Set<number>();
  const sources: AnswerSource[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) continue;
    const { memory_id, source_node_id, tree_id, tree_title, scope } = item as Record<string, unknown>;
    if (!isId(memory_id) || seen.has(memory_id as number) || !isOptionalId(source_node_id) || !isOptionalId(tree_id)) continue;
    if (typeof tree_title !== "string" || !SCOPES.includes(scope)) continue;
    seen.add(memory_id as number);
    const source: AnswerSource = { memory_id: memory_id as number, source_node_id: source_node_id as number | null, tree_id: tree_id as number | null, tree_title, scope: scope as AnswerSource["scope"] };
    const { muted } = item as { muted?: unknown };
    if (typeof muted === "boolean") source.muted = muted;
    sources.push(source);
  }
  return sources.length ? sources : null;
}

/** What a Referenced chip can do: open its source node, show that it was deleted, or nothing. */
export type SourceLink = { kind: "open"; treeId: number; nodeId: number } | { kind: "deleted" } | { kind: "none" };

export interface SourceView {
  activeTreeId: number | null;
  /** Topics this page knows, archived ones included; empty until the list has loaded. */
  topicIds: ReadonlySet<number>;
  /** Nodes on the open topic's map; empty while the map is loading. */
  nodeIds: ReadonlySet<number>;
  /** Whether this view can switch to another topic at all. */
  canOpenTopics: boolean;
}

/**
 * Stored provenance can outlive its source, so a chip checks the topic list and the open map.
 * A node of another topic is only known once that topic loads; the click checks it then.
 */
export function sourceLink(source: AnswerSource, view: SourceView): SourceLink {
  const treeId = source.tree_id;
  const nodeId = source.source_node_id;
  if (treeId == null || nodeId == null) return { kind: "none" };
  if (treeId === view.activeTreeId) {
    return view.nodeIds.size && !view.nodeIds.has(nodeId) ? { kind: "deleted" } : { kind: "open", treeId, nodeId };
  }
  if (view.topicIds.size && !view.topicIds.has(treeId)) return { kind: "deleted" };
  return view.canOpenTopics ? { kind: "open", treeId, nodeId } : { kind: "none" };
}

/**
 * Ask the server whether a source still exists before leaving the current view: a 404 for its
 * topic, or a node missing from that topic, means it was deleted. Other failures are rethrown.
 */
export async function locateSource(
  target: { treeId: number; nodeId: number | null },
  loadNodes: (treeId: number) => Promise<readonly { id: number }[]>,
): Promise<"found" | "deleted"> {
  let nodes: readonly { id: number }[];
  try { nodes = await loadNodes(target.treeId); }
  catch (error) {
    if ((error as { status?: unknown } | null)?.status === 404) return "deleted";
    throw error;
  }
  return target.nodeId === null || nodes.some(node => node.id === target.nodeId) ? "found" : "deleted";
}
