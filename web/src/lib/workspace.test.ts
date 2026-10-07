import { describe, expect, it } from 'vitest';
import { clearLegacyRecovery, deletionAffectsRequest, matchesDeletedWorkspace, NavigationGate, orphanedWorkspaceNodes, orphanedWorkspaceTrees, purgeWorkspace } from './workspace';

function storageWith(values: Record<string, string>) {
  const data = new Map(Object.entries(values));
  return {
    get length() { return data.size; },
    key: (index: number) => [...data.keys()][index] ?? null,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); },
    removeItem: (key: string) => { data.delete(key); },
    data,
  };
}

describe('permanent workspace cleanup', () => {
  it('preserves an unrelated stream when another tab deletes a sibling branch', () => {
    const request = { tree: 2, from: 20 };
    expect(deletionAffectsRequest({ treeId: 2, nodeIds: [21, 22] }, request, 23)).toBe(false);
    expect(deletionAffectsRequest({ treeId: 3 }, request, 23)).toBe(false);
    expect(deletionAffectsRequest({ treeId: 2 }, null, null)).toBe(false);
    expect(deletionAffectsRequest({ treeId: 2, nodeIds: [20] }, request, null)).toBe(true);
    expect(deletionAffectsRequest({ treeId: 2, nodeIds: [23] }, request, 23)).toBe(true);
    expect(deletionAffectsRequest({ treeId: 2 }, request, 23)).toBe(true);
  });

  it('purges server-confirmed descendants absent from a stale local map', () => {
    const localMapIds = [20, 21];
    const serverDeleted = [21, 22];
    expect(localMapIds).not.toContain(22);
    const storage = storageWith({
      'bl-draft-v2:2:20': 'surviving source',
      'bl-draft-v2:2:21': 'deleted known branch',
      'bl-draft-v2:2:22': 'new descendant draft from another tab',
      'bl-note-draft-v2:2:22': 'new descendant note',
    });
    purgeWorkspace({ treeId: 2, nodeIds: serverDeleted }, storage);
    expect([...storage.data]).toEqual([['bl-draft-v2:2:20', 'surviving source']]);
  });

  it('removes large image drafts, reflection drafts, scroll and navigation only for the deleted tree', () => {
    const storage = storageWith({
      'bl-draft-v2:2:20': JSON.stringify({ images: ['data:image/png;base64,' + 'a'.repeat(500_000)], text: 'private' }),
      'bl-draft-v2:2:null': 'private pending draft',
      'bl-note-draft-v2:2:none': 'private reflection',
      'bl-note-draft-v2:2:21': 'private reflection',
      'bl-scroll-v2:2:20': '250', 'bl-node:2': '20', 'bl-tree': '2',
      'bl-draft-v2:20:200': 'unrelated topic', 'bl-draft-v2:3:30': 'other topic',
      'bl-node:3': '30', 'bl-theme': 'dark', 'bl-model': '2', 'bl-locale': 'en',
      'bl-map-open': 'true', 'bl-right-w': '420', 'external:2': 'unrelated app value',
    });
    purgeWorkspace({ treeId: 2 }, storage);
    expect([...storage.data.keys()].filter(key => matchesDeletedWorkspace(key, { treeId: 2 }))).toEqual([]);
    expect(storage.getItem('bl-node:2')).toBeNull();
    expect(storage.getItem('bl-tree')).toBeNull();
    expect(storage.getItem('bl-draft-v2:20:200')).toBe('unrelated topic');
    expect(storage.getItem('bl-draft-v2:3:30')).toBe('other topic');
    expect(storage.getItem('bl-theme')).toBe('dark');
    expect(storage.getItem('bl-model')).toBe('2');
    expect(storage.getItem('bl-locale')).toBe('en');
    expect(storage.getItem('bl-map-open')).toBe('true');
    expect(storage.getItem('bl-right-w')).toBe('420');
    expect(storage.getItem('external:2')).toBe('unrelated app value');
  });

  it('cleans only deleted branch descendants and retains sibling drafts and tree navigation', () => {
    const storage = storageWith({
      'bl-draft-v2:2:20': 'root', 'bl-draft-v2:2:21': 'removed', 'bl-note-draft-v2:2:22': 'removed child',
      'bl-scroll-v2:2:22': '90', 'bl-note-draft-v2:2:none': 'root note',
      'bl-node:2': '22', 'bl-tree': '2', 'bl-draft-v2:3:22': 'other topic',
    });
    purgeWorkspace({ treeId: 2, nodeIds: [21, 22] }, storage);
    expect(storage.getItem('bl-draft-v2:2:21')).toBeNull();
    expect(storage.getItem('bl-note-draft-v2:2:22')).toBeNull();
    expect(storage.getItem('bl-scroll-v2:2:22')).toBeNull();
    expect(storage.getItem('bl-node:2')).toBeNull();
    expect(storage.getItem('bl-tree')).toBe('2');
    expect(storage.getItem('bl-draft-v2:2:20')).toBe('root');
    expect(storage.getItem('bl-note-draft-v2:2:none')).toBe('root note');
    expect(storage.getItem('bl-draft-v2:3:22')).toBe('other topic');
  });

  it('keeps selected sibling or another topic navigation', () => {
    const storage = storageWith({ 'bl-node:2': '20', 'bl-tree': '3' });
    purgeWorkspace({ treeId: 2, nodeIds: [21] }, storage);
    expect(storage.getItem('bl-node:2')).toBe('20');
    purgeWorkspace({ treeId: 2 }, storage);
    expect(storage.getItem('bl-tree')).toBe('3');
  });

  it('reconciles only missing topic IDs in recognized keys, including saved selection', () => {
    const storage = storageWith({
      'bl-draft-v2:2:20': 'orphan', 'bl-note-draft-v2:3:none': 'archived kept',
      'bl-node:4': '40', 'bl-tree': '5', 'bl-model': '6', 'unknown:7': 'keep',
    });
    expect(orphanedWorkspaceTrees([3], storage).sort()).toEqual([2, 4, 5]);
    expect(storage.getItem('bl-draft-v2:2:20')).toBe('orphan');
  });

  it('removes obsolete recovery content without clearing other local settings or backups', () => {
    const storage = storageWith({ 'bl-recovery': 'old deleted data', 'bl-theme': 'dark', 'custom-export': 'manual file info' });
    clearLegacyRecovery(storage);
    expect(storage.getItem('bl-recovery')).toBeNull();
    expect([...storage.data]).toEqual([['bl-theme', 'dark'], ['custom-export', 'manual file info']]);
  });

  it('reconciles missing node IDs only within the complete current topic', () => {
    const storage = storageWith({
      'bl-draft-v2:2:20': 'root', 'bl-scroll-v2:2:21': 'deleted branch',
      'bl-note-draft-v2:2:22': 'deleted child', 'bl-note-draft-v2:2:none': 'unscoped note',
      'bl-node:2': '23', 'bl-draft-v2:3:21': 'other topic',
    });
    expect(orphanedWorkspaceNodes(2, [20], storage).sort()).toEqual([21, 22, 23]);
    expect(storage.getItem('bl-draft-v2:2:20')).toBe('root');
    expect(storage.getItem('bl-draft-v2:3:21')).toBe('other topic');
  });

  it('does not leave whole-tree navigation behind when a node key has malformed JSON', () => {
    const storage = storageWith({ 'bl-node:2': 'broken', 'bl-tree': '2' });
    purgeWorkspace({ treeId: 2 }, storage);
    expect(storage.getItem('bl-node:2')).toBeNull();
    expect(storage.getItem('bl-tree')).toBeNull();
  });
});

describe('navigation tickets', () => {
  it('let a background reload commit only until the next navigation, without cancelling one', () => {
    const gate = new NavigationGate();
    const navigation = gate.next();
    const reload = gate.peek();
    expect(gate.current(navigation) && gate.current(reload)).toBe(true);
    gate.next();
    expect(gate.current(reload)).toBe(false);
  });
});
