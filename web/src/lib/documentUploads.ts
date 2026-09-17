import type { DocumentSummary } from "../types";

export const MAX_DOCUMENTS = 4;
export const MAX_DOCUMENT_BYTES = 10 * 1024 * 1024;
export const DOCUMENT_ACCEPT = ".pdf,.docx,.txt,.md,.csv,.tsv,.json,.log";

export function documentFileError(file: Pick<File, "name" | "size">): "type" | "empty" | "size" | null {
  if (!/\.(pdf|docx|txt|md|csv|tsv|json|log)$/i.test(file.name)) return "type";
  if (!file.size) return "empty";
  if (file.size > MAX_DOCUMENT_BYTES) return "size";
  return null;
}

/** Pick metadata explicitly: parsed document content must never enter a browser draft. */
export function documentMetadata(value: unknown): DocumentSummary[] {
  if (!Array.isArray(value)) return [];
  const valid = value.filter((item): item is DocumentSummary => !!item && typeof item.id === "string"
    && typeof item.name === "string" && typeof item.size === "number" && typeof item.characters === "number")
    .map(item => ({ id: item.id, name: item.name,
      media_type: typeof item.media_type === "string" ? item.media_type : "application/octet-stream",
      size: item.size, characters: item.characters,
      warnings: Array.isArray(item.warnings) ? item.warnings.filter(warning => typeof warning === "string") : [] }));
  return [...new Map(valid.map(item => [item.id, item])).values()].slice(0, MAX_DOCUMENTS);
}

export type DocumentUpload = {
  id: number; file: File; nodeId: number; status: "queued" | "uploading" | "error";
  progress: number; error?: string; controller: AbortController;
};

/** Reservations and completion belong to a draft key, even after the user navigates elsewhere. */
export class DocumentUploadQueue {
  private nextId = 0;
  private entries = new Map<string, DocumentUpload[]>();
  private running = new Set<string>();
  constructor(private options: {
    documents: (key: string) => DocumentSummary[];
    upload: (nodeId: number, file: File, progress: (percent: number) => void, signal: AbortSignal) => Promise<DocumentSummary>;
    complete: (key: string, document: DocumentSummary) => void;
    changed: () => void;
  }) {}

  list(key: string): DocumentUpload[] { return this.entries.get(key) ?? []; }
  pending(key: string): boolean { return this.list(key).some(item => item.status !== "error"); }
  enqueue(key: string, nodeId: number, file: File): "type" | "empty" | "size" | "limit" | null {
    const error = documentFileError(file);
    if (error) return error;
    if (this.options.documents(key).length + this.list(key).filter(item => item.status !== "error").length >= MAX_DOCUMENTS) return "limit";
    this.entries.set(key, [...this.list(key), { id: ++this.nextId, file, nodeId, status: "queued", progress: 0, controller: new AbortController() }]);
    this.options.changed();
    void this.run(key);
    return null;
  }
  remove(key: string, id: number) {
    const entry = this.list(key).find(item => item.id === id);
    this.entries.set(key, this.list(key).filter(item => item.id !== id));
    entry?.controller.abort();
    this.options.changed();
  }
  cancel(key: string) {
    const entries = this.list(key);
    this.entries.delete(key);
    for (const entry of entries) entry.controller.abort();
    this.options.changed();
  }
  private async run(key: string) {
    if (this.running.has(key)) return;
    this.running.add(key);
    try {
      let entry: DocumentUpload | undefined;
      while ((entry = this.list(key).find(item => item.status === "queued"))) {
        const item = entry;
        item.status = "uploading";
        this.options.changed();
        try {
          const document = await this.options.upload(item.nodeId, item.file, progress => {
            item.progress = progress; this.options.changed();
          }, item.controller.signal);
          if (this.list(key).includes(item)) {
            this.options.complete(key, document);
            this.entries.set(key, this.list(key).filter(current => current !== item));
          }
        } catch (error) {
          if (this.list(key).includes(item)) {
            item.status = "error";
            item.error = error instanceof Error ? error.message : String(error);
          }
        }
        this.options.changed();
      }
    } finally { this.running.delete(key); }
  }
}
