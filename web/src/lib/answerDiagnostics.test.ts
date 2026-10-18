import { describe, expect, it } from "vitest";
import { budgetStopLabel } from "./answerDiagnostics";

const zh = (text: string) => text;
const en = (_zh: string, text: string) => text;

describe("answer diagnostics labels", () => {
  it("names the agent budget that ended tool rounds in both languages", () => {
    expect(budgetStopLabel("time", zh)).toBe("工具时间已用完");
    expect(budgetStopLabel("time", en)).toBe("Tool time limit reached");
    expect(budgetStopLabel("tokens", zh)).toBe("工具结果已达上限");
    expect(budgetStopLabel("tokens", en)).toBe("Tool result budget reached");
  });

  it("shows nothing when no budget stopped the rounds or the value is unknown", () => {
    for (const value of [null, undefined, "rounds", 1, {}]) expect(budgetStopLabel(value, en)).toBeNull();
  });
});
