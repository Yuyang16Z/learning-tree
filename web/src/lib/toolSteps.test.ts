import { describe, expect, it } from "vitest";
import { finishToolStep, fullResultSource, startToolStep, toolReference } from "./toolSteps";

describe("tool steps and their stored complete results", () => {
  it("fills the running step with its result and stored-result reference", () => {
    let steps = startToolStep([], "web_search");
    steps = finishToolStep(steps, "web_search", "preview", { call_index: 1, truncated: true, request_id: "r1", node_id: 7 });
    steps = startToolStep(steps, "fetch");
    expect(steps).toEqual([
      { tool: "web_search", result: "preview", call_index: 1, truncated: true, request_id: "r1", node_id: 7 },
      { tool: "fetch", result: null },
    ]);
    expect(finishToolStep(steps, "fetch", "done")[1]).toEqual({ tool: "fetch", result: "done" });
    expect(finishToolStep([], "fetch", "orphan")).toEqual([]);
  });

  it("reads references from tool_end events and ignores malformed values", () => {
    expect(toolReference({ call_index: 2, truncated: true }, { request_id: "r", node_id: 3 })).toEqual({ call_index: 2, truncated: true, request_id: "r", node_id: 3 });
    expect(toolReference({ truncated: false }, { request_id: "r", node_id: 3 })).toEqual({ truncated: false });
    expect(toolReference({ call_index: "2", truncated: "yes" }, { request_id: 5, node_id: "3" })).toEqual({});
    expect(toolReference({ call_index: 0 }, {})).toEqual({});
  });

  it("offers the complete text only for truncated steps with a stored reference", () => {
    const step = { tool: "t", result: "p", call_index: 4, truncated: true, request_id: "r" };
    expect(fullResultSource(step, 9)).toEqual({ nodeId: 9, callIndex: 4, requestId: "r" });
    expect(fullResultSource({ ...step, node_id: 2 }, 9)).toEqual({ nodeId: 2, callIndex: 4, requestId: "r" });
    expect(fullResultSource({ ...step, truncated: false }, 9)).toBeNull();
    expect(fullResultSource({ tool: "t", result: "p", truncated: true }, 9)).toBeNull();
    expect(fullResultSource(step, null)).toBeNull();
  });
});
