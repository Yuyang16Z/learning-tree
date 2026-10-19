import { describe, expect, it } from "vitest";
import { formatMessage, type MessageParams } from "../i18n/locale";
import type { AnswerDiagnostics } from "../types";
import { budgetStopLabel, diagnosticsText } from "./answerDiagnostics";

const zh = (text: string) => text;
const en = (_zh: string, text: string) => text;
const enT = (zhText: string, enText: string, params?: MessageParams) => formatMessage("en", zhText, enText, params);
const zhT = (zhText: string, enText: string, params?: MessageParams) => formatMessage("zh-CN", zhText, enText, params);
const count = (value: number | null | undefined) => value == null ? "–" : value.toLocaleString("en-US");
const timings = { retrieval: 10, tools: 9000, summary: 0, model: 3000, total: 12300 };
const toolAnswer: AnswerDiagnostics = {
  estimated_input_tokens: 90000, actual_input_tokens: 89400, cached_input_tokens: 58000, output_tokens: 90,
  requests: 3, largest_request: { estimated_input_tokens: 31000, actual_input_tokens: 30900, cached_input_tokens: 28000 },
  compacted: false, tool_rounds: 2, calibration_ratio: 1, timings_ms: timings,
};

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

  it("shows one request's input as context and the sum over requests apart from it", () => {
    expect(diagnosticsText(toolAnswer, enT, count)).toEqual({
      summary: "Context: est. 31,000 · actual 30,900 tokens (cached 28,000) · 2 tool rounds · 12.3 s",
      total: "Input across 3 requests: est. 90,000 · actual 89,400 tokens (cached 58,000)",
    });
    expect(diagnosticsText(toolAnswer, zhT, count)).toEqual({
      summary: "上下文：估算 31,000 · 实际 30,900 tokens（缓存 28,000） · 工具 2 轮 · 12.3 s",
      total: "输入合计（3 次请求）：估算 90,000 · 实际 89,400 tokens（缓存 58,000）",
    });
  });

  it("keeps a single request's context unchanged and names one tool round in the singular", () => {
    const single: AnswerDiagnostics = {
      ...toolAnswer, estimated_input_tokens: 1234, actual_input_tokens: null, cached_input_tokens: null,
      requests: 1, largest_request: { estimated_input_tokens: 1234, actual_input_tokens: null, cached_input_tokens: null },
      tool_rounds: 0, timings_ms: { ...timings, total: 4200 },
    };
    expect(diagnosticsText(single, enT, count)).toEqual({ summary: "Context: est. 1,234 tokens · 4.2 s", total: null });
    const oneRound = { ...toolAnswer, requests: 2, tool_rounds: 1, tool_budget_stop: "time" as const };
    expect(diagnosticsText(oneRound, enT, count).summary).toBe("Context: est. 31,000 · actual 30,900 tokens (cached 28,000) · 1 tool round · Tool time limit reached · 12.3 s");
    expect(diagnosticsText(oneRound, zhT, count).summary).toContain("工具 1 轮");
  });

  it("never calls the stored sum of several requests context", () => {
    // Diagnostics stored before per-request figures were recorded hold only the sum.
    const { requests: _requests, largest_request: _largest, ...stored } = toolAnswer;
    expect(diagnosticsText(stored, enT, count)).toEqual({
      summary: "Total input: est. 90,000 · actual 89,400 tokens (cached 58,000) · 2 tool rounds · 12.3 s",
      total: null,
    });
    const { requests: _r, largest_request: _l, ...plain } = { ...toolAnswer, tool_rounds: 0 };
    expect(diagnosticsText(plain, enT, count).summary).toBe("Context: est. 90,000 · actual 89,400 tokens (cached 58,000) · 12.3 s");
    const malformed = { ...toolAnswer, largest_request: "not an object" } as unknown as AnswerDiagnostics;
    expect(diagnosticsText(malformed, enT, count).summary.startsWith("Input across 3 requests")).toBe(true);
  });
});
