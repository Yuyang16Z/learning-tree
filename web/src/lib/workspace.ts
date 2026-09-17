export function readSaved<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) ?? 'null') ?? fallback; } catch { return fallback; }
}
export function saveValue(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}
/** A response may commit only while its navigation ticket is still current. */
export class NavigationGate {
  private revision = 0;
  next() { return ++this.revision; }
  current(ticket: number) { return ticket === this.revision; }
}

/** IDs only: deletion notifications never contain a copy of the removed content. */
export type WorkspaceDeletion = { treeId: number; nodeIds?: number[] };

/** A remote sibling deletion must not interrupt an unrelated response in the same topic. */
export function deletionAffectsRequest(
  deletion: WorkspaceDeletion,
  request: { tree: number; from: number } | null,
  targetNodeId: number | null,
): boolean {
  if (!request || deletion.treeId !== request.tree) return false;
  return deletion.nodeIds === undefined || deletion.nodeIds.includes(request.from)
    || (targetNodeId !== null && deletion.nodeIds.includes(targetNodeId));
}

export const WORKSPACE_DELETION_KEY = 'bl-workspace-deleted';
const WORKSPACE_DELETION_EVENT = 'learning-tree-workspace-deleted';
type BrowserStorage = Pick<Storage, 'length' | 'key' | 'getItem' | 'removeItem' | 'setItem'>;
const nodeStorageKey = /^bl-(?:draft-v2|scroll-v2|note-draft-v2):(\d+):(\d+|none|null)$/;

function storageKeys(storage: BrowserStorage) {
  return Array.from({ length: storage.length }, (_, index) => storage.key(index)).filter((key): key is string => key !== null);
}

function savedId(storage: BrowserStorage, key: string): number | null {
  try {
    const value = JSON.parse(storage.getItem(key) ?? 'null');
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  } catch { return null; }
}

export function matchesDeletedWorkspace(key: string, deletion: WorkspaceDeletion): boolean {
  const match = key.match(nodeStorageKey);
  return !!match && Number(match[1]) === deletion.treeId
    && (deletion.nodeIds === undefined || deletion.nodeIds.includes(Number(match[2])));
}

export function purgeWorkspace(deletion: WorkspaceDeletion, storage: BrowserStorage = localStorage): void {
  try {
    for (const key of storageKeys(storage)) {
      if (matchesDeletedWorkspace(key, deletion)) storage.removeItem(key);
    }
    const navigationKey = `bl-node:${deletion.treeId}`;
    const selectedNode = savedId(storage, navigationKey);
    if (deletion.nodeIds === undefined || (selectedNode !== null && deletion.nodeIds.includes(selectedNode))) storage.removeItem(navigationKey);
    if (deletion.nodeIds === undefined && savedId(storage, 'bl-tree') === deletion.treeId) storage.removeItem('bl-tree');
  } catch { /* Storage may be unavailable; in-memory cleanup still runs. */ }
}

export function clearLegacyRecovery(storage: BrowserStorage = localStorage): void {
  try { storage.removeItem('bl-recovery'); } catch { /* Optional browser storage. */ }
}

export function removeWorkspace(deletion: WorkspaceDeletion, broadcast = true): void {
  purgeWorkspace(deletion);
  window.dispatchEvent(new CustomEvent(WORKSPACE_DELETION_EVENT, { detail: deletion }));
  if (broadcast) {
    try { localStorage.setItem(WORKSPACE_DELETION_KEY, JSON.stringify({ ...deletion, nonce: crypto.randomUUID() })); }
    catch { /* The current window remains cleaned even when storage is disabled. */ }
  }
}

function parseDeletion(raw: string | null): WorkspaceDeletion | null {
  try {
    const value = JSON.parse(raw ?? 'null');
    if (!Number.isSafeInteger(value?.treeId) || value.treeId < 1) return null;
    if (value.nodeIds !== undefined && (!Array.isArray(value.nodeIds) || !value.nodeIds.every((id: unknown) => Number.isSafeInteger(id) && Number(id) > 0))) return null;
    return { treeId: value.treeId, ...(value.nodeIds === undefined ? {} : { nodeIds: value.nodeIds }) };
  } catch { return null; }
}

export function onWorkspaceDeletion(listener: (deletion: WorkspaceDeletion, remote: boolean) => void): () => void {
  const local = (event: Event) => listener((event as CustomEvent<WorkspaceDeletion>).detail, false);
  const remote = (event: StorageEvent) => {
    if (event.key !== WORKSPACE_DELETION_KEY) return;
    const deletion = parseDeletion(event.newValue);
    if (!deletion) return;
    purgeWorkspace(deletion);
    listener(deletion, true);
  };
  window.addEventListener(WORKSPACE_DELETION_EVENT, local);
  window.addEventListener('storage', remote);
  return () => {
    window.removeEventListener(WORKSPACE_DELETION_EVENT, local);
    window.removeEventListener('storage', remote);
  };
}

/** Only reconcile against a successful complete list including archived topics. */
export function orphanedWorkspaceTrees(treeIds: number[], storage: BrowserStorage = localStorage): number[] {
  const existing = new Set(treeIds);
  const orphaned = new Set<number>();
  try {
    for (const key of storageKeys(storage)) {
      const match = key.match(nodeStorageKey) ?? key.match(/^bl-node:(\d+)$/);
      if (match && !existing.has(Number(match[1]))) orphaned.add(Number(match[1]));
    }
    const selected = savedId(storage, 'bl-tree');
    if (selected !== null && !existing.has(selected)) orphaned.add(selected);
  } catch { /* Do not infer deletion from a storage error. */ }
  return [...orphaned];
}

/** A successful node list reconciles branch deletions missed while this tab was asleep. */
export function orphanedWorkspaceNodes(treeId: number, nodeIds: number[], storage: BrowserStorage = localStorage): number[] {
  const existing = new Set(nodeIds);
  const orphaned = new Set<number>();
  try {
    for (const key of storageKeys(storage)) {
      const match = key.match(nodeStorageKey);
      if (!match || Number(match[1]) !== treeId || !/^\d+$/.test(match[2])) continue;
      const id = Number(match[2]);
      if (!existing.has(id)) orphaned.add(id);
    }
    const selected = savedId(storage, `bl-node:${treeId}`);
    if (selected !== null && !existing.has(selected)) orphaned.add(selected);
  } catch { /* A failed read is not evidence of a deleted node. */ }
  return [...orphaned];
}
