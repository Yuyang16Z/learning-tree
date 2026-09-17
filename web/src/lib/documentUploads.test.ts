import { describe, expect, it, vi } from "vitest";
import { documentFileError, documentMetadata, DocumentUploadQueue, MAX_DOCUMENT_BYTES } from "./documentUploads";
import type { DocumentSummary } from "../types";

const summary = (name: string): DocumentSummary => ({ id: name, name, media_type: "text/plain", size: 12, characters: 12, warnings: [] });
const file = (name: string) => new File(["sample text"], name);
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };

describe("document upload boundaries", () => {
  it("accepts supported case-insensitive extensions and rejects empty, oversized and older binary Word files", () => {
    for (const name of ["notes.PDF", "notes.docx", "notes.txt", "notes.md", "notes.csv", "notes.tsv", "notes.json", "notes.log"]) expect(documentFileError(file(name))).toBeNull();
    expect(documentFileError(file("notes.doc"))).toBe("type");
    expect(documentFileError(file("notes.pdf.exe"))).toBe("type");
    expect(documentFileError({ name: "notes.pdf", size: 0 })).toBe("empty");
    expect(documentFileError({ name: "notes.pdf", size: MAX_DOCUMENT_BYTES + 1 })).toBe("size");
  });
  it("restores only attachment metadata, without parsed sections or unknown content", () => {
    expect(documentMetadata(undefined)).toEqual([]);
    expect(documentMetadata([{ ...summary("notes.txt"), text: "private full text", sections: [{ text: "private" }] }])).toEqual([summary("notes.txt")]);
    expect(documentMetadata([null, { id: 1 }, summary("valid.txt")])).toEqual([summary("valid.txt")]);
    expect(documentMetadata([summary("same.txt"), summary("same.txt")])).toEqual([summary("same.txt")]);
  });
});

describe("draft-scoped document upload queue", () => {
  function setup() {
    const drafts = new Map<string, DocumentSummary[]>();
    const requests: { name: string; node: number; resolve: (doc: DocumentSummary) => void; reject: (error: Error) => void; signal: AbortSignal }[] = [];
    const upload = vi.fn((node: number, selected: File, _progress: (n: number) => void, signal: AbortSignal) => new Promise<DocumentSummary>((resolve, reject) => {
      requests.push({ name: selected.name, node, resolve, reject, signal });
    }));
    const queue = new DocumentUploadQueue({ documents: key => drafts.get(key) ?? [], upload,
      complete: (key, document) => drafts.set(key, [...(drafts.get(key) ?? []), document]), changed: vi.fn() });
    return { queue, drafts, requests, upload };
  }
  it("reserves slots before starting uploads and preserves selection order in each draft", async () => {
    const { queue, requests, drafts } = setup();
    for (const name of ["one.txt", "two.pdf", "three.docx", "four.csv"]) expect(queue.enqueue("A", 1, file(name))).toBeNull();
    expect(queue.enqueue("A", 1, file("five.md"))).toBe("limit");
    expect(requests.map(r => r.name)).toEqual(["one.txt"]);
    requests[0].resolve(summary("one.txt")); await tick();
    expect(requests.map(r => r.name)).toEqual(["one.txt", "two.pdf"]);
    requests[1].resolve(summary("two.pdf")); await tick();
    requests[2].resolve(summary("three.docx")); await tick();
    requests[3].resolve(summary("four.csv")); await tick();
    expect(drafts.get("A")?.map(d => d.name)).toEqual(["one.txt", "two.pdf", "three.docx", "four.csv"]);
    expect(queue.pending("A")).toBe(false);
  });
  it("keeps completions on their original draft when another topic uploads concurrently", async () => {
    const { queue, requests, drafts } = setup();
    queue.enqueue("A", 1, file("a.txt")); queue.enqueue("B", 2, file("b.txt"));
    requests[1].resolve(summary("b.txt")); await tick();
    expect(drafts.has("A")).toBe(false);
    requests[0].resolve(summary("a.txt")); await tick();
    expect(drafts.get("A")).toEqual([summary("a.txt")]);
    expect(drafts.get("B")).toEqual([summary("b.txt")]);
    expect(requests.map(r => r.node)).toEqual([1, 2]);
  });
  it("ignores late completion of a removed upload and never starts removed queued files", async () => {
    const { queue, requests, drafts } = setup();
    queue.enqueue("A", 1, file("a.txt")); queue.enqueue("A", 1, file("b.txt"));
    const [first, second] = queue.list("A");
    queue.remove("A", first.id); queue.remove("A", second.id);
    expect(requests[0].signal.aborted).toBe(true);
    requests[0].resolve(summary("a.txt")); await tick();
    expect(drafts.has("A")).toBe(false);
    expect(requests).toHaveLength(1);
  });
  it("exposes failures without dropping the remaining selected documents", async () => {
    const { queue, requests, drafts } = setup();
    queue.enqueue("A", 1, file("bad.pdf")); queue.enqueue("A", 1, file("good.txt"));
    requests[0].reject(new Error("No extractable text")); await tick();
    expect(queue.list("A")[0]).toMatchObject({ status: "error", error: "No extractable text" });
    requests[1].resolve(summary("good.txt")); await tick();
    expect(drafts.get("A")).toEqual([summary("good.txt")]);
    expect(queue.pending("A")).toBe(false);
    expect(queue.list("A")).toHaveLength(1);
  });
  it("cancels all pending uploads for a removed draft and ignores their late completion", async () => {
    const { queue, requests, drafts } = setup();
    queue.enqueue("A", 1, file("removed.txt")); queue.enqueue("A", 1, file("queued.txt"));
    queue.enqueue("B", 2, file("kept.txt"));
    queue.cancel("A");
    expect(requests[0].signal.aborted).toBe(true);
    expect(requests[1].signal.aborted).toBe(false);
    requests[0].resolve(summary("removed.txt")); requests[1].resolve(summary("kept.txt")); await tick();
    expect(queue.list("A")).toEqual([]);
    expect(drafts.has("A")).toBe(false);
    expect(drafts.get("B")).toEqual([summary("kept.txt")]);
    expect(requests).toHaveLength(2);
  });

});
