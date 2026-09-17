import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./clipboard";

afterEach(() => vi.unstubAllGlobals());

describe("copying message text", () => {
  function fallback(succeeds: boolean) {
    const originalRange = { cloneRange: () => originalRange };
    const selection = { rangeCount: 1, getRangeAt: vi.fn(() => originalRange), removeAllRanges: vi.fn(), addRange: vi.fn() };
    const field = { value: "", readOnly: false, style: {}, setAttribute: vi.fn(), select: vi.fn(), remove: vi.fn() };
    const focus = vi.fn();
    const execCommand = vi.fn(() => succeeds);
    vi.stubGlobal("document", { activeElement: { focus }, createElement: vi.fn(() => field), body: { appendChild: vi.fn() }, execCommand });
    vi.stubGlobal("window", { getSelection: () => selection });
    return { field, selection, focus, execCommand, originalRange };
  }
  it("copies the original message including line breaks and Markdown", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const text = "**Explanation**\n\n```python\nprint('你好')\n```";
    await copyText(text);
    expect(writeText).toHaveBeenCalledWith(text);
  });
  it("falls back in a desktop WebView and restores focus and selection", async () => {
    vi.stubGlobal("navigator", {});
    const { field, selection, focus, execCommand, originalRange } = fallback(true);
    await copyText("学习树");
    expect(field.value).toBe("学习树");
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(field.remove).toHaveBeenCalledOnce();
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(selection.addRange).toHaveBeenCalledWith(originalRange);
  });
  it("tries user-initiated copy after a permission denial and reports a real failure", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    const { field, execCommand, focus } = fallback(false);
    await expect(copyText("answer")).rejects.toThrow("Clipboard is unavailable");
    expect(execCommand).toHaveBeenCalledOnce();
    expect(field.remove).toHaveBeenCalledOnce();
    expect(focus).toHaveBeenCalledOnce();
  });
});
