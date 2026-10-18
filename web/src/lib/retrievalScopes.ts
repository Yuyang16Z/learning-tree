import type { RetrievalSettings } from "../types";

export type ScopeChange = Partial<Pick<RetrievalSettings, "include_siblings" | "include_other_trees">>;

/** The memory-scope switches as Settings shows them. */
export interface ScopeState {
  settings: RetrievalSettings | null;
  /** A save, or the reload after a conflicting save, has not finished; Settings stays open. */
  saving: boolean;
  /** After a save conflicted: "reloading" until the latest settings arrive, then "reloaded". */
  conflict: "reloading" | "reloaded" | null;
  /**
   * The last failed load or save, as the server or browser reported it. A later successful read
   * clears a failed read; a failed save stays reported until the next change.
   */
  error: string | null;
}

export interface ScopeStore {
  get(): ScopeState;
  subscribe(listener: () => void): () => void;
  /** Read the saved switches (again); ignored while a save or its conflict reload is running. */
  load(): Promise<void>;
  /**
   * Save one change with the known revision; ignored before the switches load or while saving.
   * A read still running is dropped, so it cannot replace the saved result with older settings.
   */
  update(change: ScopeChange): Promise<void>;
  /** Settings closed: nothing still in flight changes the state any more. */
  close(): void;
}

interface StoreOptions {
  load: (signal: AbortSignal) => Promise<RetrievalSettings>;
  save: (settings: RetrievalSettings) => Promise<RetrievalSettings>;
}

const messageOf = (error: unknown) => error instanceof Error ? error.message : String(error);

/**
 * Owned by Settings rather than by the Memory tab's panel, so a save that settles after the
 * learner switched tabs is still reported, and Settings can stay open until it does.
 */
export function createScopeStore({ load, save }: StoreOptions): ScopeStore {
  let state: ScopeState = { settings: null, saving: false, conflict: null, error: null };
  const listeners = new Set<() => void>();
  let reading: AbortController | null = null;
  // The error shown came from a read, so the next successful read replaces it.
  let readFailed = false;
  let closed = false;
  const set = (change: Partial<ScopeState>) => {
    if (closed) return;
    state = { ...state, ...change };
    for (const listener of [...listeners]) listener();
  };
  async function read(): Promise<boolean> {
    reading?.abort();
    const controller = new AbortController();
    reading = controller;
    try {
      const settings = await load(controller.signal);
      if (controller.signal.aborted) return false;
      set(readFailed ? { settings, error: null } : { settings });
      readFailed = false;
      return true;
    } catch (error) {
      if (controller.signal.aborted) return false;
      readFailed = true;
      set({ error: messageOf(error) });
      return false;
    }
  }
  return {
    get: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    async load() {
      if (!closed && !state.saving) await read();
    },
    async update(change) {
      const current = state.settings;
      if (!current || state.saving || closed) return;
      reading?.abort();
      readFailed = false;
      set({ saving: true, conflict: null, error: null });
      try {
        set({ settings: await save({ ...current, ...change }), saving: false });
      } catch (error) {
        if ((error as { status?: unknown } | null)?.status !== 409) {
          set({ saving: false, error: messageOf(error) });
          return;
        }
        // Changed elsewhere: say so while the latest settings load, never before they have.
        set({ conflict: "reloading" });
        const reloaded = await read();
        set({ saving: false, conflict: reloaded ? "reloaded" : null });
      }
    },
    close() {
      closed = true;
      reading?.abort();
      listeners.clear();
    },
  };
}
