import { describe, expect, it } from "vitest";
import { factTypeLabel } from "./factTypes";

describe("fact type labels", () => {
  it("gives every known type a Chinese and English label", () => {
    for (const type of ["definition", "condition", "misconception", "open_question", "learner_state", "conclusion"]) {
      const [zh, en] = factTypeLabel(type);
      expect(zh).not.toBe("");
      expect(en).not.toBe("");
    }
    expect(factTypeLabel("misconception")).toEqual(["纠正误解", "Correction"]);
  });

  it("reads legacy or unknown types as conclusions", () => {
    expect(factTypeLabel(undefined)).toEqual(["结论", "Conclusion"]);
    expect(factTypeLabel(null)).toEqual(["结论", "Conclusion"]);
    expect(factTypeLabel("mystery")).toEqual(["结论", "Conclusion"]);
  });
});
