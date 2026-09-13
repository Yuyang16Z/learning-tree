import type { DocumentSummary } from "../types";

export type ChatDraft = {
  key: string;
  text: string;
  images: string[];
  documents?: DocumentSummary[];
  revisionId?: number;
  revisionMessageId?: number;
  previous?: { text: string; images: string[]; documents?: DocumentSummary[] };
};

export function isEmptyDraft(draft: ChatDraft): boolean {
  return !draft.text && !draft.images.length && !draft.documents?.length && !draft.revisionId;
}

/** The server accepted this snapshot; edits made while it was in flight belong to the next draft. */
export function clearAcceptedDraft(current: ChatDraft, submitted: ChatDraft): ChatDraft {
  if (current.key !== submitted.key || current.text !== submitted.text || current.revisionId !== submitted.revisionId
    || current.revisionMessageId !== submitted.revisionMessageId || JSON.stringify(current.images) !== JSON.stringify(submitted.images)
    || JSON.stringify(current.documents ?? []) !== JSON.stringify(submitted.documents ?? [])) return current;
  return { key: current.key, text: "", images: [] };
}

/** Carry the next question along with automatic navigation, without overwriting an existing node draft. */
export function moveFollowupDraft(source: ChatDraft, target: ChatDraft): { source: ChatDraft; target: ChatDraft } | null {
  if (source.key === target.key || isEmptyDraft(source) || !isEmptyDraft(target)) return null;
  return { source: { key: source.key, text: "", images: [] }, target: { ...source, key: target.key } };
}
