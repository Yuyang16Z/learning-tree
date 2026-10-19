import { describe, expect, it } from "vitest";
import { locatePassage } from "./passage";

describe("locating an anchored passage in a rendered answer", () => {
  const content = "Quicksort needs a pivot.\nPivot choice\nTake the first element.\nFinally: median-of-three keeps it stable.";

  it("uses stored offsets while they still hold the quoted text", () => {
    expect(locatePassage(content, { start: 0, end: 9, text: "Quicksort" })).toEqual({ start: 0, end: 9 });
    expect(locatePassage(content, { start: 0, end: 9 })).toEqual({ start: 0, end: 9 });
  });

  it("finds the quoted text itself when offsets are missing or stale", () => {
    const start = content.indexOf("median-of-three");
    expect(locatePassage(content, { text: "median-of-three" })).toEqual({ start, end: start + 15 });
    expect(locatePassage(content, { start: 2, end: 8, text: "median-of-three" })).toEqual({ start, end: start + 15 });
    expect(locatePassage(content, { start: 0, end: 500, text: "median-of-three" })).toEqual({ start, end: start + 15 });
  });

  it("tolerates differences in case and whitespace from the rendered text", () => {
    const start = content.indexOf("Pivot choice");
    expect(locatePassage(content, { text: "pivot CHOICE" })).toEqual({ start, end: start + 12 });
    const across = content.indexOf("choice\nTake");
    expect(locatePassage(content, { text: "choice\n\nTake  the" })).toEqual({ start: across, end: across + 15 });
  });

  it("gives up when nothing matches", () => {
    expect(locatePassage(content, { text: "**median-of-three**" })).toBeNull();
    expect(locatePassage(content, {})).toBeNull();
    expect(locatePassage(content, { start: 5, end: 2 })).toBeNull();
  });
});
