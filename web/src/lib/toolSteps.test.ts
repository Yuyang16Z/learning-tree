import { describe, expect, it } from "vitest";
import type { McpServer } from "../types";
import { finishToolStep, fullResultSource, startToolStep, toolReference, toolStepLabel } from "./toolSteps";

const zh = (text: string) => text;
const en = (_zh: string, text: string) => text;

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

describe("tool step names", () => {
  it("names the learning-history search and every other built-in reader in both languages", () => {
    expect(toolStepLabel("search_learning_history", zh)).toBe("搜索学习记录");
    expect(toolStepLabel("search_learning_history", en)).toBe("Search learning history");
    expect(toolStepLabel("read_learning_source", zh)).toBe("回查原文");
    expect(toolStepLabel("read_learning_source", en)).toBe("Read source passages");
    const builtIns = ["fetch", "web_search", "search_available_tools", "read_learning_source", "search_learning_history", "read_document_source", "read_tool_result"];
    for (const translate of [zh, en]) {
      const labels = builtIns.map(tool => toolStepLabel(tool, translate));
      expect(labels.every((label, index) => label !== builtIns[index])).toBe(true);
      expect(new Set(labels).size).toBe(builtIns.length);
    }
  });

  it("names the managed web-research tools and keeps any other tool's own name", () => {
    const web: McpServer = { id: 3, label: "联网搜索", command: "/usr/bin/python3", args: ["/work/integrations/mcp/launch.py", "web-research"], enabled: true };
    expect(toolStepLabel("mcp_3_web_search", en, [web])).toBe("Web search");
    expect(toolStepLabel("mcp_3_read_webpage", zh, [web])).toBe("读取网页");
    expect(toolStepLabel("mcp_3_web_search", en, [{ ...web, args: ["/custom/server.py", "web-research"] }])).toBe("mcp_3_web_search");
    expect(toolStepLabel("mcp_4_query", en, [web])).toBe("mcp_4_query");
    expect(toolStepLabel("constructor", en)).toBe("constructor");
  });
});
