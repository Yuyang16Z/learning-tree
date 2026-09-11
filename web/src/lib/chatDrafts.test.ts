import { describe, expect, it } from "vitest";
import { clearAcceptedDraft, moveFollowupDraft, type ChatDraft } from "./chatDrafts";

const sent: ChatDraft = { key: "tree:1:node:1", text: "原来的问题", images: ["data:image/png;base64,a"] };

describe("accepted chat drafts", () => {
  it("clears the accepted message and attachment even after a draft was restored from storage", () => {
    const restored = JSON.parse(JSON.stringify(sent));
    expect(clearAcceptedDraft(restored, sent)).toEqual({ key: sent.key, text: "", images: [] });
  });
  it("does not erase text or images edited while acceptance was pending", () => {
    const edited = { ...sent, text: "新的问题" };
    expect(clearAcceptedDraft(edited, sent)).toBe(edited);
    const newImage = { ...sent, images: [...sent.images, "data:image/png;base64,b"] };
    expect(clearAcceptedDraft(newImage, sent)).toBe(newImage);
  });
  it("does not erase a draft on another node or a new revision with identical text", () => {
    const other = { ...sent, key: "tree:1:node:2" };
    expect(clearAcceptedDraft(other, sent)).toBe(other);
    const revision = { ...sent, revisionId: 10 };
    expect(clearAcceptedDraft(revision, sent)).toBe(revision);
  });
  it("moves the next question to the newly created conversation node and clears its old copy", () => {
    const next = { ...sent, text: "下一条问题" };
    const target = { key: "tree:1:node:2", text: "", images: [] };
    expect(moveFollowupDraft(next, target)).toEqual({ source: { key: sent.key, text: "", images: [] }, target: { ...next, key: target.key } });
  });
  it("keeps both node drafts when the destination already has one", () => {
    expect(moveFollowupDraft(sent, { key: "tree:1:node:2", text: "该节点自己的草稿", images: [] })).toBeNull();
  });
});
