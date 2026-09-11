import type { TreeNode } from "../types";

export const TOPIC_CARD = { width: 194, height: 86, roundHeight: 32, indent: 24, gap: 18, padding: 24 };

type TopicNode = TreeNode & { kind?: string; revision_of_node_id?: number | null };

export interface TopicGroup {
  id: number;
  parentId: number | null;
  sourceNodeId: number | null;
  title: string;
  nodes: TreeNode[];
  children: number[];
}

export interface TopicTree {
  groups: TopicGroup[];
  byId: Map<number, TopicGroup>;
  nodeToGroup: Map<number, number>;
  roots: number[];
}

export interface TopicPosition {
  group: TopicGroup;
  x: number;
  y: number;
  height: number;
  depth: number;
}

export interface TopicLayout {
  items: TopicPosition[];
  byId: Map<number, TopicPosition>;
  edges: { from: TopicPosition; to: TopicPosition }[];
  width: number;
  height: number;
}

function startsTopic(node: TopicNode): boolean {
  return Boolean(node.seed_text?.trim()) || node.kind === "branch" || node.kind === "revision" || node.revision_of_node_id != null;
}

/** Group only uninterrupted follow-ups. A branch keeps its original parent Q/A. */
export function groupTopics(input: TreeNode[], fallbackTitle = "新问题"): TopicTree {
  const sorted = [...new Map(input.map((node) => [node.id, node])).values()].sort((a, b) => a.id - b.id);
  const nodesById = new Map(sorted.map((node) => [node.id, node]));
  const children = new Map<number, TreeNode[]>();
  for (const node of sorted) {
    if (node.parent_id != null && node.parent_id !== node.id && nodesById.has(node.parent_id)) {
      const siblings = children.get(node.parent_id) ?? [];
      siblings.push(node);
      children.set(node.parent_id, siblings);
    }
  }
  const result: TopicTree = { groups: [], byId: new Map(), nodeToGroup: new Map(), roots: [] };
  const visited = new Set<number>();
  // An explicit stack avoids overflowing on a long imported learning history.
  const pending: { node: TreeNode; parent: number | null }[] = [];
  const append = (start: TreeNode) => {
    pending.push({ node: start, parent: null });
    while (pending.length) {
      const { node: first, parent } = pending.pop()!;
      if (visited.has(first.id)) continue;
      const members: TreeNode[] = [];
      let current = first;
      while (!visited.has(current.id)) {
        visited.add(current.id);
        members.push(current);
        const next = children.get(current.id) ?? [];
        if (next.length !== 1 || startsTopic(next[0]) || visited.has(next[0].id)) break;
        current = next[0];
      }
      const group: TopicGroup = {
        id: first.id,
        parentId: parent,
        sourceNodeId: parent == null ? null : first.parent_id,
        title: first.title.trim() || first.seed_text?.trim() || fallbackTitle,
        nodes: members,
        children: [],
      };
      result.groups.push(group);
      result.byId.set(group.id, group);
      for (const member of members) result.nodeToGroup.set(member.id, group.id);
      if (parent == null) result.roots.push(group.id);
      else result.byId.get(parent)?.children.push(group.id);
      const next = children.get(current.id) ?? [];
      for (let index = next.length - 1; index >= 0; index--) {
        if (!visited.has(next[index].id)) pending.push({ node: next[index], parent: group.id });
      }
    }
  };
  for (const node of sorted) {
    if (node.parent_id == null || node.parent_id === node.id || !nodesById.has(node.parent_id)) append(node);
  }
  // Keep malformed imports navigable, including disconnected cycles.
  for (const node of sorted) if (!visited.has(node.id)) append(node);
  return result;
}

export function groupAncestorIds(tree: TopicTree, nodeId: number | null): Set<number> {
  const ancestors = new Set<number>();
  const groupId = nodeId == null ? undefined : tree.nodeToGroup.get(nodeId);
  let parent = groupId == null ? null : tree.byId.get(groupId)?.parentId;
  while (parent != null && !ancestors.has(parent)) {
    ancestors.add(parent);
    parent = tree.byId.get(parent)?.parentId;
  }
  return ancestors;
}

/** A compact outline layout keeps siblings readable in a narrow map pane. */
export function layoutTopics(tree: TopicTree, collapsed = new Set<number>(), expanded = new Set<number>()): TopicLayout {
  const result: TopicLayout = { items: [], byId: new Map(), edges: [], width: 0, height: 0 };
  const stack = [...tree.roots].reverse().map((id) => ({ id, depth: 0 }));
  let y = TOPIC_CARD.padding;
  while (stack.length) {
    const { id, depth } = stack.pop()!;
    const group = tree.byId.get(id);
    if (!group || result.byId.has(id)) continue;
    const height = TOPIC_CARD.height + (expanded.has(id) && group.nodes.length > 1 ? group.nodes.length * TOPIC_CARD.roundHeight + 10 : 0);
    const item = { group, depth, x: TOPIC_CARD.padding + depth * TOPIC_CARD.indent, y, height };
    result.items.push(item);
    result.byId.set(id, item);
    result.width = Math.max(result.width, item.x + TOPIC_CARD.width + TOPIC_CARD.padding);
    y += height + TOPIC_CARD.gap;
    if (!collapsed.has(id)) {
      for (let index = group.children.length - 1; index >= 0; index--) stack.push({ id: group.children[index], depth: depth + 1 });
    }
  }
  for (const item of result.items) {
    const parent = item.group.parentId == null ? undefined : result.byId.get(item.group.parentId);
    if (parent) result.edges.push({ from: parent, to: item });
  }
  result.height = result.items.length ? y - TOPIC_CARD.gap + TOPIC_CARD.padding : 0;
  return result;
}
