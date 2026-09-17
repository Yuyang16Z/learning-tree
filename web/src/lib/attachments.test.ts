import { describe, expect, it, vi } from "vitest";
import { attachmentKind, ImageAttachmentQueue, MAX_IMAGE_BYTES } from "./attachments";

const image = (name: string) => new File(["image"], name, { type: "image/png" });
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };

describe("mixed attachments", () => {
  it("routes images and documents from one selection, including missing image MIME types", () => {
    const files = [image("diagram.png"), new File(["notes"], "notes.pdf", { type: "application/pdf" }),
      new File(["image"], "photo.JPG"), new File(["notes"], "notes.docx"), new File(["binary"], "image.png.exe")];
    expect(files.map(attachmentKind)).toEqual(["image", "document", "image", "document", "document"]);
    expect(attachmentKind({ name: "notes.png", type: "application/pdf" })).toBe("document");
  });
});

describe("draft-scoped image reads", () => {
  function setup() {
    const drafts = new Map<string, string[]>();
    const requests: { file: File; resolve: (value: string) => void; reject: (error: Error) => void }[] = [];
    const failed = vi.fn();
    const queue = new ImageAttachmentQueue({ images: key => drafts.get(key) ?? [],
      read: file => new Promise((resolve, reject) => requests.push({ file, resolve, reject })),
      complete: (key, value) => drafts.set(key, [...(drafts.get(key) ?? []), value]), failed, changed: vi.fn() });
    return { drafts, requests, queue, failed };
  }
  it("reserves all four slots before reads finish and rejects oversized or empty images", async () => {
    const { queue, requests, drafts } = setup();
    for (let i = 0; i < 4; i++) expect(queue.enqueue("A", image(`${i}.png`))).toBeNull();
    expect(queue.enqueue("A", image("extra.png"))).toBe("limit");
    expect(queue.enqueue("B", new File([], "empty.png"))).toBe("empty");
    expect(queue.enqueue("B", { ...image("large.png"), size: MAX_IMAGE_BYTES + 1 } as File)).toBe("size");
    for (let i = 0; i < 4; i++) { expect(requests[i].file.name).toBe(`${i}.png`); requests[i].resolve(`data:image/png;base64,${i}`); await tick(); }
    expect(drafts.get("A")).toHaveLength(4);
    expect(queue.count("A")).toBe(0);
  });
  it("keeps images on the originating draft when the user switches branches", async () => {
    const { queue, requests, drafts } = setup();
    queue.enqueue("A", image("a.png")); queue.enqueue("B", image("b.png"));
    requests[1].resolve("image B"); await tick();
    requests[0].resolve("image A"); await tick();
    expect(drafts.get("A")).toEqual(["image A"]);
    expect(drafts.get("B")).toEqual(["image B"]);
  });
  it("releases failed slots and continues reading the rest of a selection", async () => {
    const { queue, requests, drafts, failed } = setup();
    const broken = image("broken.png");
    queue.enqueue("A", broken); queue.enqueue("A", image("good.png"));
    requests[0].reject(new Error("unreadable")); await tick();
    expect(failed).toHaveBeenCalledWith("A", broken);
    requests[1].resolve("good image"); await tick();
    expect(drafts.get("A")).toEqual(["good image"]);
    expect(queue.count("A")).toBe(0);
    expect(queue.enqueue("A", image("retry.png"))).toBeNull();
  });
  it("cancels a deleted draft's pending reads without restoring images or changing another draft", async () => {
    const { queue, requests, drafts, failed } = setup();
    queue.enqueue("A", image("removed.png")); queue.enqueue("A", image("queued.png"));
    queue.enqueue("B", image("kept.png"));
    queue.cancel("A");
    requests[0].resolve("removed image"); requests[1].resolve("kept image"); await tick();
    expect(queue.count("A")).toBe(0);
    expect(drafts.has("A")).toBe(false);
    expect(drafts.get("B")).toEqual(["kept image"]);
    expect(requests).toHaveLength(2);
    expect(failed).not.toHaveBeenCalled();
  });

});
